#!/usr/bin/env python3
"""
    SIMULATED RIDE INTO PRODUCTION (test / demo)

Makes synthetic data flow through the deployed stack, with no real bike,
radar or 5G core:
  - bike + radar: a bike riding back and forth inside the current AoI (on
    the longest street/path in it, or a loop), with radar objects around it
    (parked cars, oncoming cars, pedestrians crossing, cars overtaking),
    sent as radar frames to the broker's UDP port;
  - 5G core: a fake AMF (IMSI -> ranUeNgapID) and a fake metrics server
    with a scripted channel score (good, with a coverage hole every minute).
    The broker only uses them if RESOLVER_URL/METRICS_URL point here: the
    script checks, and offers to switch them (restarting broker and
    escalator). Switch back from the dashboard (Settings -> 5G core, clear
    both fields) or by removing the two lines from .env.

Everything downstream is real: the LLM, the escalator, and the risk-events it
sends to the Risk Escalation Service for the devices really in the AoI.

The dashboard's Demo Mode runs this script with --control, and pauses and
resumes it from the page: while paused, the last frame is re-sent unchanged
and the simulated clock (the scripted channel's too) stands still.

Usage (from the repo root; Ctrl-C to stop):
    python3 tools/sim_ride.py
    python3 tools/sim_ride.py --yes --speed 4 --no-core
"""

import argparse
import json
import logging
import math
import os
import random
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "dashboard"))
import dashboard as settings                 # noqa: E402  (.env + compose defaults)

log = logging.getLogger("sim-ride")

M_PER_DEG = 111320.0
UE_ID = 7
BROKER = "guardtwin-broker"
AMF_PORT, METRICS_PORT = 18180, 18181


# --------------------------------------------------------------------------
# Route, in local east/north meters around the AoI center
# --------------------------------------------------------------------------
class Route:
    def __init__(self, pts_xy, lat0, lon0):
        self.lat0, self.lon0 = lat0, lon0
        self.kx = M_PER_DEG * math.cos(math.radians(lat0))
        self.pts = pts_xy
        self.cum = [0.0]
        for (ax, ay), (bx, by) in zip(pts_xy, pts_xy[1:]):
            self.cum.append(self.cum[-1] + math.hypot(bx - ax, by - ay))
        self.length = self.cum[-1]

    def at(self, s):
        """(x, y, heading_deg) at arc length s; beyond the ends the first/
        last segment is extended, so objects can come from afar."""
        i = 0 if s <= 0 else len(self.pts) - 2 if s >= self.length else \
            next(k for k in range(1, len(self.cum)) if self.cum[k] >= s) - 1
        (ax, ay), (bx, by) = self.pts[i], self.pts[i + 1]
        seg = max(1e-6, self.cum[i + 1] - self.cum[i])
        f = (s - self.cum[i]) / seg
        return ax + f * (bx - ax), ay + f * (by - ay), math.degrees(math.atan2(bx - ax, by - ay)) % 360

    def point(self, s, lateral, direction):
        """Point at arc length s, `lateral` m to the left of the direction of
        travel (+1: increasing s, -1: decreasing)."""
        x, y, h = self.at(s)
        h = math.radians(h if direction > 0 else h + 180)
        return x - lateral * math.cos(h), y + lateral * math.sin(h)

    def latlon(self, x, y):
        return self.lat0 + y / M_PER_DEG, self.lon0 + x / self.kx


def make_route(geometry_file, lat, lon, radius):
    """The longest street/path of the geometry inside the AoI; a loop around
    the center if nothing long enough fits (the broker drops frames from
    outside the AoI)."""
    kx = M_PER_DEG * math.cos(math.radians(lat))
    inside = radius - 3.0
    best, best_len, name = None, 0.0, None
    try:
        with open(geometry_file, encoding="utf-8") as f:
            doc = json.load(f)
        for feat in doc["features"]:
            g, p = feat.get("geometry") or {}, feat.get("properties") or {}
            if g.get("type") != "LineString" or not p.get("highway"):
                continue
            # longest run of consecutive vertices inside the AoI
            run = []
            for lo, la in g["coordinates"] + [[None, None]]:
                xy = None if lo is None else ((lo - lon) * kx, (la - lat) * M_PER_DEG)
                if xy is not None and math.hypot(*xy) <= inside:
                    run.append(xy)
                    continue
                length = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(run, run[1:]))
                if length > best_len:
                    best, best_len = run, length
                    name = p.get("name") or p.get("highway")
                run = []
    except (OSError, ValueError, KeyError) as e:
        log.warning("cannot read the geometry (%s): riding a loop", e)
    if best and best_len >= max(30.0, radius * 0.8):
        return Route(best, lat, lon), f"{name} ({best_len:.0f} m)"
    r = max(5.0, radius * 0.6)
    loop = [(r * math.sin(a), r * math.cos(a)) for a in
            [2 * math.pi * k / 36 for k in range(37)]]
    return Route(loop, lat, lon), f"a {r:.0f} m-radius loop around the AoI center"


