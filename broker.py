"""
    ENVELOPE GUARD-TWIN BROKER
"""

import argparse
import json
import logging
import math
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

import ai_assess

log = logging.getLogger("broker")


# Network state details given the IMSI list in ---imsi
class UEState:
    __slots__ = ("imsi", "ue_id", "cm_state", "score", "score_ts_us",
                 "score_mono")

    def __init__(self, imsi):
        self.imsi = imsi
        self.ue_id = None          # ranUeNgapID (int) or None if unresolved
        self.cm_state = None       # cmState from the AMF, e.g. "connected"
        self.score = None          # channel score 0-10, None if unavailable
        self.score_ts_us = None    # metric sample time, us since epoch
        self.score_mono = None     # time.monotonic() of the last good score


class NetworkState:

    # Polls AMF and metrics server for every configured IMSI.
    def __init__(self, args):
        self.args = args
        self._lock = threading.Lock()
        self._ues = [UEState(imsi) for imsi in args.imsi]
        self._stop = threading.Event()

    # AMF: IMSI -> ranUeNgapID
    def _resolve_ue_id(self, ue):
        supi = ue.imsi if ue.imsi.startswith("imsi-") else "imsi-" + ue.imsi
        url = f"{self.args.resolver_url}/api/v1/ue/{supi}"
        try:
            with urllib.request.urlopen(url, timeout=self.args.http_timeout) as r:
                doc = json.load(r)
            with self._lock:
                ue.ue_id = doc.get("ranUeNgapID")
                ue.cm_state = doc.get("cmState")
            log.info("AMF: %s -> ranUeNgapID=%s cmState=%s",
                     supi, ue.ue_id, ue.cm_state)
        except urllib.error.HTTPError as e:
            # 404 & co.: the UE is not registered right now
            with self._lock:
                ue.ue_id, ue.cm_state = None, None
            log.warning("AMF: %s not registered (HTTP %s)", supi, e.code)
        except Exception as e:
            # network error: keep the last known ue_id, it is our best guess
            log.warning("AMF unreachable (%s): keeping ue_id=%s for %s",
                        e, ue.ue_id, ue.imsi)

    # Metrics: channel score for one resolved UE
    def _poll_score(self, ue):
        with self._lock:
            ue_id = ue.ue_id
        if ue_id is None:
            return
        url = (f"{self.args.metrics_url}/ue_mac_stats/{self.args.gnb_id}"
               f"/ran_ngap_ue_id/{ue_id}/score")
        try:
            with urllib.request.urlopen(url, timeout=self.args.http_timeout) as r:
                doc = json.load(r)
        except Exception as e:
            log.warning("metrics unreachable (%s)", e)
            return
        if not isinstance(doc, dict) or "Score" not in doc:
            # the server answers a plain string on errors ("No data for ...")
            log.warning("metrics: no score for ue_id=%s: %r", ue_id, doc)
            return
        ts = doc.get("time")
        try:
            ts_us = int(datetime.fromisoformat(
                ts.replace("Z", "+00:00")).timestamp() * 1e6)
        except Exception:
            ts_us = None
        with self._lock:
            ue.score = doc["Score"]
            ue.score_ts_us = ts_us
            ue.score_mono = time.monotonic()

    # Background loop
    def run(self):
        next_resolve = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            resolve = now >= next_resolve
            if resolve:
                next_resolve = now + self.args.resolve_period
            for ue in self._ues:
                if resolve or ue.ue_id is None:
                    self._resolve_ue_id(ue)
                self._poll_score(ue)
            self._stop.wait(self.args.score_period)

    def start(self):
        t = threading.Thread(target=self.run, name="net-state", daemon=True)
        t.start()

    def stop(self):
        self._stop.set()

    def snapshot(self):
        """Consistent view of all UEs for one fused frame."""
        out = []
        now = time.monotonic()
        with self._lock:
            for ue in self._ues:
                age = (now - ue.score_mono) if ue.score_mono is not None else None
                if ue.ue_id is None:
                    state = "unresolved"
                elif ue.score is None or age is None:
                    state = "no_score"
                elif age > self.args.score_stale_s:
                    state = "stale"
                else:
                    state = "ok"
                out.append({
                    "imsi": ue.imsi,
                    "ue_id": ue.ue_id,
                    "cm": ue.cm_state,
                    "score": ue.score,
                    "score_ts_us": ue.score_ts_us,
                    "score_age_s": round(age, 2) if age is not None else None,
                    "state": state,
                })
        return out



