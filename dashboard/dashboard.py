#!/usr/bin/env python3
"""
    GUARDTWIN DEBUG DASHBOARD

Host-side web page for debugging the edge stack at a glance:
  - map: AoI, bike (with trail and heading), radar objects, the hazards the
    LLM cites and the pose it was asked about;
  - live risk score and its 3 components (radar, AI environment, channel);
  - ENVELOPE devices-in-area: the current query result for the AoI;
  - settings (AoI and the main compose variables): written to .env, then
    `docker compose up -d broker escalator`;
  - the raw logs of broker, escalator and vLLM, together or one at a time;
  - Demo Mode (Settings): runs tools/sim_ride.py, with Play/Pause on the page.

Started by compose with the rest of the stack (the `dashboard` service, with
the docker socket and the repository mounted), or by hand on the host (it
needs the docker CLI). It listens on all interfaces, so it can be opened from
the local network at http://<server-ip>:8095 (--host 127.0.0.1 restricts it
to localhost again).

    python3 dashboard/dashboard.py [--host 0.0.0.0] [--port 8095]
"""

import argparse
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                 # the compose project (repo root)
sys.path.insert(0, ROOT)
import envelope_location as loc             # noqa: E402

log = logging.getLogger("dashboard")

COMPOSE_FILE = os.path.join(ROOT, "docker-compose.yml")
ENV_FILE = os.path.join(ROOT, ".env")
PAGE = os.path.join(HERE, "dashboard.html")
SIM_RIDE = os.path.join(ROOT, "tools", "sim_ride.py")

CONTAINERS = {"broker": "guardtwin-broker", "escalator": "guardtwin-escalator",
              "vllm": "guardtwin-vllm"}
APPLY_SERVICES = ["broker", "escalator"]

# (key, label, group, kind): the compose variables editable from the page
SETTINGS = [
    ("AOI_LAT", "Latitude", "aoi", "float"),
    ("AOI_LON", "Longitude", "aoi", "float"),
    ("AOI_RADIUS", "Radius (m)", "aoi", "float"),
    ("IMSI", "IMSI(s), comma-separated", "bike", "imsi"),
    ("RESOLVER_URL", "AMF URL (IMSI -> ranUeNgapID)", "core", "url"),
    ("METRICS_URL", "Metrics server URL (channel score)", "core", "url"),
    ("AI_CONTEXT_MAX", "AI Context Score: max points", "score", "float"),
    ("CHAN_PENALTY_MAX", "Channel Penalty: max points", "score", "float"),
    ("LLM_DISABLE", "Disable the AI environmental risk", "llm", "flag"),
    ("LLM_CLOCK", "Fixed LLM clock (empty: real time)", "llm", "clock"),
    ("RISK_ESCALATION_URL", "Risk Escalation URL", "escalation", "url"),
    ("AOI_ID", "AoI id", "escalation", "text"),
    ("EVALUATOR_BEARER_TOKEN", "Evaluator token", "escalation", "secret"),
    ("VERBOSE", "Verbose logging", "startup", "flag"),
    ("SKIP_DEVICE_DETECT", "Skip device detection (phase 2)", "startup", "flag"),
    ("NOTIFY_HOST", "ENVELOPE callback host", "startup", "text"),
    ("NOTIFY_PORT", "ENVELOPE callback port", "startup", "int"),
    ("DEVICES_MAX_AGE_S", "Devices max age (s)", "startup", "int"),
    ("DEMO_MODE", "Enable Demo Mode", "demo", "flag"),
]
RECORDINGS = os.path.join(ROOT, "recordings")      # the Record button (recorder.py)
# The 5G core the broker used before Demo Mode pointed it at the fake one,
# restored when Demo Mode is turned off ("default": the compose default)
CORE_KEYS = ("RESOLVER_URL", "METRICS_URL")

