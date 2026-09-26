#!/usr/bin/env python3
"""
    REPLAY A RECORDING INTO THE STACK

Plays a broker recording (recorder.py: the dashboard's Record button)
back into the deployed stack, as it arrived on the field:
  - the radar datagrams, byte for byte, on their original timing;
  - the 5G core: a fake AMF and a fake metrics server answering what the
    real ones answered at that point of the ride (errors included);
  - the settings of the recorded run: AoI, score limits, the site geometry
    it used (the copy saved next to the recording, if the current one
    differs) and the LLM clock (fixed at the recording's start, to the
    minute: the LLM sees the date and time of the ride).
The LLM is the live one: with temperature 0 it answers the same prompt the
same way, but its calls fall at slightly different moments, so the AI
Context Score can differ a little. The recorded answers are in the file.

The settings are written to .env (asking first, unless --yes), broker and
escalator restarted, and everything is put back at the end (Ctrl-C too).
At the end the replayed output is compared with the recorded one, frame by
frame. Everything downstream is real, as with sim_ride.py: the escalator
sends risk-events for the devices really in the AoI.

Usage (from the repo root; Demo Mode must be off):
    python3 tools/replay_recording.py recordings/20260926T120835Z-broker.ndjson   # the <id> is the start, UTC
    python3 tools/replay_recording.py <file> --from-s 60 --to-s 300 --speed 2 --yes
"""

import argparse
import bisect
import hashlib
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import sim_ride                               # noqa: E402  (fake core helpers)
from sim_ride import settings                 # noqa: E402  (.env + compose defaults)

log = logging.getLogger("replay")

AMF_PORT, METRICS_PORT = 18182, 18183        # not sim_ride's: both may be around


# --------------------------------------------------------------------------
# The recording
# --------------------------------------------------------------------------
def load(path):
    session, by_kind = None, {}
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            try:
                ev = json.loads(line)
            except ValueError:
                log.warning("%s:%d: not JSON, skipped", path, n)
                continue
            if ev.get("kind") == "session":
                session = session or ev
            else:
                by_kind.setdefault(ev.get("kind"), []).append(ev)
    if session is None or session.get("component") != "broker":
        sys.exit(f"{path}: not a broker recording (no broker session line)")
    for evs in by_kind.values():
        evs.sort(key=lambda e: e["t"])
    return session, by_kind


def wanted_settings(session, args, amf_url, metrics_url):
    """The .env values that make the stack behave like the recorded run."""
    a = session["args"]
    want = {"AOI_LAT": a.get("aoi_lat"), "AOI_LON": a.get("aoi_lon"),
            "AOI_RADIUS": a.get("aoi_radius"),
            "CHAN_PENALTY_MAX": a.get("chan_penalty_max"),
            "AI_CONTEXT_MAX": a.get("ai_context_max"),
            "LLM_DISABLE": "1" if a.get("llm_disable") else "",
            "LLM_CLOCK": args.llm_clock or (session.get("llm_clock_now") or "")[:16]}
    if not args.no_core:
        want.update(RESOLVER_URL=amf_url, METRICS_URL=metrics_url)
    geo = session.get("geometry") or {}
    if geo.get("copy"):
        cur = settings.effective_settings().get("GEOMETRY_FILE_HOST") or "./files/geometry.geojson"
        cur_path = cur if os.path.isabs(cur) else os.path.join(ROOT, cur)
        try:
            with open(cur_path, "rb") as f:
                same = hashlib.sha256(f.read()).hexdigest() == geo.get("sha256")
        except OSError:
            same = False
        if not same:
            want["GEOMETRY_FILE_HOST"] = "./recordings/" + geo["copy"]
    return {k: ("" if v is None else str(v)) for k, v in want.items()}


# --------------------------------------------------------------------------
# Fake core: answers what the real one answered at this point of the ride
# --------------------------------------------------------------------------
class Clock:
    """Replay time -> recording time."""
    def __init__(self, t0_rec, speed):
        self.t0_rec, self.speed, self.m0 = t0_rec, speed, None

    def start(self):
        self.m0 = time.monotonic()

    def rec_time(self):
        return self.t0_rec if self.m0 is None else \
            self.t0_rec + (time.monotonic() - self.m0) * self.speed


