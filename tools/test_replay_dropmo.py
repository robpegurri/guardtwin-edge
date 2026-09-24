#!/usr/bin/env python3
r"""Test harness for broker.py: replays real DROPMO v1.1 radar objects
(a Dropper session already decoded to `objects_yaw.csv` by
DROPMO/tools/convert_session.py) as if they were arriving from the
UNIMORE OBU's geo bridge, i.e. as broker.py's expected georeferenced
UDP JSON.

DROPMO objects carry no GPS at all: they are radar-relative (x=right,
y=forward along the boresight, metres) -- geo-referencing them is the
job of the (real, GPS-equipped) UNIMORE OBU, not of DROPMO or of this
repo. The two recorded sessions under DROPMO/data/ are bench-rig
captures with the bike stationary the whole time, so there is no real
GPS track to replay either. This script fakes both: it anchors the
bike at one fixed lat/lon (--ego-lat/--ego-lon) with one fixed heading
(--heading-deg, arbitrary -- the bike never actually turned), and
projects each object's radar-relative position around that anchor.
Good enough to exercise the real pipeline (fusion, risk model, yaw)
against real radar object shapes; the resulting lat/lon values are not
geographically meaningful.

DROPMO's own dropmo.yaw helpers (sensor_to_vehicle_yaw,
vehicle_to_enu_yaw, enu_yaw_to_heading_deg) do the same mount/heading
composition for the yaw angle, so an object's world-frame yaw_deg is
consistent with its projected position.

Also stands up mock AMF/metrics servers, like test_replay.py, but with
a constant or random channel score (this dataset has no real channel
log to cycle through).

Usage (from the repo root; run broker.py separately, pointed at this
script's mock servers):

    python3 tools/test_replay_dropmo.py --amf-port 18080 --metrics-port 18081 &
    python3 broker.py --imsi 001010000167806 \
        --listen-port 30491 --serve-port 30500 \
        --resolver-url http://127.0.0.1:18080 \
        --metrics-url http://127.0.0.1:18081 --gnb-id f01 \
        --llm-url http://127.0.0.1:8000 --geometry-file files/geometry.geojson \
        --aoi-lat 45.0649195 --aoi-lon 7.659724166666667 --aoi-radius 150 \
        --stdout -v
"""

import argparse
import csv
import json
import logging
import math
import random
import socket
import sys
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("test-replay-dropmo")

DROPMO_PATH_DEFAULT = "/home/guardtwin/DROPMO"
DEFAULT_CSV = f"{DROPMO_PATH_DEFAULT}/data/2026-09-11_test/objects_yaw.csv"


# --------------------------------------------------------------------------
# Mock AMF / metrics (same shape as test_replay.py)
# --------------------------------------------------------------------------
def make_amf_handler(ue_id):
    class AMFHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"ranUeNgapID": ue_id, "cmState": "connected"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *a):
            log.debug("mock-amf: %s", fmt % a)

    return AMFHandler