# TCP server for for the Risk Escalator (escalator.py)
class EscalatorServer:
    def __init__(self, host, port):
        self.addr = (host, port)
        self._lock = threading.Lock()
        self._clients = []  # list of sockets

    def start(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(self.addr)
        srv.listen(8)
        threading.Thread(target=self._accept_loop, args=(srv,),
                         name="escalator-accept", daemon=True).start()
        log.info("Escalator server listening on tcp://%s:%d", *self.addr)

    def _accept_loop(self, srv):
        while True:
            client, peer = srv.accept()
            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._lock:
                self._clients.append(client)
            log.info("Escalator client connected from %s:%d (%d total)",
                     peer[0], peer[1], len(self._clients))

    def broadcast(self, line_bytes):
        # Send one NDJSON line to every client
        dead = []
        with self._lock:
            for c in self._clients:
                try:
                    c.sendall(line_bytes)
                except OSError:
                    dead.append(c)
            for c in dead:
                self._clients.remove(c)
                try:
                    c.close()
                except OSError:
                    pass
        if dead:
            log.info("dropped %d Escalator client(s)", len(dead))

    @property
    def n_clients(self):
        with self._lock:
            return len(self._clients)


# Flat-earth helpers (AoI check, yaw factor)

def _dist_m(lat1, lon1, lat2, lon2):
    # Flat-earth approximation
    dlat = (lat2 - lat1) * 111320.0
    dlon = (lon2 - lon1) * 111320.0 * math.cos(math.radians((lat1 + lat2) / 2.0))
    return math.hypot(dlat, dlon)


def _bearing_deg(lat1, lon1, lat2, lon2):
    # Compass bearing from (lat1,lon1) to (lat2,lon2), degrees from North (clockwise).
    north = (lat2 - lat1) * 111320.0
    east = (lon2 - lon1) * 111320.0 * math.cos(math.radians((lat1 + lat2) / 2.0))
    return math.degrees(math.atan2(east, north)) % 360.0


# --------------------------------------------------------------------------
# Risk model (see DATA_MODEL.md)
#
# The bicycle publishes radar/geo frames; the Escalator receives a single risk
# grade 0-10 and acts on it. Per obstacle:
#   - TTC factor: time-to-collision = range / closing speed (radar speed
#     is used as closing speed when the obstacle is approaching).
#     TTC <= 1.5 s -> 1.0, TTC >= 8 s -> 0.0, linear in between.
#   - proximity factor: 1.0 at 0 m, 0.0 at >= 30 m, linear.
#   - class weight: heavy vehicles threaten a cyclist more than pedestrians.
#   - track confidence scales everything; static obstacles are damped.
# Obstacle risks combine as a probabilistic OR (two cars at TTC 3 s are
# worse than one). The channel quality then *qualifies the reliability*
# of the radar data reaching the Escalator: a degraded link amplifies the risk
# the radar has actually detected (the Escalator may not be able to warn in
# time), but on its own it does not create risk — an empty scene stays
# at zero whatever the link state.
# --------------------------------------------------------------------------
CLASS_WEIGHT = {"TRUCK": 1.0, "CAR": 0.9, "SCOOTER": 0.7, "BICYCLE": 0.6,
                "PERSON": 0.5, "UNKNOWN": 0.7, "OTHER": 0.7}
TTC_MIN_S, TTC_MAX_S = 1.5, 8.0     # full risk .. no risk
PROX_MAX_M = 30.0                   # beyond this an obstacle is ignored
STATIC_DAMP = 0.5
CHAN_PENALTY_MAX = 3.0              # default --chan-penalty-max: points added
                                     # at radar risk 10 with a dead link
YAW_BOOST_MAX = 0.15                # extra risk (0-1 object scale) for an object
                                     # whose confirmed direction of motion (DROPMO
                                     # v1.1 yaw) heads back towards the bike
THREAT_FLOOR = 0.05                 # per-object risk below this is noise
LEVELS = [(1.0, "none"), (3.0, "low"), (5.0, "medium"),
          (7.5, "high"), (11.0, "critical")]
# The AI Environmental Risk (ai_assess.py) adds up to --ai-context-max
# points on top, from the site map and the time of day alone.


def _yaw_closing(o, ego_lat, ego_lon):
    # 0-1: how much the object's confirmed direction of motion (yaw) heads
    # back towards the bike, regardless of current range-rate.

    if not o.get("yaw_valid"):
        return 0.0
    yaw_deg, lat, lon = o.get("yaw_deg"), o.get("lat"), o.get("lon")
    if yaw_deg is None or lat is None or lon is None or ego_lat is None or ego_lon is None:
        return 0.0
    bearing_obj_to_ego = (_bearing_deg(ego_lat, ego_lon, lat, lon) + 180.0) % 360.0
    delta = (yaw_deg - bearing_obj_to_ego + 180.0) % 360.0 - 180.0  # wrap to [-180,180]
    return max(0.0, math.cos(math.radians(delta)))


def object_risk(o, ego_lat=None, ego_lon=None):
    # Risk 0-1 contributed by one radar object, plus its TTC.

    rng = o.get("range_m")
    if rng is None or rng > PROX_MAX_M:
        return 0.0, None
    prox = 1.0 - rng / PROX_MAX_M

    ttc = None
    f_ttc = 0.0
    v_close = (o.get("speed_kmh") or 0.0) / 3.6
    if o.get("approaching") and v_close > 0.1:
        ttc = rng / v_close
        if ttc <= TTC_MIN_S:
            f_ttc = 1.0
        elif ttc < TTC_MAX_S:
            f_ttc = (TTC_MAX_S - ttc) / (TTC_MAX_S - TTC_MIN_S)

    conf = o.get("track_confidence")
    conf = 1.0 if conf is None else conf
    weight = CLASS_WEIGHT.get(o.get("class"), 0.7)
    yaw_close = _yaw_closing(o, ego_lat, ego_lon)
    risk = conf * weight * (0.6 * f_ttc + 0.4 * prox + YAW_BOOST_MAX * yaw_close)
    if o.get("static"):
        risk *= STATIC_DAMP
    return min(1.0, risk), (round(ttc, 1) if ttc is not None else None)


def pick_channel(ues):
    # Best available channel among the monitored UEs (the bike's link)
    rank = {"ok": 0, "stale": 1, "no_score": 2, "unresolved": 3}
    best = min(ues, key=lambda u: (rank.get(u["state"], 9),
                                   -(u["score"] or 0.0))) if ues else None
    return best


def assess(frame, ues, env, args):
    now_us = int(time.time() * 1e6)
    ego_in = frame.get("ego") or {}
    lat, lon = ego_in.get("lat"), ego_in.get("lon")

    # Per-object risks
    threats, objects = [], []
    no_risk_prod = 1.0
    objs = frame.get("objects") or []
    for o in objs:
        risk, ttc = object_risk(o, lat, lon)
        if o.get("lat") is not None and o.get("lon") is not None:
            # every located object, for map views (threats: the risky ones)
            objects.append({"cls": o.get("class"), "pos": [o["lat"], o["lon"]],
                            "risk": round(risk * 10, 1),
                            "appr": bool(o.get("approaching")),
                            "static": bool(o.get("static"))})
        if risk < THREAT_FLOOR:
            continue
        no_risk_prod *= (1.0 - risk)
        threats.append({
            "cls": o.get("class"),
            "pos": ([o["lat"], o["lon"]]
                    if o.get("lat") is not None else None),
            "range_m": round(o.get("range_m", 0.0), 1),
            "ttc_s": ttc,
            "appr": bool(o.get("approaching")),
            "yaw_valid": bool(o.get("yaw_valid")),
            "yaw_deg": (round(o["yaw_deg"], 1)
                       if o.get("yaw_valid") and o.get("yaw_deg") is not None else None),
            "risk": round(risk * 10, 1),
        })
    threats.sort(key=lambda t: -t["risk"])
    radar_risk = (1.0 - no_risk_prod) * 10.0

    # Channel contribution
    chan = pick_channel(ues)
    if chan and chan["state"] in ("ok", "stale") and chan["score"] is not None:
        link_quality = chan["score"] / 10.0
    else:
        link_quality = 0.0
    chan_penalty = radar_risk / 10.0 * args.chan_penalty_max * (1.0 - link_quality)

    # AI Environmental Risk: already scored by ai_assess, 0 unless valid here
    ai_env_risk = env["points"]

    risk = min(10.0, radar_risk + chan_penalty + ai_env_risk)
    level = next(name for thr, name in LEVELS if risk < thr)

    t_cap = frame.get("t_capture_us")
    return {
        "service": "guardtwin.risk",
        "src": chan["imsi"] if chan else args.imsi[0],
        "ts_us": now_us,
        "seq": frame.get("seq"),
        "age_ms": (round((now_us - t_cap) / 1e3, 1)
                   if isinstance(t_cap, int) else None),
        "risk_score": round(risk, 1),
        "risk_level": level,
        "radar_risk": round(radar_risk, 1),
        "chan_penalty": round(chan_penalty, 1),
        "channel": ({"imsi": chan["imsi"], "ue_id": chan["ue_id"],
                     "score": chan["score"], "state": chan["state"]}
                    if chan else None),
        "ai_env_risk": round(ai_env_risk, 1),
        # the most each component can contribute (for gauges)
        "limits": {"radar_risk": 10.0, "chan_penalty": args.chan_penalty_max,
                   "ai_env_risk": args.ai_context_max},
        "env": {"state": env["state"], "age_s": env["age_s"],
                "hazards": env["hazards"], "asked_pos": env.get("asked_pos"),
                "call": env.get("call"), "stats": env.get("stats")},
        "ego": {
            "pos": [lat, lon] if lat is not None else None,
            "heading_deg": ego_in.get("heading_deg"),
            "speed_mps": ego_in.get("speed_mps"),
            "fix": bool(ego_in.get("fix_ok")),
            "sim": bool(ego_in.get("simulated")),
        },
        "n_obj": len(objs),
        "threats": threats[:3],
        "objects": objects,
    }


def imsi_list(value):
    items = [x.strip() for x in value.split(",") if x.strip()]
    if not items:
        raise argparse.ArgumentTypeError("empty IMSI list")
    return items


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--imsi", type=imsi_list, required=True,
                    help="comma-separated list of IMSIs to monitor, "
                         "e.g. 001010000167808,001010000167802")
    ap.add_argument("--listen-host", default="0.0.0.0")
    ap.add_argument("--listen-port", type=int, default=30490,
                    help="UDP port for incoming geo JSON frames (user traffic)")
    ap.add_argument("--serve-host", default="0.0.0.0")
    ap.add_argument("--serve-port", type=int, default=30500,
                    help="TCP port where Risk Escalator clients connect to "
                         "receive the fused NDJSON stream")
    ap.add_argument("--resolver-url", default="http://172.24.254.140:8080",
                    help="base URL of the AMF API (IMSI -> ranUeNgapID)")
    ap.add_argument("--metrics-url", default="http://10.211.1.140:8080",
                    help="base URL of the metrics REST server")
    ap.add_argument("--gnb-id", default="f01")
    ap.add_argument("--score-period", type=float, default=1.0,
                    help="seconds between score polls")
    ap.add_argument("--resolve-period", type=float, default=60.0,
                    help="seconds between IMSI re-resolutions")
    ap.add_argument("--score-stale-s", type=float, default=5.0,
                    help="score older than this is flagged 'stale'")
    ap.add_argument("--http-timeout", type=float, default=3.0)
    ap.add_argument("--chan-penalty-max", type=float, default=CHAN_PENALTY_MAX,
                    help="most points the Channel Penalty adds: reached with "
                         "radar risk 10 and a dead link (it scales with both)")
    ai_assess.add_arguments(ap)
    ap.add_argument("--aoi-lat", type=float, default=None,
                    help="area of interest center latitude; frames whose ego "
                         "position falls outside --aoi-radius meters of this "
                         "point are dropped, with a log warning (omit, along "
                         "with --aoi-lon, to disable this check)")
    ap.add_argument("--aoi-lon", type=float, default=None,
                    help="area of interest center longitude")
    ap.add_argument("--aoi-radius", type=float, default=150,
                    help="area of interest radius in meters (only used if "
                         "--aoi-lat/--aoi-lon are given)")
    ap.add_argument("--escalator-host", default=None,
                    help="optional UDP push: send each fused frame as one "
                         "datagram to this host as well")
    ap.add_argument("--escalator-port", type=int, default=30491)
    ap.add_argument("--jsonl-out", default=None,
                    help="also append fused frames to this JSON Lines file")
    ap.add_argument("--stdout", action="store_true",
                    help="print each fused frame to stdout")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    geometry = ai_assess.load_geometry(args.geometry_file, args.geometry_max_bytes)

    net_state = NetworkState(args)
    net_state.start()

    env_estimator = ai_assess.EnvRiskEstimator(args, geometry)
    env_estimator.start()

    escalator_server = EscalatorServer(args.serve_host, args.serve_port)
    escalator_server.start()

    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind((args.listen_host, args.listen_port))
    log.info("listening for user traffic on udp://%s:%d (IMSIs: %s)",
             args.listen_host, args.listen_port, ",".join(args.imsi))

    tx = None
    if args.escalator_host:
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        log.info("also pushing fused frames to udp://%s:%d",
                 args.escalator_host, args.escalator_port)

    out_file = open(args.jsonl_out, "a", buffering=1) if args.jsonl_out else None

    n_rx = n_bad = n_out_aoi = 0
    try:
        while True:
            data, addr = rx.recvfrom(65535)
            try:
                frame = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                n_bad += 1
                log.warning("bad datagram from %s (%s), %d so far", addr, e, n_bad)
                continue
            if args.aoi_lat is not None and args.aoi_lon is not None:
                ego_in = frame.get("ego") or {}
                lat, lon = ego_in.get("lat"), ego_in.get("lon")
                if lat is not None and lon is not None:
                    d = _dist_m(lat, lon, args.aoi_lat, args.aoi_lon)
                    if d > args.aoi_radius:
                        n_out_aoi += 1
                        log.warning("dropping frame seq=%s from %s: %.0fm outside "
                                    "the AoI (radius %.0fm), %d so far",
                                    frame.get("seq"), addr[0], d - args.aoi_radius,
                                    args.aoi_radius, n_out_aoi)
                        continue
            ego = frame.get("ego") or {}
            env_estimator.update_pose(ego)
            record = assess(frame, net_state.snapshot(),
                            env_estimator.snapshot(ego), args)
            payload = json.dumps(record, separators=(",", ":"))
            escalator_server.broadcast(payload.encode() + b"\n")
            if tx:
                tx.sendto(payload.encode(), (args.escalator_host, args.escalator_port))
            if out_file:
                out_file.write(payload + "\n")
            if args.stdout:
                print(payload, flush=True)
            n_rx += 1
            if n_rx % 100 == 0:
                log.info("assessed %d frames (last seq=%s, risk=%.1f/%s, "
                         "escalator_clients=%d)", n_rx, record["seq"],
                         record["risk_score"], record["risk_level"],
                         escalator_server.n_clients)
    except KeyboardInterrupt:
        pass
    finally:
        net_state.stop()
        env_estimator.stop()
        rx.close()
        if out_file:
            out_file.close()
        log.info("done: %d frames fused, %d bad datagrams, %d dropped (outside AoI)",
                 n_rx, n_bad, n_out_aoi)


if __name__ == "__main__":
    main()
