#!/usr/bin/env python3
r"""Test harness for broker.py: replays recorded smi.risk field logs as if
they were arriving live from the real entities broker.py talks to.

The logs in /home/guardtwin/logs are *output* smi.risk records (pre-LLM
schema) captured from a real deployment -- not raw DROPMO radar frames.
This script reconstructs an approximate raw radar/geo frame from each
record's `ego` + `threats[]` (the only per-object data smi.risk carries:
class, range_m, ttc_s, approaching -- no raw speed_kmh/track_confidence/
static/object_id, so those are approximated) and replays it over UDP to
broker.py's --listen-port, exactly like the README's geo_run.jsonl replay
trick but from ~32k real records instead of 8 synthetic ones.

It also stands up two mock HTTP servers so broker.py's other dependencies
are satisfied without touching the real AMF/metrics infrastructure:
  - a mock AMF  (--amf-port)     : resolves any IMSI to a fixed ue_id
  - a mock metrics (--metrics-port): serves a channel score cycling
    through the *real* historical scores found in the logs, so the
    channel-quality contribution to the risk sees realistic variation
    instead of a constant.

Reconstruction is lossy by design (a quick end-to-end smoke test, not a
byte-exact replay) -- do not expect the newly computed risk_score to
match the historical value in the log; the point is to exercise the full
pipeline, including the new async LLM factor, against real field shapes.

Usage (from the repo root; run broker.py separately, pointed at this
script's mock servers):

    python3 tools/test_replay.py --amf-port 18080 --metrics-port 18081 &
    python3 broker.py --imsi 001010000167806 \
        --listen-port 30491 --serve-port 30500 \
        --resolver-url http://127.0.0.1:18080 \
        --metrics-url http://127.0.0.1:18081 --gnb-id f01 \
        --llm-url http://127.0.0.1:8000 --geometry-file files/geometry.geojson \
        --stdout -v
"""

import argparse
import glob
import json
import logging
import socket
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("test-replay")


# --------------------------------------------------------------------------
# Mock AMF: resolves any IMSI to a fixed ranUeNgapID
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