def make_metrics_handler(mode, constant_score, rand_min, rand_max):
    class MetricsHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            score = (constant_score if mode == "constant"
                     else random.uniform(rand_min, rand_max))
            body = json.dumps({
                "Score": round(score, 2),
                "time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *a):
            log.debug("mock-metrics: %s", fmt % a)

    return MetricsHandler


# --------------------------------------------------------------------------
# Geo projection: DROPMO radar-relative (x right, y forward) -> world lat/lon
# Same flat-earth approximation as broker.py's _dist_m, inverted.
# --------------------------------------------------------------------------
def project_latlon(ego_lat, ego_lon, x_m, y_m, heading_deg):
    h = math.radians(heading_deg)
    east_m = x_m * math.cos(h) + y_m * math.sin(h)
    north_m = -x_m * math.sin(h) + y_m * math.cos(h)
    lat = ego_lat + north_m / 111320.0
    lon = ego_lon + east_m / (111320.0 * math.cos(math.radians(ego_lat)))
    return lat, lon


# --------------------------------------------------------------------------
# Load objects_yaw.csv, grouped into frames by timestamp_us
# --------------------------------------------------------------------------
def load_frames(csv_path):
    frames = OrderedDict()
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            frames.setdefault(int(row["timestamp_us"]), []).append(row)
    return list(frames.items())  # [(timestamp_us, [row, ...]), ...], insertion order


def build_object(row, ego_lat, ego_lon, heading_deg, mount, dropmo):
    x, y = float(row["x_m"]), float(row["y_m"])
    if mount == dropmo.MOUNT_REAR:
        x, y = -x, -y  # rear mount: sensor package faces the opposite way
    lat, lon = project_latlon(ego_lat, ego_lon, x, y, heading_deg)

    yaw_valid = row["yaw_valid"] == "1"
    yaw_deg = None
    if yaw_valid and row["yaw_rad"]:
        yaw_vehicle = dropmo.sensor_to_vehicle_yaw(float(row["yaw_rad"]), mount)
        # the bike's yaw in ENU (East = 0, counter-clockwise), not its compass heading
        bike_yaw_enu = math.radians(90.0 - heading_deg)
        yaw_enu = dropmo.vehicle_to_enu_yaw(yaw_vehicle, bike_yaw_enu)
        yaw_deg = round(dropmo.enu_yaw_to_heading_deg(yaw_enu), 2)
    else:
        yaw_valid = False

    radial_velocity = float(row["radial_velocity_mps"])
    speed_mps = float(row["speed_mps"])
    return {
        "object_id": int(row["object_id"]),
        "class": row["class_name"],
        "lat": lat, "lon": lon,
        "range_m": round(float(row["range_m"]), 2),
        "speed_kmh": round(float(row["speed_kmh"]), 2),
        "track_confidence": float(row["confidence"]),
        "class_confidence": float(row["class_confidence"]),
        "approaching": radial_velocity < -0.05,
        "static": speed_mps < 0.5,          # same rule DROPMO/tools/convert_session.py uses
        "yaw_valid": yaw_valid,
        "yaw_deg": yaw_deg,
    }


def build_frame(seq, rows, ego_lat, ego_lon, heading_deg, mount, dropmo):
    return {
        "seq": seq,
        "t_capture_us": int(time.time() * 1e6),   # stamped fresh at send time
        "ego": {
            "lat": ego_lat, "lon": ego_lon, "alt_m": 0.0,
            "heading_deg": heading_deg, "speed_mps": 0.0,
            "fix_ok": True, "simulated": True,
            "position_age_s": 0.0, "heading_age_s": 0.0,
        },
        "objects": [build_object(r, ego_lat, ego_lon, heading_deg, mount, dropmo)
                    for r in rows],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--csv", default=DEFAULT_CSV,
                    help="DROPMO objects_yaw.csv to replay")
    ap.add_argument("--dropmo-path", default=DROPMO_PATH_DEFAULT,
                    help="path to the DROPMO checkout (for dropmo.yaw helpers)")
    ap.add_argument("--ego-lat", type=float, default=45.0649195,
                    help="fixed bike position for the whole replay (lat)")
    ap.add_argument("--ego-lon", type=float, default=7.659724166666667,
                    help="fixed bike position for the whole replay (lon)")
    ap.add_argument("--heading-deg", type=float, default=0.0,
                    help="fixed bike heading, degrees from North clockwise -- "
                         "arbitrary, the bike was stationary for this session")
    ap.add_argument("--mount", choices=("front", "rear"), default="front",
                    help="radar mount used when this session was recorded "
                         "(2026-09-11_test was converted with --mount front)")
    ap.add_argument("--broker-host", default="127.0.0.1")
    ap.add_argument("--broker-port", type=int, default=30491,
                    help="broker's --listen-port (UDP)")
    ap.add_argument("--amf-host", default="0.0.0.0")
    ap.add_argument("--amf-port", type=int, default=18080)
    ap.add_argument("--metrics-host", default="0.0.0.0")
    ap.add_argument("--metrics-port", type=int, default=18081)
    ap.add_argument("--ue-id", type=int, default=1)
    ap.add_argument("--metrics-mode", choices=("constant", "random"), default="constant",
                    help="ignore the real network state: serve a constant "
                         "channel score, or a random one each poll")
    ap.add_argument("--metrics-score", type=float, default=8.0,
                    help="--metrics-mode constant: the score served")
    ap.add_argument("--metrics-random-min", type=float, default=3.0)
    ap.add_argument("--metrics-random-max", type=float, default=9.0)
    ap.add_argument("--speed", type=float, default=8.0,
                    help="playback speed multiplier vs. the recorded pace "
                         "(1.0 = real time, ~10-20 Hz depending on the profile)")
    ap.add_argument("--max-gap-s", type=float, default=0.3,
                    help="cap on the wait between two frames")
    ap.add_argument("--limit", type=int, default=5000,
                    help="max number of frames to replay, 0 = all")
    ap.add_argument("--no-mocks", action="store_true",
                    help="only replay UDP frames, don't start the AMF/metrics mocks")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    sys.path.insert(0, args.dropmo_path)
    try:
        import dropmo
    except ImportError:
        log.critical("can't import dropmo from %s -- check --dropmo-path", args.dropmo_path)
        raise SystemExit(2)
    mount = dropmo.MOUNT_FRONT if args.mount == "front" else dropmo.MOUNT_REAR

    frames = load_frames(args.csv)
    if not frames:
        log.critical("no frames found in %s", args.csv)
        raise SystemExit(2)
    if args.limit:
        frames = frames[:args.limit]
    n_objects = sum(len(rows) for _, rows in frames)
    n_yaw = sum(1 for _, rows in frames for r in rows if r["yaw_valid"] == "1")
    log.info("loaded %d frame(s), %d object(s), %d with valid yaw",
             len(frames), n_objects, n_yaw)
    log.info("anchoring the bike at %.7f,%.7f, heading %.1f deg (mount=%s) "
             "-- simulated position, not real GPS",
             args.ego_lat, args.ego_lon, args.heading_deg, args.mount)

    if not args.no_mocks:
        amf_srv = ThreadingHTTPServer((args.amf_host, args.amf_port),
                                       make_amf_handler(args.ue_id))
        threading.Thread(target=amf_srv.serve_forever, daemon=True).start()
        log.info("mock AMF listening on http://%s:%d (resolves every IMSI "
                 "to ranUeNgapID=%d)", args.amf_host, args.amf_port, args.ue_id)

        metrics_srv = ThreadingHTTPServer(
            (args.metrics_host, args.metrics_port),
            make_metrics_handler(args.metrics_mode, args.metrics_score,
                                 args.metrics_random_min, args.metrics_random_max))
        threading.Thread(target=metrics_srv.serve_forever, daemon=True).start()
        log.info("mock metrics listening on http://%s:%d (%s score%s)",
                 args.metrics_host, args.metrics_port, args.metrics_mode,
                 f"={args.metrics_score}" if args.metrics_mode == "constant" else "")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dst = (args.broker_host, args.broker_port)
    log.info("replaying %d frame(s) to udp://%s:%d at %.1fx speed "
             "(point broker.py's --listen-port here)",
             len(frames), dst[0], dst[1], args.speed)

    n_sent = 0
    prev_ts_us = None
    try:
        for seq, (ts_us, rows) in enumerate(frames):
            if prev_ts_us is not None:
                delay = max(0.0, (ts_us - prev_ts_us) / 1e6 / args.speed)
                delay = min(delay, args.max_gap_s)
                if delay > 0:
                    time.sleep(delay)
            prev_ts_us = ts_us

            frame = build_frame(seq, rows, args.ego_lat, args.ego_lon,
                                args.heading_deg, mount, dropmo)
            sock.sendto(json.dumps(frame).encode(), dst)
            n_sent += 1
            if n_sent % 500 == 0:
                log.info("sent %d/%d frames", n_sent, len(frames))
    except KeyboardInterrupt:
        pass
    finally:
        log.info("done: sent %d frame(s)", n_sent)


if __name__ == "__main__":
    main()
