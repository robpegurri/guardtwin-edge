#!/usr/bin/env python3
"""Risk Escalation Service client.

Connects to broker.py's --serve-port, reads each smi.risk NDJSON
record, maps risk_score (0-10) to the contract's riskLevel (1-5) and,
on every change of that level, POSTs a risk-event to the Risk
Escalation Service (INSOFTDEV middleware, SPEC-001 v2.2) -- the
CAMARA-adjacent consumer entity that replaces "the digital twin".
devices[] is the list of IPv4 addresses ENVELOPE currently reports
inside the AoI (Devices-in-Area query), since the risk applies to
whichever device(s) are physically in the risky area, not only the
sensing bike.
"""

import argparse
import json
import logging
import socket
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

import envelope_location as loc

log = logging.getLogger("dt-client")


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


def devices_in_aoi(aoi, max_age_s):
    """
    IPv4 addresses ENVELOPE currently reports inside the AoI (last
    max_age_s seconds), shaped as the contract's devices[] entries.

    Caught broadly on purpose: an ENVELOPE hiccup (ConnectionError is an
    OSError subclass) must not bubble up into the broker socket loop's
    own except (ConnectionError, OSError) in main() -- that would make a
    transient ENVELOPE outage look like the broker TCP connection
    dropped and needlessly reconnect it, right when the risk-event we
    were trying to build is the one that's actually failing.
    """
    try:
        addresses = loc.ipv4_addresses_in(aoi, max_age=max_age_s)
        devices = []
        skipped = 0
        for address in addresses:
            if (address.get("publicAddress") and
                    (address.get("privateAddress") or address.get("publicPort"))):
                devices.append({"ipv4Address": address})
            else:
                skipped += 1
        if skipped:
            log.warning("devices-in-area skipped %d address(es) without publicAddress "
                        "and privateAddress or publicPort", skipped)
        log.debug("devices-in-area (max_age=%ss): %d found: %s", max_age_s, len(devices), devices)
        return devices
    except Exception as e:
        log.warning("devices-in-area lookup failed (%s)", e)
        return []


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
                resp = json.load(r)
            log.info("risk-event %s accepted -> decisionId=%s", event_id, resp.get("decisionId"))
            return True
        except urllib.error.HTTPError as e:
            problem = e.read().decode("utf-8", "replace")
            if e.code in (400, 401):
                log.warning("risk-event %s rejected (HTTP %s): %s", event_id, e.code, problem)
                return False
            log.warning("risk-event %s failed (HTTP %s), attempt %d/%d: %s",
                        event_id, e.code, attempt, retries, problem)
        except (urllib.error.URLError, OSError) as e:
            log.warning("risk-event %s failed (%s), attempt %d/%d", event_id, e, attempt, retries)
        if attempt < retries:
            time.sleep(backoff_s * attempt)

    log.error("risk-event %s giving up after %d attempts", event_id, retries)
    return False


def escalate(args, aoi, record, last_level):
    """
    If record's riskLevel differs from last_level, build and send a
    risk-event; returns the level that should become the new
    last_level (unchanged on a skip, e.g. no device currently in the
    AoI -- retried on the next record instead of on a timer).
    """
    level = to_risk_level(record["risk_score"])
    if level == last_level:
        return last_level

    log.debug("risk level changed %s -> %s (risk_score=%s)", last_level, level, record["risk_score"])
    devices = devices_in_aoi(aoi, args.devices_max_age_s)
    if not devices:
        log.warning("risk level changed to %s but no device currently in the "
                    "AoI, skipping risk-event", level)
        return last_level

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
                         "offline review of env.reasoning/env.score when "
                         "tuning the LLM prompt; omit to not keep a log")
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
                            last_level = escalate(args, aoi, record, last_level)
            except (ConnectionError, OSError) as e:
                log.warning("connection lost (%s), retrying in %ss", e, args.reconnect_s)
            except KeyboardInterrupt:
                break
            time.sleep(args.reconnect_s)
    finally:
        if log_file:
            log_file.close()


if __name__ == "__main__":
    main()
