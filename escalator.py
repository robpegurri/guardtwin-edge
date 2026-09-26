"""
    RISK ESCALATION SERVICE CLIENT

Follows the broker's risk stream and sends a risk-event to the Risk
Escalation Service whenever the riskLevel changes, addressed to the devices
in the AoI. Those come from a list kept up to date in the background
(DeviceTracker): an ENVELOPE subscription on the AoI adds devices as soon as
they enter, a periodic devices-in-area query adds and removes them.
"""

import argparse
import json
import logging
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

import envelope_location as loc
import recorder

log = logging.getLogger("risk-escalator")
dlog = logging.getLogger("devices")      # parsed by the dashboard: keep the wording


def to_risk_level(risk_score):
    """Map the broker's 0-10 risk_score to the contract's 1-5 integer riskLevel.

    int(x + 0.5) instead of round(): round() rounds half-to-even, which
    would make e.g. risk_score=1 -> 0 but risk_score=3 -> 2 (asymmetric
    for a monotonic severity score); this rounds half up consistently.
    Clamped to [1, 5] since the contract rejects 0 (and anything outside
    1-5) with 400.
    """
    return max(1, min(5, int(risk_score / 2 + 0.5)))


def rfc3339(ts_us):
    """Format a broker ts_us (microseconds since epoch, UTC) as RFC 3339."""
    dt = datetime.fromtimestamp(ts_us / 1_000_000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _addresses(obj):
    """Every ipv4Address object anywhere in a callback payload (its exact
    shape is not documented: look everywhere rather than guess a path)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "ipv4Address" and isinstance(v, dict):
                yield v
            else:
                yield from _addresses(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _addresses(v)


class DeviceTracker:
    """
    The devices in the AoI, as the contract's devices[] entries, kept up to
    date in the background so that escalating never waits on ENVELOPE:
      - the AoI subscription's callbacks add devices as soon as they enter
        (there is no "left" callback);
      - a devices-in-area query every refresh_s adds and removes them; a
        device a callback added is not removed by a query within
        CALLBACK_GRACE_S of it, in case the queries lag behind;
      - a failed query leaves the list as it is.
    Subscriptions still notifying this host other than the current one
    (zombies: runs that could not unsubscribe) are deleted, retried every
    zombie_retry_s; their callbacks are dropped meanwhile (see
    loc.start_receiver).
    """
    CALLBACK_GRACE_S = 10.0

    def __init__(self, aoi, max_age_s, refresh_s, zombie_retry_s,
                 notify_port, notify_host):
        self.aoi, self.max_age_s = aoi, max_age_s
        self.refresh_s, self.zombie_retry_s = refresh_s, zombie_retry_s
        self.notify_port, self.notify_host = notify_port, notify_host
        self.lock = threading.Lock()
        self.devices = {}                    # key -> {"addr", "src", "t"}
        self.sub_id, self.sink, self.base, self.stop_receiver = None, None, None, None
        self.zombies, self.zombie_warned = set(), set()
        self.query_ok, self.incomplete_warned = True, False
        self._stop = threading.Event()

    # ---- the list
    @staticmethod
    def _key(addr):
        return (addr.get("publicAddress"), addr.get("privateAddress"), addr.get("publicPort"))

    @staticmethod
    def _label(addr):
        label = addr.get("publicAddress") or "?"
        if addr.get("publicPort"):
            label += f":{addr['publicPort']}"
        if addr.get("privateAddress") and addr["privateAddress"] != addr.get("publicAddress"):
            label += f" ({addr['privateAddress']})"
        return label

    def _complete(self, addresses):
        # The contract needs publicAddress and privateAddress or publicPort
        ok = [a for a in addresses if a.get("publicAddress")
              and (a.get("privateAddress") or a.get("publicPort"))]
        if len(ok) < len(addresses) and not self.incomplete_warned:
            dlog.warning("skipping address(es) without publicAddress and "
                         "privateAddress or publicPort: %s",
                         [a for a in addresses if a not in ok])
            self.incomplete_warned = True
        return ok

    def current(self):
        """devices[] for a risk-event (empty: nobody to warn)."""
        with self.lock:
            return [{"ipv4Address": dict(d["addr"])} for d in self.devices.values()]

    def _on_callback(self, event):
        addresses = [{k: a[k] for k in ("publicAddress", "privateAddress", "publicPort")
                      if a.get(k) is not None} for a in _addresses(event)]
        if not addresses:
            dlog.info("callback without devices: %s", json.dumps(event)[:300])
            return
        now = time.time()
        with self.lock:
            for a in self._complete(addresses):
                k = self._key(a)
                if k not in self.devices:
                    dlog.info("entered %s (callback)", self._label(a))
                self.devices[k] = {"addr": a, "src": "callback", "t": now}
        dlog.info("callback: %d device(s)", len(addresses))

    def _refresh(self):
        try:
            found = self._complete(loc.ipv4_addresses_in(self.aoi, max_age=self.max_age_s))
        except Exception as e:
            # Broad on purpose: an ENVELOPE hiccup must never stop the tracker
            if self.query_ok:
                dlog.warning("devices-in-area query failed (%s): list kept as it is", e)
            self.query_ok = False
            return
        if not self.query_ok:
            dlog.info("devices-in-area query works again")
        self.query_ok = True
        now, seen = time.time(), set()
        with self.lock:
            for a in found:
                k = self._key(a)
                seen.add(k)
                if k not in self.devices:
                    dlog.info("entered %s (query)", self._label(a))
                    self.devices[k] = {"addr": a, "src": "query", "t": now}
                else:
                    self.devices[k]["t"] = now
            for k, d in list(self.devices.items()):
                if k not in seen and not (d["src"] == "callback"
                                          and now - d["t"] < self.CALLBACK_GRACE_S):
                    dlog.info("left %s (query)", self._label(d["addr"]))
                    del self.devices[k]

    # ---- subscription and zombies
    def _subscribe(self):
        if self.base is None:                # the receiver first: it gives the sink
            self.sink, self.base, self.stop_receiver = loc.start_receiver(
                self._on_callback, self.notify_port, self.notify_host)
        self.sub_id = loc.subscribe(self.aoi, self.sink)
        dlog.info("subscribed %s", self.sub_id)

    def _reap_zombies(self):
        try:
            found = {s["id"] for s in loc.our_subscriptions(self.base)
                     if s.get("id") != self.sub_id}
        except Exception as e:
            dlog.debug("cannot list subscriptions (%s)", e)
            return
        left = set(found)
        for z in sorted(found):
            try:
                loc.unsubscribe(z)
                left.discard(z)
                dlog.info("removed zombie subscription %s", z)
            except Exception as e:
                if z not in self.zombie_warned:
                    dlog.warning("cannot remove zombie subscription %s (%s): "
                                 "retrying every %.0f s", z, e, self.zombie_retry_s)
                    self.zombie_warned.add(z)
        if left != self.zombies:             # the last one of these lines is the truth
            dlog.info("zombie subscriptions: %d%s", len(left),
                      f" ({', '.join(sorted(left))})" if left else "")
        self.zombies = left

    def run(self):
        dlog.info("tracking started")
        next_zombies, sub_warned = 0.0, False
        while not self._stop.is_set():
            if self.sub_id is None:
                try:
                    self._subscribe()
                except Exception as e:
                    if not sub_warned:
                        dlog.warning("cannot subscribe to the AoI (%s): retrying, "
                                     "queries only meanwhile", e)
                    sub_warned = True
            self._refresh()
            if self.base and time.time() >= next_zombies:
                self._reap_zombies()
                next_zombies = time.time() + self.zombie_retry_s
            self._stop.wait(self.refresh_s)

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    def close(self):
        self._stop.set()
        if self.sub_id:
            try:
                loc.unsubscribe(self.sub_id)
            except Exception as e:
                dlog.warning("could not unsubscribe %s (%s): removed as a zombie "
                             "by the next run", self.sub_id, e)
        if self.stop_receiver:
            self.stop_receiver()


def post_risk_event(url, token, event_id, body, timeout, retries, backoff_s):
    """
    POST one risk-event to the Risk Escalation Service (contract
    section 2). Retries on network errors / 5xx / 429, reusing the same
    eventId per the contract's idempotency rule ("retries must reuse
    eventId"); gives up immediately on 400/401 -- a client error won't
    be fixed by resending the same body.
    """
    data = json.dumps(body).encode()
    headers = {
        "Content-Type": "application/json",
        "x-correlator": event_id,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    log.debug("POST %s event_id=%s body=%s", url, event_id, body)

    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, data=data, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            recorder.record("risk_event", attempt=attempt, request=body, status=200,
                            body=raw.decode("utf-8", "replace"))
            resp = json.loads(raw)
            log.info("risk-event %s accepted -> decisionId=%s", event_id, resp.get("decisionId"))
            return True
        except urllib.error.HTTPError as e:
            problem = e.read().decode("utf-8", "replace")
            recorder.record("risk_event", attempt=attempt, request=body, status=e.code,
                            body=problem)
            if e.code in (400, 401):
                log.warning("risk-event %s rejected (HTTP %s): %s", event_id, e.code, problem)
                return False
            log.warning("risk-event %s failed (HTTP %s), attempt %d/%d: %s",
                        event_id, e.code, attempt, retries, problem)
        except (urllib.error.URLError, OSError, ValueError) as e:
            recorder.record("risk_event", attempt=attempt, request=body, error=str(e))
            log.warning("risk-event %s failed (%s), attempt %d/%d", event_id, e, attempt, retries)
        if attempt < retries:
            time.sleep(backoff_s * attempt)

    log.error("risk-event %s giving up after %d attempts", event_id, retries)
    return False


_no_device_warned = False


def escalate(args, tracker, record, last_level):
    """
    If record's riskLevel differs from last_level, build and send a
    risk-event; returns the level that should become the new
    last_level (unchanged on a skip, e.g. no device currently in the
    AoI -- retried on the next record, which costs nothing: the devices
    come from the tracker's list, not from a query).
    """
    global _no_device_warned
    level = to_risk_level(record["risk_score"])
    if level == last_level:
        return last_level

    log.debug("risk level changed %s -> %s (risk_score=%s)", last_level, level, record["risk_score"])
    devices = tracker.current()
    if not devices:
        # The service rejects risk-events without devices. Said once while
        # nobody is there, not at every level change
        if not _no_device_warned:
            log.warning("no device in the AoI: risk-events skipped until one "
                        "enters (riskLevel now %s)", level)
            _no_device_warned = True
        else:
            log.debug("riskLevel %s, no device in the AoI: skipped", level)
        return last_level
    _no_device_warned = False

    body = {
        "eventId": str(uuid.uuid4()),
        "riskLevel": level,
        "occurredAt": rfc3339(record["ts_us"]),
        "devices": devices,
    }
    aoi_id = args.risk_escalator_aoi_id or args.aoi_id
    if aoi_id:
        body["aoiId"] = aoi_id

    url = args.risk_escalation_url.rstrip("/") + "/api/v1/risk-events"
    post_risk_event(url, args.evaluator_token, body["eventId"], body,
                    args.risk_escalation_timeout, args.risk_escalation_retries,
                    args.risk_escalation_backoff_s)
    return level


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1",
                    help="broker.py host to connect to for the smi.risk stream")
    ap.add_argument("--port", type=int, default=30500,
                    help="broker.py --serve-port to connect to")
    ap.add_argument("--reconnect-s", type=float, default=2.0,
                    help="seconds to wait before reconnecting if the connection drops")
    ap.add_argument("--aoi-lat", type=float, default=None,
                    help="AoI center latitude, for the devices-in-area lookup "
                         "(same value passed to startup.py/broker.py); required "
                         "for --risk-escalation-url to do anything")
    ap.add_argument("--aoi-lon", type=float, default=None,
                    help="AoI center longitude")
    ap.add_argument("--aoi-radius", type=float, default=150,
                    help="AoI radius in meters")
    ap.add_argument("--aoi-id", default=None,
                    help="legacy alias for --risk-escalator-aoi-id")
    ap.add_argument("--risk-escalator-aoi-id", default=None,
                    help="AoI name sent as aoiId with each risk-event")
    ap.add_argument("--devices-max-age-s", type=int, default=60,
                    help="how recent a device's last ENVELOPE sighting must be to "
                         "count as 'in the AoI' for devices[]")
    ap.add_argument("--devices-refresh-s", type=float, default=5.0,
                    help="seconds between the devices-in-area queries that keep "
                         "the device list up to date (callbacks add devices at once)")
    ap.add_argument("--zombie-retry-s", type=float, default=60.0,
                    help="seconds between attempts to delete leftover "
                         "subscriptions notifying this host")
    ap.add_argument("--notify-port", type=int, default=8080,
                    help="local port for the ENVELOPE subscription callbacks")
    ap.add_argument("--notify-host", default=None,
                    help="externally-reachable host/IP ENVELOPE should call "
                         "back on; required when running in a container "
                         "behind published ports (auto-detected otherwise)")
    ap.add_argument("--risk-escalation-url", default=None,
                    help="Risk Escalation Service base URL "
                         "(POST {url}/api/v1/risk-events); omit to only log locally")
    ap.add_argument("--evaluator-token", default=None,
                    help="bearer token issued to this evaluator for the Risk "
                         "Escalation Service (EVALUATOR_BEARER_TOKEN)")
    ap.add_argument("--risk-escalation-timeout", type=float, default=5.0)
    ap.add_argument("--risk-escalation-retries", type=int, default=3,
                    help="attempts per risk-event before giving up (contract "
                         "recommends retry with backoff on 5xx/network errors)")
    ap.add_argument("--risk-escalation-backoff-s", type=float, default=1.0,
                    help="base backoff between retries, multiplied by the attempt number")
    ap.add_argument("--log-file", default=None,
                    help="append every record received, exactly as it arrived "
                         "(one NDJSON line each), to this file -- e.g. for "
                         "offline review of env.hazards when "
                         "tuning the LLM prompt; omit to not keep a log")
    ap.add_argument("--record-dir", default=None,
                    help="where to record every input (ENVELOPE callbacks, "
                         "queries and subscription calls, Risk Escalation "
                         "responses) while the dashboard's Record button is on "
                         "(see recorder.py)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="DEBUG logging: every record's risk_level, devices-in-area "
                         "query results, outgoing risk-event bodies")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    if args.risk_escalation_url and (args.aoi_lat is None or args.aoi_lon is None):
        ap.error("--risk-escalation-url requires --aoi-lat/--aoi-lon "
                 "(devices[] is sourced from the AoI devices-in-area lookup)")

    aoi = loc.area(args.aoi_lat, args.aoi_lon, args.aoi_radius) \
        if args.aoi_lat is not None else None

    if args.record_dir:
        safe = {k: ("…" if k == "evaluator_token" and v else v) for k, v in vars(args).items()}
        recorder.configure(args.record_dir, "escalator",
                           lambda: dict(args=safe, envelope_url=loc.BASE))

    # `docker stop` sends SIGTERM: exit normally, so that the finally below
    # unsubscribes (Python's default would die on the spot, leaving a zombie)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    tracker = None
    if aoi:
        tracker = DeviceTracker(aoi, args.devices_max_age_s, args.devices_refresh_s,
                                args.zombie_retry_s, args.notify_port, args.notify_host)
        tracker.start()

    log_file = open(args.log_file, "a", buffering=1) if args.log_file else None
    last_level = None

    try:
        while True:
            try:
                with socket.create_connection((args.host, args.port)) as sock:
                    log.info("connected to %s:%d", args.host, args.port)
                    for line in sock.makefile("r", encoding="utf-8"):
                        line = line.strip()
                        if not line:
                            continue
                        if log_file:
                            log_file.write(line + "\n")
                        try:
                            record = json.loads(line)
                            level = to_risk_level(record["risk_score"])
                        except (ValueError, KeyError):
                            log.warning("unparseable record, passing through: %s", line)
                            continue
                        log.debug("%s  (risk_level=%s)", line, level)
                        if args.risk_escalation_url:
                            last_level = escalate(args, tracker, record, last_level)
            except (ConnectionError, OSError) as e:
                log.warning("connection lost (%s), retrying in %ss", e, args.reconnect_s)
            except KeyboardInterrupt:
                break
            time.sleep(args.reconnect_s)
    finally:
        if tracker:
            tracker.close()
        if log_file:
            log_file.close()


if __name__ == "__main__":
    main()