# --------------------------------------------------------------------------
# Scenario: objects defined on the route (arc length + lateral offset)
# --------------------------------------------------------------------------
class Sim:
    def __init__(self, route, speed):
        self.route, self.speed = route, speed
        self.t = 0.0                                         # simulated time, s
        self.s, self.dir = 2.0, 1
        self.objects, self.prev_range, self.next_id = {}, {}, 1
        self.timers = {"oncoming": 4.0, "pedestrian": 10.0, "overtake": 30.0}
        for s in range(10, int(route.length), 30):           # parked cars
            self._add("CAR", s, -3.0, 0.0, 0.0, +1, static=True)

    def _add(self, cls, s, lat, v_s, v_lat, direction, static=False):
        self.objects[self.next_id] = {"cls": cls, "s": s, "lat": lat, "v_s": v_s,
                                      "v_lat": v_lat, "dir": direction, "static": static}
        self.next_id += 1

    def step(self, dt):
        self.t += dt
        self.s += self.dir * self.speed * dt
        if self.s >= self.route.length - 1 or self.s <= 1:
            self.dir *= -1                                   # turn around
            self.s = max(1.0, min(self.route.length - 1, self.s))
            self.objects = {k: o for k, o in self.objects.items() if o["static"]}
        for k in self.timers:
            self.timers[k] -= dt
        d = self.dir
        if self.timers["oncoming"] <= 0:
            self._add("CAR", self.s + d * 60, 3.5, -d * 9.0, 0.0, d)
            self.timers["oncoming"] = random.uniform(15, 25)
        if self.timers["pedestrian"] <= 0:
            self._add("PERSON", self.s + d * 20, -6.0, 0.0, 1.3, d)
            self.timers["pedestrian"] = random.uniform(25, 35)
        if self.timers["overtake"] <= 0:
            self._add("CAR", self.s - d * 30, 2.2, d * (self.speed + 4.0), 0.0, d)
            self.timers["overtake"] = random.uniform(50, 70)
        for k, o in list(self.objects.items()):
            if o["static"]:
                continue
            o["s"] += o["v_s"] * dt
            o["lat"] += o["v_lat"] * dt
            if abs(o["s"] - self.s) > 80 or abs(o["lat"]) > 8:
                del self.objects[k]

    def frame(self, seq, dt):
        r = self.route
        bx, by, h = r.at(self.s)
        heading = h if self.dir > 0 else (h + 180) % 360
        blat, blon = r.latlon(bx, by)
        objs = []
        for k, o in self.objects.items():
            x, y = r.point(o["s"], o["lat"], o["dir"])
            rng = math.hypot(x - bx, y - by)
            if rng > 40:                                     # radar range
                self.prev_range.pop(k, None)
                continue
            closing = (self.prev_range.get(k, rng) - rng) / dt
            self.prev_range[k] = rng
            approaching = closing > 0.2
            speed = abs(o["v_s"]) + abs(o["v_lat"])
            yaw = None
            if not o["static"] and speed > 0.3:
                _, _, rh = r.at(o["s"])
                if abs(o["v_s"]) > abs(o["v_lat"]):
                    yaw = rh if o["v_s"] > 0 else rh + 180
                else:                                        # crossing
                    fwd = rh if o["dir"] > 0 else rh + 180
                    yaw = fwd - 90 if o["v_lat"] > 0 else fwd + 90
                yaw %= 360
            lat, lon = r.latlon(x, y)
            objs.append({
                "object_id": k, "class": o["cls"], "lat": lat, "lon": lon,
                "range_m": round(rng, 2),
                "speed_kmh": round((closing if approaching else speed) * 3.6, 2),
                "track_confidence": 0.9, "class_confidence": 1.0,
                "approaching": approaching, "static": o["static"],
                "yaw_deg": round(yaw, 1) if yaw is not None else None,
                "yaw_valid": yaw is not None,
            })
        return {"seq": seq, "t_capture_us": int(time.time() * 1e6),
                "ego": {"lat": blat, "lon": blon, "alt_m": 240.0,
                        "heading_deg": round(heading, 1), "speed_mps": self.speed,
                        "fix_ok": True, "simulated": True,
                        "position_age_s": 0.0, "heading_age_s": 0.0},
                "objects": objs}