def recorded_handler(events, clock):
    times = [e["t"] for e in events]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if not events:
                self.send_error(404)
                return
            i = max(0, bisect.bisect_right(times, clock.rec_time()) - 1)
            ev = events[i]
            if "error" in ev:                        # unreachable on the field
                self.close_connection = True
                return
            body = (ev.get("body") or "").encode()
            self.send_response(ev.get("status") or 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    return Handler


# --------------------------------------------------------------------------
# Settings switch, restored at the end
# --------------------------------------------------------------------------
def compose_up():
    env = {k: v for k, v in os.environ.items() if k not in settings.SETTING_KEYS
           and k not in ("RESOLVER_URL", "METRICS_URL", "GEOMETRY_FILE_HOST")}
    rc = subprocess.run(["docker", "compose", "up", "-d", "broker", "escalator"],
                        cwd=ROOT, env=env).returncode
    if rc != 0:
        log.error("docker compose failed (exit %d)", rc)
    return rc == 0


def same_value(a, b):
    """187 and 187.0 are the same setting."""
    try:
        return float(a) == float(b)
    except ValueError:
        return a == b


def switch_settings(want, assume_yes):
    """Write `want` to .env; returns the previous .env values to restore, or
    None if the user declined."""
    env, eff = settings.read_env(), settings.effective_settings()
    changes = {k: v for k, v in want.items() if not same_value(eff.get(k) or "", v)}
    if not changes:
        log.info("the stack already has the recorded settings")
        return {}
    print("\nThe replay needs these settings (written to .env, broker and escalator "
          "restarted, put back at the end):")
    for k, v in changes.items():
        print(f"  {k:20} {eff.get(k) or '(default)'}  ->  {v or '(default)'}")
    if not assume_yes and input("Go ahead? [y/N] ").strip().lower() not in ("y", "yes"):
        return None
    previous = {k: env.get(k, "") for k in changes}
    settings.write_env(changes)
    if not compose_up():
        settings.write_env(previous)
        return None
    return previous


def restore(previous):
    if not previous:
        return
    log.info("putting the settings back: %s", ", ".join(previous))
    settings.write_env(previous)
    compose_up()


# --------------------------------------------------------------------------
# Output tap: what the broker computes during the replay
# --------------------------------------------------------------------------
def tap(port, out, stop):
    while not stop.is_set():
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2) as s:
                s.settimeout(1)
                buf = b""
                while not stop.is_set():
                    try:
                        chunk = s.recv(65536)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    buf += chunk
                    *lines, buf = buf.split(b"\n")
                    for line in lines:
                        try:
                            rec = json.loads(line)
                            out[rec.get("seq")] = rec
                        except ValueError:
                            pass
        except OSError:
            time.sleep(0.5)