# --------------------------------------------------------------------------
# Mock metrics server: cycles through the real historical channel scores
# --------------------------------------------------------------------------
def make_metrics_handler(scores):
    state = {"i": 0, "lock": threading.Lock()}

    class MetricsHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            with state["lock"]:
                score = scores[state["i"] % len(scores)] if scores else 5.0
                state["i"] += 1
            body = json.dumps({
                "Score": score,
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
# Load recorded smi.risk records, oldest first, from one or more log files
# --------------------------------------------------------------------------
def load_records(pattern):
    records = []
    for fn in sorted(glob.glob(pattern)):
        with open(fn) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


# --------------------------------------------------------------------------
# Reconstruct an approximate raw DROPMO frame from a historical smi.risk
# record. Lossy: only the top-3 threats survive in the source data, and
# per-object speed/confidence/static are not recorded, so they are
# approximated.
# --------------------------------------------------------------------------
def reconstruct_frame(record):
    ego = record.get("ego") or {}
    pos = ego.get("pos")
    lat, lon = (pos[0], pos[1]) if pos else (None, None)

    objects = []
    for i, t in enumerate(record.get("threats") or []):
        range_m = t.get("range_m")
        ttc_s = t.get("ttc_s")
        appr = bool(t.get("appr"))
        speed_kmh = (range_m / ttc_s * 3.6) if (appr and ttc_s and ttc_s > 0
                                                  and range_m is not None) else 0.0
        opos = t.get("pos")
        objects.append({
            "object_id": i + 1,
            "class": t.get("cls") or "UNKNOWN",
            "lat": opos[0] if opos else None,
            "lon": opos[1] if opos else None,
            "range_m": range_m,
            "speed_kmh": round(speed_kmh, 2),
            "track_confidence": 0.9,   # not recorded in smi.risk output; assumed high
            "class_confidence": 1.0,
            "approaching": appr,
            "static": False,           # not recorded in smi.risk output
        })

    return {
        "seq": record.get("seq"),
        "t_capture_us": int(time.time() * 1e6),   # stamped fresh at send time
        "ego": {
            "lat": lat,
            "lon": lon,
            "alt_m": 0.0,
            "heading_deg": 0.0,
            "speed_mps": ego.get("speed_mps"),
            "fix_ok": bool(ego.get("fix")),
            "simulated": bool(ego.get("sim")),
            "position_age_s": 0.0,
            "heading_age_s": 0.0,
        },
        "objects": objects,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--logs", default="/home/guardtwin/logs/*.log",
                    help="glob pattern for the smi.risk log files to replay")
    ap.add_argument("--broker-host", default="127.0.0.1")
    ap.add_argument("--broker-port", type=int, default=30491,
                    help="broker's --listen-port (UDP)")
    ap.add_argument("--amf-host", default="0.0.0.0")
    ap.add_argument("--amf-port", type=int, default=18080)
    ap.add_argument("--metrics-host", default="0.0.0.0")
    ap.add_argument("--metrics-port", type=int, default=18081)
    ap.add_argument("--ue-id", type=int, default=1,
                    help="ranUeNgapID the mock AMF resolves every IMSI to")
    ap.add_argument("--speed", type=float, default=8.0,
                    help="playback speed multiplier vs. the recorded pace "
                         "(1.0 = real time, ~10 Hz)")
    ap.add_argument("--max-gap-s", type=float, default=0.3,
                    help="cap on the wait between two records (recorded "
                         "sessions have large real gaps between them; this "
                         "keeps replay moving instead of stalling)")
    ap.add_argument("--limit", type=int, default=5000,
                    help="max number of records to replay, 0 = all")
    ap.add_argument("--no-mocks", action="store_true",
                    help="only replay UDP frames, don't start the AMF/metrics mocks")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    records = load_records(args.logs)
    if not records:
        log.critical("no records found matching %s", args.logs)
        raise SystemExit(2)
    if args.limit:
        records = records[:args.limit]

    scores = [r["channel"]["score"] for r in records
              if r.get("channel") and r["channel"].get("score") is not None]
    level_hist = {}
    for r in records:
        level_hist[r["risk_level"]] = level_hist.get(r["risk_level"], 0) + 1
    log.info("loaded %d record(s), historical risk_level distribution: %s",
             len(records), level_hist)

    if not args.no_mocks:
        amf_srv = ThreadingHTTPServer((args.amf_host, args.amf_port),
                                       make_amf_handler(args.ue_id))
        threading.Thread(target=amf_srv.serve_forever, daemon=True).start()
        log.info("mock AMF listening on http://%s:%d (resolves every IMSI "
                 "to ranUeNgapID=%d)", args.amf_host, args.amf_port, args.ue_id)

        metrics_srv = ThreadingHTTPServer((args.metrics_host, args.metrics_port),
                                           make_metrics_handler(scores))
        threading.Thread(target=metrics_srv.serve_forever, daemon=True).start()
        log.info("mock metrics listening on http://%s:%d (cycling %d "
                 "historical channel score(s))",
                 args.metrics_host, args.metrics_port, len(scores))

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dst = (args.broker_host, args.broker_port)
    log.info("replaying %d frame(s) to udp://%s:%d at %.1fx speed "
             "(point broker.py's --listen-port here)",
             len(records), dst[0], dst[1], args.speed)

    n_sent = 0
    prev_ts_us = None
    try:
        for record in records:
            ts_us = record.get("ts_us")
            if prev_ts_us is not None and ts_us is not None:
                delay = max(0.0, (ts_us - prev_ts_us) / 1e6 / args.speed)
                delay = min(delay, args.max_gap_s)
                if delay > 0:
                    time.sleep(delay)
            prev_ts_us = ts_us

            frame = reconstruct_frame(record)
            sock.sendto(json.dumps(frame).encode(), dst)
            n_sent += 1
            if n_sent % 500 == 0:
                log.info("sent %d/%d frames (last historical risk=%.1f/%s)",
                         n_sent, len(records), record["risk_score"],
                         record["risk_level"])
    except KeyboardInterrupt:
        pass
    finally:
        log.info("done: sent %d frame(s)", n_sent)


if __name__ == "__main__":
    main()