def channel_score(t, period=60.0, hole=15.0):
    """Good channel with a `hole` s coverage hole every `period` s. The noise
    depends on t only, so a paused ride keeps its score."""
    phase, start = t % period, period - hole
    if phase < start - 5:
        base = 8.5
    elif phase < start:
        base = 8.5 - (phase - (start - 5)) / 5 * 7.0         # 5 s ramp down
    else:
        base = 1.5
    return max(0.0, min(10.0, base + random.Random(round(t, 1)).uniform(-0.4, 0.4)))


# --------------------------------------------------------------------------
# Fake 5G core
# --------------------------------------------------------------------------
def json_handler(body_fn):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(body_fn()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    return Handler


def broker_gateway_ip():
    """This host's address on the broker's Docker network: the broker can
    reach services listening there, the LAN cannot. (Docker rejects traffic
    from one bridge network to another bridge's address, so e.g. the default
    bridge's 172.17.0.1 would not do.)"""
    try:
        out = subprocess.run(["docker", "inspect", BROKER, "--format",
                              "{{range .NetworkSettings.Networks}}{{.Gateway}} {{end}}"],
                             capture_output=True, text=True, timeout=10).stdout.split()
        socket.inet_aton(out[0])
        return out[0]
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


def fake_core_urls(amf_port=AMF_PORT, metrics_port=METRICS_PORT):
    """(AMF URL, metrics URL) of the fake core, or None if the broker's
    network cannot be found."""
    gw = broker_gateway_ip()
    if gw is None:
        return None
    return f"http://{gw}:{amf_port}", f"http://{gw}:{metrics_port}"


def broker_args():
    try:
        out = subprocess.run(["docker", "inspect", BROKER, "--format", "{{json .Args}}"],
                             capture_output=True, text=True, timeout=10).stdout
        args = json.loads(out)
        return {a: b for a, b in zip(args, args[1:]) if a.startswith("--")}
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def point_broker_at_fake_core(amf_url, metrics_url, assume_yes, compose_dir=ROOT):
    """Make the deployed broker use the fake AMF/metrics; True if it does."""
    args = broker_args()
    if args is None:
        log.error("container %s not found: is the stack up (docker compose up -d)?", BROKER)
        return False
    if args.get("--resolver-url") == amf_url and args.get("--metrics-url") == metrics_url:
        log.info("the broker already uses the fake AMF/metrics")
        return True
    print(f"\nThe broker uses AMF {args.get('--resolver-url')} and metrics "
          f"{args.get('--metrics-url')}.\nPoint it at this script's fake ones "
          f"(sets RESOLVER_URL/METRICS_URL in .env, restarts broker and escalator)?")
    if not assume_yes and input("[y/N] ").strip().lower() not in ("y", "yes"):
        log.info("left as it is: the channel score will come from the real core, if any")
        return False
    settings.write_env({"RESOLVER_URL": amf_url, "METRICS_URL": metrics_url})
    env = {k: v for k, v in os.environ.items() if k not in settings.SETTING_KEYS
           and k not in ("RESOLVER_URL", "METRICS_URL")}
    env["PWD"] = compose_dir
    rc = subprocess.run(["docker", "compose", "up", "-d", "broker", "escalator"],
                        cwd=compose_dir, env=env).returncode
    if rc != 0:
        log.error("docker compose failed (exit %d)", rc)
        return False
    log.info("broker restarted with the fake core; to go back to the real one, "
             "clear Settings -> 5G core in the dashboard (or remove RESOLVER_URL/"
             "METRICS_URL from .env) and apply")
    return True


def check_reachable_from_broker(amf_url):
    """The container -> host path can be blocked by a host firewall."""
    code = ("import urllib.request,sys;"
            f"urllib.request.urlopen('{amf_url}/api/v1/ue/check', timeout=3);print('ok')")
    for _ in range(10):                              # the broker may still be starting
        r = subprocess.run(["docker", "exec", BROKER, "python3", "-c", code],
                           capture_output=True, text=True)
        if r.stdout.strip() == "ok":
            log.info("the broker container reaches the fake AMF")
            return True
        time.sleep(1)
    log.warning("the broker container cannot reach %s (%s)", amf_url,
                (r.stderr.strip().splitlines() or ["?"])[-1])
    return False


def read_commands(paused):
    """--control: 'pause' / 'play' lines on stdin."""
    for line in sys.stdin:
        cmd = line.strip().lower()
        if cmd == "pause" and not paused.is_set():
            paused.set()
            log.info("paused: re-sending the last frame")
        elif cmd == "play" and paused.is_set():
            paused.clear()
            log.info("resumed")


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--speed", type=float, default=5.0, help="bike speed, m/s")
    ap.add_argument("--rate", type=float, default=10.0, help="radar frames per second")
    ap.add_argument("--duration-s", type=float, default=0.0, help="0: until Ctrl-C")
    ap.add_argument("--broker-host", default="127.0.0.1")
    ap.add_argument("--broker-port", type=int, default=None,
                    help="broker UDP port (default: the compose UDP_INGEST_PORT)")
    ap.add_argument("--no-core", action="store_true",
                    help="bike and radar only: no fake AMF/metrics")
    ap.add_argument("--amf-port", type=int, default=AMF_PORT)
    ap.add_argument("--metrics-port", type=int, default=METRICS_PORT)
    ap.add_argument("--yes", action="store_true",
                    help="switch the broker to the fake core without asking")
    ap.add_argument("--control", action="store_true",
                    help="read 'pause' / 'play' lines from stdin (the "
                         "dashboard's Demo Mode); implies --yes")
    ap.add_argument("--compose-dir", default=ROOT,
                    help="directory to run docker compose from (the "
                         "repository's host path, when run in a container)")
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()
    args.yes = args.yes or args.control
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    random.seed(args.seed)

    cfg = settings.effective_settings()
    lat, lon, radius = float(cfg["AOI_LAT"]), float(cfg["AOI_LON"]), float(cfg["AOI_RADIUS"])
    port = args.broker_port or int(cfg.get("UDP_INGEST_PORT") or 30491)
    geometry = cfg.get("GEOMETRY_FILE_HOST") or "./files/geometry.geojson"
    route, what = make_route(os.path.join(ROOT, geometry), lat, lon, radius)
    sim = Sim(route, args.speed)
    log.info("AoI %.6f, %.6f r %.0f m: riding %s at %.1f m/s -> udp://%s:%d",
             lat, lon, radius, what, args.speed, args.broker_host, port)
    log.warning("everything downstream is real: the escalator will send risk-events "
                "to %s for the devices really in the AoI",
                cfg.get("RISK_ESCALATION_URL") or "the Risk Escalation Service")

    clock = lambda: sim.t
    if not args.no_core:
        gw = broker_gateway_ip()
        if gw is None:
            log.error("cannot find the broker's Docker network (is the stack up?): "
                      "run with --no-core for bike and radar only")
            return 1
        for p, fn in ((args.amf_port, lambda: {"ranUeNgapID": UE_ID, "cmState": "connected"}),
                      (args.metrics_port, lambda: {
                          "Score": round(channel_score(clock()), 2),
                          "time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")})):
            httpd = ThreadingHTTPServer((gw, p), json_handler(fn))
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
        amf_url = f"http://{gw}:{args.amf_port}"
        metrics_url = f"http://{gw}:{args.metrics_port}"
        log.info("fake AMF on %s, fake metrics on %s", amf_url, metrics_url)
        if point_broker_at_fake_core(amf_url, metrics_url, args.yes, args.compose_dir):
            check_reachable_from_broker(amf_url)

    paused = threading.Event()
    if args.control:
        threading.Thread(target=read_commands, args=(paused,), daemon=True).start()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dt, seq, nxt, frame = 1.0 / args.rate, 0, time.monotonic(), None
    try:
        while not args.duration_s or clock() < args.duration_s:
            if paused.is_set() and frame is not None:
                # the same scene, still arriving: the broker keeps assessing it
                frame = dict(frame, seq=seq, t_capture_us=int(time.time() * 1e6))
            else:
                sim.step(dt)
                frame = sim.frame(seq, dt)
            sock.sendto(json.dumps(frame).encode(), (args.broker_host, port))
            seq += 1
            if seq % int(10 * args.rate) == 0 and not paused.is_set():
                log.info("t=%.0fs, %d frames sent, %d object(s) around, channel %.1f",
                         clock(), seq, len(sim.objects),
                         channel_score(clock()) if not args.no_core else float("nan"))
            nxt += dt
            time.sleep(max(0.0, nxt - time.monotonic()))
    except KeyboardInterrupt:
        pass
    log.info("stopped after %d frames", seq)


if __name__ == "__main__":
    sys.exit(main())