def broker_ready(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        return True
    except OSError:
        return False


def compare(recorded, replayed):
    pairs = [(r, replayed[r["seq"]]) for r in recorded if r.get("seq") in replayed]
    if not pairs:
        log.warning("no replayed frame to compare with the recording")
        return
    print(f"\nReplayed vs recorded output, {len(pairs)} of {len(recorded)} frames matched by seq:")
    print(f"  {'component':22} {'mean |diff|':>11} {'max |diff|':>10} {'within 0.1':>10}")
    for k, name in (("risk_score", "Risk score"), ("radar_risk", "Deterministic Score"),
                    ("chan_penalty", "Channel Penalty"), ("ai_env_risk", "AI Context Score")):
        d = [abs((a.get(k) or 0) - (b.get(k) or 0)) for a, b in pairs]
        print(f"  {name:22} {sum(d) / len(d):11.2f} {max(d):10.1f} "
              f"{100 * sum(x <= 0.1 for x in d) / len(d):9.0f}%")


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("recording", help="a recordings/<start>-broker.ndjson file")
    ap.add_argument("--speed", type=float, default=1.0, help="replay speed factor")
    ap.add_argument("--from-s", type=float, default=0.0,
                    help="start this many seconds into the recording")
    ap.add_argument("--to-s", type=float, default=None,
                    help="stop this many seconds into the recording")
    ap.add_argument("--llm-clock", default=None, metavar="YYYY-MM-DDTHH:MM",
                    help="the LLM clock to use (default: the recording's start)")
    ap.add_argument("--no-core", action="store_true",
                    help="leave the 5G core settings alone (no fake AMF/metrics)")
    ap.add_argument("--keep-settings", action="store_true",
                    help="do not put the settings back at the end")
    ap.add_argument("--yes", action="store_true", help="switch settings without asking")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    session, ev = load(args.recording)
    radar = ev.get("radar", [])
    if not radar:
        sys.exit("no radar frame in the recording")
    t0 = radar[0]["t"]
    lo, hi = t0 + args.from_s, (t0 + args.to_s) if args.to_s is not None else float("inf")
    frames = [e for e in radar if lo <= e["t"] <= hi]
    recorded_out = [e for e in ev.get("out", []) if lo <= e["t"] <= hi]
    span = frames[-1]["t"] - frames[0]["t"] if frames else 0
    log.info("%s: %d radar frames (%.0f s), %d AMF, %d metrics, %d LLM answers; "
             "replaying %d frames (%.0f s at x%g)", os.path.basename(args.recording),
             len(radar), radar[-1]["t"] - t0, len(ev.get("amf", [])),
             len(ev.get("metrics", [])), len(ev.get("llm", [])), len(frames),
             span, args.speed)
    if not frames:
        sys.exit("no radar frame in the chosen window")

    cfg = settings.effective_settings()
    if cfg.get("DEMO_MODE"):
        sys.exit("Demo Mode is on: turn it off first (its simulated frames would mix in)")
    udp_port = int(cfg.get("UDP_INGEST_PORT") or 30491)
    serve_port = int(cfg.get("ESCALATOR_SERVE_PORT") or 30500)

    clock = Clock(frames[0]["t"], args.speed)
    amf_url = metrics_url = None
    if not args.no_core:
        gw = sim_ride.broker_gateway_ip()
        if gw is None:
            sys.exit("cannot find the broker's Docker network: is the stack up?")
        for port, kind in ((AMF_PORT, "amf"), (METRICS_PORT, "metrics")):
            httpd = ThreadingHTTPServer((gw, port), recorded_handler(ev.get(kind, []), clock))
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
        amf_url, metrics_url = f"http://{gw}:{AMF_PORT}", f"http://{gw}:{METRICS_PORT}"
        log.info("fake AMF on %s, fake metrics on %s (recorded answers)", amf_url, metrics_url)

    previous = switch_settings(wanted_settings(session, args, amf_url, metrics_url), args.yes)
    if previous is None:
        log.info("left as it is: nothing replayed")
        return 1
    log.warning("everything downstream is real: the escalator will send risk-events "
                "for the devices really in the AoI")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    payload = lambda e: bytes.fromhex(e["data_hex"]) if "data_hex" in e else e["data"].encode()
    replayed, stop = {}, threading.Event()
    try:
        # The broker's startup takes the first frame to detect the bike, and
        # only then opens its stream: prime it with the first frame
        log.info("waiting for the broker (sending the first frame until it runs)")
        deadline = time.monotonic() + 120
        while not broker_ready(serve_port):
            if time.monotonic() > deadline:
                log.error("the broker did not start within 2 minutes")
                return 1
            sock.sendto(payload(frames[0]), ("127.0.0.1", udp_port))
            time.sleep(0.5)
        threading.Thread(target=tap, args=(serve_port, replayed, stop), daemon=True).start()
        time.sleep(1.0)

        clock.start()
        next_log = 10.0
        for n, e in enumerate(frames, 1):
            wait = clock.m0 + (e["t"] - frames[0]["t"]) / args.speed - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            sock.sendto(payload(e), ("127.0.0.1", udp_port))
            if e["t"] - frames[0]["t"] >= next_log:
                log.info("%.0f / %.0f s, %d frames sent", e["t"] - frames[0]["t"], span, n)
                next_log += 10.0
        log.info("all %d frames sent", len(frames))
        time.sleep(1.0)
    except KeyboardInterrupt:
        log.info("interrupted")
    finally:
        stop.set()
        compare(recorded_out, replayed)
        if not args.keep_settings:
            restore(previous)
    return 0


if __name__ == "__main__":
    sys.exit(main())