SETTING_KEYS = {k for k, *_ in SETTINGS}
# Log sequence numbers restart from 0 with each run: pages tell runs apart by this
BOOT_ID = secrets.token_hex(4)
# Set by the dashboard compose service: our own container, whose mounts tell
# where the repository is on the host
SELF_CONTAINER = os.environ.get("DASHBOARD_CONTAINER")
COMPOSE_DIR = ROOT       # where compose runs from; see host_root()

HISTORY_S = 300          # risk components kept for the chart
HISTORY_STEP_S = 0.5
TRAIL_POINTS = 600
LOG_LINES = 6000
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
M_PER_DEG = 111320.0


# --------------------------------------------------------------------------
# Settings: compose defaults, overridden by .env
# --------------------------------------------------------------------------
def compose_defaults():
    """{VAR: default} for every ${VAR:-default} in the compose file; flags
    (${VAR:+...}) default to ""."""
    with open(COMPOSE_FILE, encoding="utf-8") as f:
        text = f.read()
    out = {k: "" for k in re.findall(r"\$\{(\w+):\+", text)}
    for k, v in re.findall(r"\$\{(\w+):-([^}]*)\}", text):
        if not out.get(k):                   # first default wins
            out[k] = v
    return out


def read_env():
    values = {}
    try:
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    values[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return values


def effective_settings():
    values = compose_defaults()
    values.update(read_env())
    return values


def write_env(updates):
    """Sets/removes keys in .env, keeping every other line as it is. An
    empty value removes the key: compose then uses its default."""
    try:
        with open(ENV_FILE, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = ["# Written by dashboard.py (compose variables); edit freely"]
    out, done = [], set()
    for line in lines:
        k = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if k in updates:
            done.add(k)
            if updates[k] != "":
                out.append(f"{k}={updates[k]}")
            continue
        out.append(line)
    out += [f"{k}={v}" for k, v in updates.items() if k not in done and v != ""]
    # Keep the file's owner: in the compose service we run as root
    try:
        st = os.stat(ENV_FILE)
    except FileNotFoundError:
        st = os.stat(ROOT)
    tmp = ENV_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    try:
        os.chown(tmp, st.st_uid, st.st_gid)
    except OSError:
        pass
    os.replace(tmp, ENV_FILE)


def validate(values):
    """(clean {key: str}, errors {key: msg}) for the submitted settings."""
    clean, errors = {}, {}
    kinds = {k: kind for k, _, _, kind in SETTINGS}
    for k, v in values.items():
        if k not in SETTING_KEYS:
            errors[k] = "not an editable setting"
            continue
        v = "" if v is None else str(v).strip()
        if any(c in v for c in "\n\r\"'$`\\ "):
            errors[k] = "spaces, quotes, $ and backslashes are not allowed"
            continue
        kind = kinds[k]
        try:
            if kind == "flag":
                v = "1" if v.lower() in ("1", "true", "yes", "on") else ""
            elif v == "":
                pass                                 # back to the default
            elif kind == "float":
                x = float(v)
                if k == "AOI_LAT" and not -90 <= x <= 90:
                    raise ValueError("latitude out of range")
                if k == "AOI_LON" and not -180 <= x <= 180:
                    raise ValueError("longitude out of range")
                if k == "AOI_RADIUS" and not 1 <= x <= 5000:
                    raise ValueError("radius must be 1-5000 m")
                if k in ("AI_CONTEXT_MAX", "CHAN_PENALTY_MAX") and not 0 <= x <= 10:
                    raise ValueError("must be 0-10 points")
            elif kind == "int":
                if int(v) < 0:
                    raise ValueError("must be positive")
            elif kind == "imsi":
                if not re.fullmatch(r"\d{5,15}(,\d{5,15})*", v):
                    raise ValueError("comma-separated digits, e.g. 001010000167802")
            elif kind == "clock":
                datetime.strptime(v, "%Y-%m-%dT%H:%M")
            elif kind == "url":
                if not re.fullmatch(r"https?://[^\s]+", v):
                    raise ValueError("must be an http(s) URL")
        except ValueError as e:
            errors[k] = str(e) if str(e) and "could not convert" not in str(e) \
                and "does not match" not in str(e) else f"invalid {kind}"
            continue
        clean[k] = v
    return clean, errors


# --------------------------------------------------------------------------
# Shared state
# --------------------------------------------------------------------------
class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.record = None
        self.record_t = None
        self.rx_times = deque(maxlen=200)
        self.n_frames = 0
        self.tap = {"connected": False, "error": None}
        self.history = deque(maxlen=int(HISTORY_S / HISTORY_STEP_S))
        self.trail = deque(maxlen=TRAIL_POINTS)
        self.devices = {"t": None, "devices": [], "error": None}
        self.health = {"vllm": None, "envelope": None}
        self.containers = {}
        self.job = {"running": False, "rc": None, "cmd": None, "t": None}
        self.logs = deque(maxlen=LOG_LINES)
        self.log_seq = 0
        self.demo = {"enabled": False, "running": False, "paused": False}
        # the escalator's device list, rebuilt from its "devices" log lines
        self.tracker = {"known": False, "sub": None, "callbacks": 0, "zombies": 0,
                        "query_ok": True, "current": {}, "events": deque(maxlen=80),
                        "last_ts": ""}

    def add_log(self, src, ts, text):
        with self.lock:
            self.log_seq += 1
            self.logs.append((self.log_seq, src, ts, text))

    def on_record(self, rec):
        now = time.time()
        with self.lock:
            self.record, self.record_t = rec, now
            self.n_frames += 1
            self.rx_times.append(now)
            if not self.history or now - self.history[-1][0] >= HISTORY_STEP_S:
                self.history.append((round(now, 2), rec.get("radar_risk") or 0.0,
                                     rec.get("ai_env_risk") or 0.0,
                                     rec.get("chan_penalty") or 0.0,
                                     rec.get("risk_score") or 0.0))
            pos = (rec.get("ego") or {}).get("pos")
            if pos and pos[0] is not None:
                last = self.trail[-1] if self.trail else None
                if last is None or _dist_m(last, pos) > 1.0:
                    self.trail.append([round(pos[0], 7), round(pos[1], 7)])

    def snapshot(self):
        now = time.time()
        with self.lock:
            rate = sum(1 for t in self.rx_times if now - t <= 5.0) / 5.0
            return {
                "record": self.record,
                "record_age_s": round(now - self.record_t, 1) if self.record_t else None,
                "fps": rate, "n_frames": self.n_frames, "tap": dict(self.tap),
                "devices": dict(self.devices),
                "health": dict(self.health), "containers": dict(self.containers),
                "job": dict(self.job), "demo": dict(self.demo),
                "tracker": dict(self.tracker, current=list(self.tracker["current"].values()),
                                events=list(self.tracker["events"])[::-1]),
                "now": now,
            }


def _dist_m(a, b):
    return math.hypot((a[0] - b[0]) * M_PER_DEG,
                      (a[1] - b[1]) * M_PER_DEG * math.cos(math.radians(a[0])))


# --------------------------------------------------------------------------
# Background workers
# --------------------------------------------------------------------------
def broker_tap(state, port_override=None):
    """Second client of the broker's NDJSON stream (next to the escalator).
    Reads continuously: a stalled client would stall the broker's broadcast."""
    while True:
        port = port_override or int(effective_settings().get("ESCALATOR_SERVE_PORT") or 30500)
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
                s.settimeout(None)
                with state.lock:
                    state.tap = {"connected": True, "error": None}
                for line in s.makefile("r", encoding="utf-8", errors="replace"):
                    line = line.strip()
                    if line:
                        try:
                            state.on_record(json.loads(line))
                        except ValueError:
                            pass
            err = "connection closed"
        except OSError as e:
            err = str(e)
        with state.lock:
            state.tap = {"connected": False, "error": err}
        time.sleep(2)


def envelope_poller(state):
    """Devices-in-area for the current AoI, as the escalator sees it
    (read-only /queries), plus the service health."""
    n = 0
    while True:
        s = effective_settings()
        try:
            area = loc.area(float(s["AOI_LAT"]), float(s["AOI_LON"]),
                            float(s["AOI_RADIUS"]))
            max_age = int(s.get("DEVICES_MAX_AGE_S") or 60)
            r = requests.post(f"{loc.BASE}/queries", headers=loc.HEADERS,
                              json={"area": area, "maxAge": max_age}, timeout=5)
            r.raise_for_status()
            devs = r.json()
            with state.lock:
                state.devices = {"t": time.time(), "devices": devs, "error": None,
                                 "max_age_s": max_age}
        except Exception as e:
            with state.lock:
                state.devices = dict(state.devices, t=time.time(), error=str(e))
        if n % 3 == 0:
            ok = loc.is_alive()
            with state.lock:
                state.health["envelope"] = ok
        n += 1
        time.sleep(5)


def health_poller(state):
    while True:
        s = effective_settings()
        try:
            r = requests.get(f"http://127.0.0.1:{s.get('LLM_PORT') or 8000}/health",
                             timeout=3)
            vllm = r.status_code == 200
        except Exception:
            vllm = False
        try:
            out = subprocess.run(
                ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}\t{{.Status}}"],
                capture_output=True, text=True, timeout=10).stdout
            rows = dict((ln.split("\t")[0], ln.split("\t")[1:])
                        for ln in out.splitlines() if "\t" in ln)
            containers = {svc: ({"state": rows[name][0], "status": rows[name][1]}
                                if name in rows else {"state": "missing", "status": ""})
                          for svc, name in CONTAINERS.items()}
        except Exception as e:
            containers = {"error": str(e)}
        with state.lock:
            state.health["vllm"] = vllm
            state.containers = containers
        update_recording(state)
        time.sleep(5)


# --------------------------------------------------------------------------
# Recording: the Record button writes recordings/control.json, which broker
# and escalator follow (recorder.py)
# --------------------------------------------------------------------------
def read_control():
    try:
        with open(os.path.join(RECORDINGS, "control.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_control(doc):
    os.makedirs(RECORDINGS, exist_ok=True)
    path = os.path.join(RECORDINGS, "control.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f)
    try:                                     # the directory's owner, not root
        st = os.stat(RECORDINGS)
        os.chown(tmp, st.st_uid, st.st_gid)
    except OSError:
        pass
    os.replace(tmp, path)                    # the recorders never see half a file


def update_recording(state):
    ctl = read_control()
    rec = {"on": bool(ctl.get("on")), "id": ctl.get("id"), "since": ctl.get("since"),
           "bytes": 0, "writing": []}
    if rec["on"] and rec["id"] and os.path.isdir(RECORDINGS):
        for e in os.scandir(RECORDINGS):
            if e.name.startswith(rec["id"] + "-") and e.name.endswith(".ndjson"):
                rec["bytes"] += e.stat().st_size
                comp = e.name[len(rec["id"]) + 1:-len(".ndjson")].split("-r")[0]
                if comp not in rec["writing"]:
                    rec["writing"].append(comp)
    with state.lock:
        state.health["recording"] = rec


def set_recording(state, on):
    if on:
        now = time.time()
        write_control({"on": True, "id": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now)),
                       "since": now})
    else:
        write_control(dict(read_control(), on=False, stopped=time.time()))
    update_recording(state)


def clean_line(text):
    text = ANSI.sub("", text.rstrip("\n"))
    return text.split("\r")[-1]          # progress bars: keep the final state


def ts_key(ts):
    """A docker log timestamp, comparable as text: docker drops the trailing
    zeros of the nanoseconds, so "…35.93528Z" would sort after "…35.9352801Z"."""
    head, dot, frac = ts.rstrip("Z").partition(".")
    return f"{head}.{frac:0<9}" if dot else f"{head}.000000000"


DEVICES_LINE = re.compile(r"\sdevices (?:INFO|WARNING) (.*)$")


def on_devices_line(state, ts, msg):
    """Follow the escalator's device list (DeviceTracker in escalator.py)
    through its "devices" log lines; ts orders them (and skips repeats)."""
    m = DEVICES_LINE.search(msg)
    if not m:
        return
    text, tr = m.group(1), state.tracker
    with state.lock:
        if ts_key(ts) <= tr["last_ts"]:
            return
        tr["last_ts"] = ts_key(ts)
        if text == "tracking started":
            tr.update(known=True, sub=None, callbacks=0, zombies=0, query_ok=True, current={})
            tr["events"].append({"t": ts, "kind": "restart"})
        elif (e := re.fullmatch(r"(entered|left) (.+) \((callback|query)\)", text)):
            kind, addr, src = e.groups()
            if kind == "entered":
                tr["current"][addr] = {"addr": addr, "src": src, "since": ts}
            else:
                tr["current"].pop(addr, None)
            tr["events"].append({"t": ts, "kind": kind, "addr": addr, "src": src})
        elif (e := re.fullmatch(r"subscribed (\S+)", text)):
            tr["sub"] = e.group(1)
        elif text.startswith("callback: "):
            tr["callbacks"] += 1
        elif (e := re.match(r"zombie subscriptions: (\d+)", text)):
            tr["zombies"] = int(e.group(1))
        elif text.startswith("devices-in-area query failed"):
            tr["query_ok"] = False
        elif text.startswith("devices-in-area query works again"):
            tr["query_ok"] = True


def seed_devices(state, container):
    """The device list so far, from the escalator's whole log: the log panel
    only loads its last lines."""
    try:
        out = subprocess.run(["docker", "logs", "--timestamps", container],
                             capture_output=True, text=True, errors="replace", timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return
    for raw in sorted((out.stdout + out.stderr).splitlines()):   # by timestamp
        ts, _, msg = raw.partition(" ")
        if " devices " in msg:
            on_devices_line(state, ts, msg)


def log_follower(state, src, container):
    """`docker logs -f` one container, resuming after each restart where it
    left off (--since the last timestamp seen)."""
    last_ts, last_meta = None, None
    while True:
        cmd = ["docker", "logs", "-f", "--timestamps"]
        cmd += ["--since", last_ts] if last_ts else ["--tail", "400"]
        try:
            p = subprocess.Popen(cmd + [container], stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True,
                                 errors="replace", bufsize=1)
        except OSError as e:
            state.add_log(src, "", f"[dashboard] cannot run docker: {e}")
            time.sleep(10)
            continue
        for raw in p.stdout:
            ts, _, msg = raw.partition(" ")
            if not re.match(r"\d{4}-\d\d-\d\dT", ts):
                meta = clean_line(raw)
                if meta != last_meta:            # e.g. "No such container", once
                    state.add_log(src, "", f"[dashboard] {meta}")
                    last_meta = meta
                continue
            if last_ts and ts_key(ts) <= ts_key(last_ts):
                continue                         # overlap after a resume
            last_ts, last_meta = ts, None
            msg = clean_line(msg)
            state.add_log(src, ts, msg)
            if src == "escalator":
                on_devices_line(state, ts, msg)
        p.wait()
        time.sleep(2)


def host_root():
    """The host path of the repository. In the compose service it is mounted
    at ROOT, but compose must run from its host path: the relative paths in
    docker-compose.yml (bind mounts, build contexts) are sent to the docker
    daemon, which resolves them on the host. That path is made valid in here
    too, as a symlink to ROOT."""
    if not SELF_CONTAINER:
        return ROOT
    try:
        out = subprocess.run(["docker", "inspect", "--format", "{{json .Mounts}}",
                              SELF_CONTAINER], capture_output=True, text=True,
                             timeout=10)
        mounts = json.loads(out.stdout) if out.returncode == 0 else []
    except (OSError, ValueError, subprocess.TimeoutExpired):
        mounts = []
    src = next((m["Source"] for m in mounts if m.get("Destination") == ROOT), None)
    if not src:
        log.warning("cannot find the host path of %s: Settings may not apply", ROOT)
        return ROOT
    try:
        if not os.path.lexists(src):
            os.makedirs(os.path.dirname(src), exist_ok=True)
            os.symlink(ROOT, src)
    except OSError as e:
        log.warning("cannot link %s to %s (%s): Settings may not apply", src, ROOT, e)
        return ROOT
    return src


def run_compose(state, rebuild):
    cmd = ["docker", "compose", "up", "-d"] + (["--build"] if rebuild else []) + APPLY_SERVICES
    # .env must win: drop these variables from our own environment, which
    # compose would otherwise prefer over .env
    env = {k: v for k, v in os.environ.items() if k not in SETTING_KEYS}
    env["PWD"] = COMPOSE_DIR
    with state.lock:
        state.job = {"running": True, "rc": None, "cmd": " ".join(cmd), "t": time.time()}
    state.add_log("compose", datetime.now().isoformat(), "$ " + " ".join(cmd))
    try:
        p = subprocess.Popen(cmd, cwd=COMPOSE_DIR, env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, errors="replace",
                             bufsize=1)
        for line in p.stdout:
            state.add_log("compose", datetime.now().isoformat(), clean_line(line))
        rc = p.wait()
    except OSError as e:
        state.add_log("compose", datetime.now().isoformat(), f"[dashboard] {e}")
        rc = -1
    state.add_log("compose", datetime.now().isoformat(), f"[dashboard] exit code {rc}")
    with state.lock:
        state.job = dict(state.job, running=False, rc=rc)


# --------------------------------------------------------------------------
# Demo Mode: tools/sim_ride.py, while DEMO_MODE is set in .env
# --------------------------------------------------------------------------
def demo_core_env(on):
    """.env changes for turning Demo Mode on (the broker uses sim_ride.py's
    fake AMF/metrics; the current ones are saved) or off (they come back)."""
    env = read_env()
    if not on:
        out = {}
        for k in CORE_KEYS:
            prev = env.get("DEMO_PREV_" + k)
            if prev is not None:
                out[k] = "" if prev == "default" else prev
                out["DEMO_PREV_" + k] = ""
        return out
    out = {"DEMO_PREV_" + k: env.get(k) or "default" for k in CORE_KEYS}
    sys.path.insert(0, os.path.dirname(SIM_RIDE))
    import sim_ride
    urls = sim_ride.fake_core_urls()
    if urls:                     # else sim_ride.py switches them once it runs
        out.update(zip(CORE_KEYS, urls))
    return out


class Demo:
    """Keeps sim_ride.py running while Demo Mode is on (and no compose run is
    in progress), stops it when it is turned off, restarts it when the
    settings it rides by change (e.g. the AoI moved); relays Play/Pause."""
    RETRY_S = 10.0
    # read by sim_ride.py once, at startup
    RIDE_KEYS = ("AOI_LAT", "AOI_LON", "AOI_RADIUS", "GEOMETRY_FILE_HOST", "UDP_INGEST_PORT")

    def __init__(self, state):
        self.state, self.proc, self.t_start, self.paused = state, None, 0.0, False
        self.ride_cfg = None
        self.lock = threading.Lock()

    def run(self):
        while True:
            cfg = effective_settings()
            on = bool(cfg.get("DEMO_MODE"))
            ride_cfg = {k: cfg.get(k) for k in self.RIDE_KEYS}
            with self.state.lock:
                busy = self.state.job["running"]
            with self.lock:
                alive = self.proc is not None and self.proc.poll() is None
                if on and alive and not busy and ride_cfg != self.ride_cfg:
                    # after the compose run: the broker already has the new AoI
                    self.state.add_log("demo", datetime.now().isoformat(),
                                       "[dashboard] ride settings changed, restarting sim_ride.py")
                    self._stop()
                    alive = False
                    self.t_start = 0.0
                if on and not alive and not busy and time.time() - self.t_start > self.RETRY_S:
                    self._start(ride_cfg)
                elif not on and alive:
                    self._stop()
                alive = self.proc is not None and self.proc.poll() is None
                with self.state.lock:
                    self.state.demo = {"enabled": on, "running": alive,
                                       "paused": alive and self.paused}
            time.sleep(1)

    def _stop(self):
        self.proc.send_signal(signal.SIGINT)
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def _start(self, ride_cfg):
        self.ride_cfg = ride_cfg
        cmd = [sys.executable, "-u", SIM_RIDE, "--control", "--compose-dir", COMPOSE_DIR]
        self.state.add_log("demo", datetime.now().isoformat(), "$ " + " ".join(cmd))
        self.t_start, self.paused = time.time(), False
        try:
            self.proc = subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         text=True, errors="replace", bufsize=1)
        except OSError as e:
            self.state.add_log("demo", datetime.now().isoformat(), f"[dashboard] {e}")
            self.proc = None
            return
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def _pump(self, proc):
        for line in proc.stdout:
            self.state.add_log("demo", datetime.now().isoformat(), clean_line(line))
        self.state.add_log("demo", datetime.now().isoformat(),
                           f"[dashboard] sim_ride.py exited ({proc.wait()})")

    def command(self, action):
        """'play' or 'pause'; False if no ride is running."""
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                return False
            try:
                self.proc.stdin.write(action + "\n")
                self.proc.stdin.flush()
            except OSError:
                return False
            self.paused = action == "pause"
            with self.state.lock:
                self.state.demo = dict(self.state.demo, paused=self.paused)
            return True


def geometry_info():
    """Path and bounding box [[s, w], [n, e]] of the geometry the broker uses."""
    # relative paths in compose are relative to the compose file
    path = effective_settings().get("GEOMETRY_FILE_HOST") or "./files/geometry.geojson"
    path = os.path.join(ROOT, path) if not os.path.isabs(path) else path
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return path, None
    lats, lons = [], []

    def walk(c):
        if c and isinstance(c[0], (int, float)):
            lons.append(c[0])
            lats.append(c[1])
        elif c:
            for x in c:
                walk(x)
    for feat in doc.get("features", []):
        walk((feat.get("geometry") or {}).get("coordinates"))
    return path, ([[min(lats), min(lons)], [max(lats), max(lons)]] if lats else None)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def make_handler(state, allowed_hosts, demo):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            log.debug("%s " + fmt, self.address_string(), *args)

        def _host_ok(self):
            # Against DNS rebinding (Host: evil.example): accept IP addresses
            # and this machine's own names only
            host = (self.headers.get("Host") or "").strip()
            host = host[1:host.find("]")] if host.startswith("[") else host.rsplit(":", 1)[0]
            try:
                ipaddress.ip_address(host)
                return True
            except ValueError:
                return host.lower() in allowed_hosts

        def _send(self, code, body, ctype="application/json"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, {"error": "unknown Host; open the dashboard "
                                                 "by IP or add --allow-host"})
            url = urlparse(self.path)
            q = parse_qs(url.query)
            if url.path == "/":
                with open(PAGE, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if url.path == "/geometry.geojson":
                path, _ = geometry_info()
                try:
                    with open(path, "rb") as f:
                        return self._send(200, f.read(), "application/geo+json")
                except OSError:
                    return self._send(404, {"error": "no geometry file"})
            if url.path == "/api/settings":
                return self._send(200, self._settings())
            if url.path == "/api/logs":
                return self._send(200, self._logs(q))
            if url.path == "/api/stream":
                return self._stream()
            self._send(404, {"error": "not found"})

        def do_POST(self):
            # Custom header: a cross-site page cannot send it without a CORS
            # preflight, which this server never grants
            if not self._host_ok() or self.headers.get("X-Guardtwin") != "1":
                return self._send(403, {"error": "forbidden"})
            path = urlparse(self.path).path
            if path not in ("/api/apply", "/api/demo", "/api/record"):
                return self._send(404, {"error": "not found"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                doc = json.loads(self.rfile.read(n))
            except ValueError:
                return self._send(400, {"error": "bad JSON"})
            if path == "/api/record":
                if doc.get("action") not in ("start", "stop"):
                    return self._send(400, {"error": "action: start or stop"})
                try:
                    set_recording(state, doc["action"] == "start")
                except OSError as e:
                    return self._send(500, {"error": f"cannot write recordings/control.json: {e}"})
                with state.lock:
                    return self._send(200, {"ok": True, "recording": state.health["recording"]})
            if path == "/api/demo":
                if doc.get("action") not in ("play", "pause"):
                    return self._send(400, {"error": "action: play or pause"})
                if not demo.command(doc["action"]):
                    return self._send(409, {"error": "the demo ride is not running"})
                return self._send(200, {"ok": True})
            with state.lock:
                busy = state.job["running"]
            if busy:
                return self._send(409, {"error": "a compose run is already in progress"})
            clean, errors = validate(doc.get("values") or {})
            if errors:
                return self._send(400, {"errors": errors})
            if "DEMO_MODE" in clean and bool(clean["DEMO_MODE"]) != \
                    bool(effective_settings().get("DEMO_MODE")):
                clean.update(demo_core_env(bool(clean["DEMO_MODE"])))
            write_env(clean)
            threading.Thread(target=run_compose, args=(state, bool(doc.get("rebuild"))),
                             daemon=True).start()
            self._send(200, {"ok": True, "settings": self._settings()})

        def _settings(self):
            eff, defaults, env = effective_settings(), compose_defaults(), read_env()
            path, bbox = geometry_info()
            return {"fields": [{"key": k, "label": lab, "group": g, "kind": kind,
                                "value": eff.get(k, ""), "default": defaults.get(k, ""),
                                "overridden": k in env}
                               for k, lab, g, kind in SETTINGS],
                    "geometry": {"path": os.path.relpath(path, ROOT), "bbox": bbox}}

        def _logs(self, q):
            after = int((q.get("after") or ["-1"])[0])
            srcs = set(((q.get("src") or [""])[0]).split(",")) - {""}
            limit = min(int((q.get("limit") or ["400"])[0]), 2000)
            with state.lock:
                rows = [r for r in state.logs
                        if r[0] > after and (not srcs or r[1] in srcs)]
                seq = state.log_seq
            return {"boot": BOOT_ID, "seq": seq, "lines": rows[-limit:]}

        def _stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                with state.lock:
                    hello = {"history": list(state.history), "trail": list(state.trail)}
                self._event("hello", hello)
                while True:
                    self._event("state", state.snapshot())
                    time.sleep(0.25)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _event(self, name, data):
            self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
            self.wfile.flush()

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--host", default="0.0.0.0",
                    help="address to listen on (default: all interfaces; "
                         "127.0.0.1 for localhost only)")
    ap.add_argument("--port", type=int, default=8095)
    ap.add_argument("--allow-host", action="append", default=[],
                    help="extra host name the page may be opened by (it "
                         "always accepts IP addresses, localhost and this "
                         "machine's name); repeatable")
    ap.add_argument("--broker-port", type=int, default=None,
                    help="broker stream to show (default: the compose "
                         "ESCALATOR_SERVE_PORT), e.g. a local test broker")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("envelope_location").setLevel(logging.WARNING)

    global COMPOSE_DIR
    COMPOSE_DIR = host_root()
    log.info("compose runs from %s", COMPOSE_DIR)
    state = State()
    workers = [(broker_tap, (args.broker_port,)), (envelope_poller, ()), (health_poller, ())]
    seed_devices(state, CONTAINERS["escalator"])
    workers += [(log_follower, (src, name)) for src, name in CONTAINERS.items()]
    for fn, extra in workers:
        threading.Thread(target=fn, args=(state, *extra), daemon=True).start()
    demo = Demo(state)
    threading.Thread(target=demo.run, daemon=True).start()

    allowed = {"localhost", socket.gethostname().lower(), socket.getfqdn().lower()}
    allowed |= {h.lower() for h in args.allow_host}
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(state, allowed, demo))
    httpd.daemon_threads = True
    log.info("dashboard on http://%s:%d", args.host, args.port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
