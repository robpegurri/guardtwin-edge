#!/usr/bin/env python3
r"""SMI sensor-fusion broker.

Receives georeferenced radar frames (DROPMO geo bridge JSON) from a
"sentient bicycle" over UDP and turns each frame into an accident/hazard
risk grade 0-10 for the digital twin, factoring in the quality of the
5G channel of the monitored UEs (IMSI -> ranUeNgapID via the AMF API,
channel score via the metrics REST server).

The digital twin is a *client*: it connects to the broker's TCP port
(--serve-port) and receives the risk stream as NDJSON (one compact
JSON object per line). Multiple clients may be connected at once; a
UDP push mode (--dt-host/--dt-port) is also available.

Only the Python standard library is used.

Pipeline:
  radar (UDP --listen-port, JSON) --+
                                    +--> fused frame --> TCP clients (NDJSON)
  AMF  /api/v1/ue/{supi}  ----------+              \--> UDP push / jsonl / stdout
  metrics /ue_mac_stats/{gnb}/ran_ngap_ue_id/{id}/score
"""

import argparse
import json
import logging
import math
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

log = logging.getLogger("smi-broker")


# --------------------------------------------------------------------------
# Network state (AMF resolution + channel score) for a set of IMSIs,
# refreshed in background
# --------------------------------------------------------------------------
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
    """Polls AMF and metrics server for every configured IMSI.

    All reads happen through snapshot(), which returns a consistent list.
    """

    def __init__(self, args):
        self.args = args
        self._lock = threading.Lock()
        self._ues = [UEState(imsi) for imsi in args.imsi]
        self._stop = threading.Event()

    # -- AMF: IMSI -> ranUeNgapID ------------------------------------------
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

    # -- metrics: channel score for one resolved UE -------------------------
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

    # -- background loop ----------------------------------------------------
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


# --------------------------------------------------------------------------
# TCP server for digital twin clients (NDJSON stream, fan-out)
# --------------------------------------------------------------------------
class DTServer:
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
                         name="dt-accept", daemon=True).start()
        log.info("DT server listening on tcp://%s:%d", *self.addr)

    def _accept_loop(self, srv):
        while True:
            client, peer = srv.accept()
            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._lock:
                self._clients.append(client)
            log.info("DT client connected from %s:%d (%d total)",
                     peer[0], peer[1], len(self._clients))

    def broadcast(self, line_bytes):
        """Send one NDJSON line to every client; drop the dead/slow ones."""
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
            log.info("dropped %d DT client(s)", len(dead))

    @property
    def n_clients(self):
        with self._lock:
            return len(self._clients)


# --------------------------------------------------------------------------
# Static site geometry (buildings/roads) for the DT AI Environmental Risk
# feature, and the async LLM estimator itself.
# --------------------------------------------------------------------------
def _dist_m(lat1, lon1, lat2, lon2):
    """Flat-earth approximation, adequate at the ~100 m scale used here."""
    dlat = (lat2 - lat1) * 111320.0
    dlon = (lon2 - lon1) * 111320.0 * math.cos(math.radians((lat1 + lat2) / 2.0))
    return math.hypot(dlat, dlon)


def _bearing_deg(lat1, lon1, lat2, lon2):
    """Compass bearing from (lat1,lon1) to (lat2,lon2), degrees from North,
    clockwise. Same flat-earth approximation as _dist_m, inverted."""
    north = (lat2 - lat1) * 111320.0
    east = (lon2 - lon1) * 111320.0 * math.cos(math.radians((lat1 + lat2) / 2.0))
    return math.degrees(math.atan2(east, north)) % 360.0


def _feature_centroid(geom):
    """Simple average of every coordinate pair found in a GeoJSON geometry
    (point/line/polygon/multi-*) -- good enough for a "roughly here" tag,
    no shapely dependency needed."""
    coords = []

    def collect(c):
        if not c:
            return
        if isinstance(c[0], (int, float)):
            coords.append(c)
        else:
            for sub in c:
                collect(sub)

    collect(geom.get("coordinates"))
    if not coords:
        return None
    lon = sum(c[0] for c in coords) / len(coords)
    lat = sum(c[1] for c in coords) / len(coords)
    return [lat, lon]


def load_geometry(path, max_bytes):
    """Load a static GeoJSON FeatureCollection at startup, reduced to compact
    {"type", "name", "centroid"} dicts for cheap per-call bounding-box
    filtering later. Fails fast (sys.exit(2)) on any configuration problem:
    this is a deploy-time file, not a runtime condition to degrade
    gracefully from like an unreachable vLLM server is.

    Generate this file with fetch_geometry.py (a separate, offline tool:
    downloads OSM buildings/roads/amenities around a point via the
    Overpass API and writes it in this exact shape) -- broker.py itself
    no longer fetches geometry live at startup. That was tried and
    reverted: the public Overpass instance 504s often enough under load
    that doing it on every broker startup wasn't worth it for geometry
    that doesn't change at runtime anyway.
    """
    if path is None:
        log.info("env-risk: no --geometry-file given, running without geometry context")
        return []
    try:
        size = os.path.getsize(path)
    except OSError as e:
        log.critical("env-risk: cannot stat --geometry-file %s: %s", path, e)
        sys.exit(2)
    if size > max_bytes:
        log.critical("env-risk: --geometry-file %s is %d bytes, over --geometry-max-bytes %d",
                     path, size, max_bytes)
        sys.exit(2)
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log.critical("env-risk: cannot read/parse --geometry-file %s: %s", path, e)
        sys.exit(2)
    if (not isinstance(doc, dict) or doc.get("type") != "FeatureCollection"
            or not isinstance(doc.get("features"), list)):
        log.critical("env-risk: --geometry-file %s is not a GeoJSON FeatureCollection", path)
        sys.exit(2)

    out = []
    for feat in doc["features"]:
        geom = feat.get("geometry") if isinstance(feat, dict) else None
        if not isinstance(geom, dict):
            continue
        centroid = _feature_centroid(geom)
        if centroid is None:
            continue
        props = feat.get("properties") or {}
        if props.get("building"):
            tag = "building"
        elif props.get("highway"):
            tag = f"highway.{props['highway']}"
        elif props.get("amenity"):
            tag = str(props["amenity"])
        else:
            tag = geom.get("type", "feature")
        out.append({"type": tag, "name": props.get("name"), "centroid": centroid})
        if len(out) >= 5000:
            log.warning("env-risk: --geometry-file has more than 5000 usable "
                        "features, truncating")
            break
    log.info("env-risk: loaded %d geometry features from %s", len(out), path)
    return out


class EnvRiskEstimator:
    """Calls a local vLLM OpenAI-compatible endpoint back-to-back, as fast
    as it responds, to get a "DT AI Environmental Risk" contribution
    informed by static site geometry.

    Same pattern as NetworkState: a lock-protected scalar state (score,
    reasoning, monotonic timestamp) read via snapshot(); the background
    thread never blocks assess()/the UDP loop. The main loop feeds it the
    latest fused record via update_context() (O(1), no I/O) once per frame;
    the background thread reads whatever is current and immediately issues
    the next LLM call once the previous one returns -- LLM inference is far
    slower than the ~10 Hz radar rate, so there is no separate fixed period
    to tune: the blocking HTTP call itself paces the loop, and each call's
    wall-clock latency is logged so it's visible how long the prompt takes
    to solve. assess() only cares whether the *last* result is fresher than
    --llm-stale-s; a slow or hung LLM just makes results age out to 0, it
    never blocks radar/channel risk.

    Calls pause automatically once the fed context goes stale (no frame
    fused in the last --radar-idle-s, e.g. the radar/dropper node stopped
    sending) instead of continuing to re-query the LLM with a frozen
    context forever; they resume as soon as a fresh frame arrives.
    """

    def __init__(self, args, geometry):
        self.args = args
        self.geometry = geometry
        self._lock = threading.Lock()
        self._context = None
        self._context_mono = None
        self._score = None
        self._reasoning = None
        self._score_mono = None
        self._stop = threading.Event()

    def update_context(self, record):
        with self._lock:
            self._context = record
            self._context_mono = time.monotonic()

    def _nearby_geometry(self, pos):
        """Returns (features, n_omitted) closest to pos, or None if pos is
        unavailable (no GPS fix)."""
        if not pos or pos[0] is None:
            return None
        lat, lon = pos
        near = []
        for feat in self.geometry:
            d = _dist_m(lat, lon, feat["centroid"][0], feat["centroid"][1])
            if d <= ENV_RISK_GEOM_RADIUS_M:
                near.append((d, feat))
        near.sort(key=lambda x: x[0])
        omitted = max(0, len(near) - ENV_RISK_GEOM_MAX_FEATURES)
        return [f for _, f in near[:ENV_RISK_GEOM_MAX_FEATURES]], omitted

    def _build_prompt(self, context):
        ego = context.get("ego") or {}
        situation = {
            "ego": {"pos": ego.get("pos"), "speed_mps": ego.get("speed_mps")},
            "radar_risk": context.get("radar_risk"),
            "chan_penalty": context.get("chan_penalty"),
            "channel_state": (context.get("channel") or {}).get("state"),
            "n_obj": context.get("n_obj"),
            "threats": context.get("threats"),
        }
        nearby = self._nearby_geometry(ego.get("pos"))
        if nearby is None:
            geom_txt = "unavailable (no ego fix)"
        else:
            feats, omitted = nearby
            geom_txt = json.dumps(feats, separators=(",", ":"))
            if omitted:
                geom_txt += f"  ...{omitted} more features omitted"
        user_msg = (
            f"Situation:\n{json.dumps(situation, separators=(',', ':'))}\n\n"
            f"Nearby static geometry (within {ENV_RISK_GEOM_RADIUS_M:.0f} m):\n"
            f"{geom_txt}\n\n"
            "Respond with only the JSON object described in the system prompt."
        )
        return LLM_SYSTEM_PROMPT, user_msg

    def _call_llm(self, system_msg, user_msg):
        url = f"{self.args.llm_url}/v1/chat/completions"
        body = json.dumps({
            "model": self.args.llm_model,
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            "max_tokens": self.args.llm_max_tokens,
            "temperature": 0.2,
        }).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.args.llm_http_timeout) as r:
            doc = json.load(r)
        return doc["choices"][0]["message"]["content"]

    @staticmethod
    def _parse_response(text):
        """Best-effort parse -> (score 0-10, reason) or None. The model is
        instructed to reply with only a JSON object, but that is a prompt
        convention, not a guarantee (and vLLM's guided-JSON decoding was
        found to produce *worse*, malformed output for this deployment --
        see the vllm section in DATA_MODEL.md), so this must tolerate plain
        non-JSON text, code-fenced JSON, or JSON with chatter around it."""
        raw = text.strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.lower().startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        doc = None
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if m:
                try:
                    doc = json.loads(m.group(0))
                except json.JSONDecodeError:
                    doc = None

        if not isinstance(doc, dict) or "env_risk" not in doc:
            return None
        try:
            score = float(doc["env_risk"])
        except (TypeError, ValueError):
            return None
        if math.isnan(score) or math.isinf(score):
            return None
        reason = doc.get("reason")
        reason = reason[:200] if isinstance(reason, str) else ""
        return max(0.0, min(ENV_RISK_SCORE_MAX, score)), reason

    def run(self):
        idle = False
        while not self._stop.is_set():
            with self._lock:
                context = self._context
                context_mono = self._context_mono
            stale = (context_mono is not None and
                     time.monotonic() - context_mono > self.args.radar_idle_s)
            if context is None or stale:
                if not idle:
                    idle = True
                    log.info("env-risk: %s, pausing LLM calls until a fresh "
                             "radar frame arrives",
                             "no radar frame received yet" if context is None
                             else f"no radar frame in >{self.args.radar_idle_s:.0f}s")
                # nothing fresh to compute, not a rate limit -- just avoid a
                # tight spin until the next frame arrives
                self._stop.wait(0.2)
                continue
            if idle:
                idle = False
                log.info("env-risk: radar frames resumed, LLM calls resumed")
            t0 = time.monotonic()
            try:
                system_msg, user_msg = self._build_prompt(context)
                raw = self._call_llm(system_msg, user_msg)
                latency_s = time.monotonic() - t0
                parsed = self._parse_response(raw)
                if parsed is not None:
                    score, reasoning = parsed
                    with self._lock:
                        self._score = score
                        self._reasoning = reasoning
                        self._score_mono = time.monotonic()
                    log.info("env-risk: LLM call took %.2fs (score=%.1f)",
                             latency_s, score)
                else:
                    log.warning("env-risk: could not parse LLM response after "
                                "%.2fs: %r", latency_s, raw[:200])
            except Exception as e:
                # network error, timeout, malformed HTTP response, model
                # still loading, etc.: keep the last known score (it will
                # age out via --llm-stale-s), never crash the loop. Only
                # this failure path is throttled (--llm-retry-s), so a down
                # vLLM doesn't get hammered with a fast retry loop -- a
                # successful call loops back with no artificial delay.
                log.warning("env-risk: LLM call failed after %.2fs (%s)",
                            time.monotonic() - t0, e)
                self._stop.wait(self.args.llm_retry_s)

    def start(self):
        if self.args.llm_disable:
            log.info("env-risk: disabled (--llm-disable)")
            return
        threading.Thread(target=self.run, name="env-risk", daemon=True).start()

    def stop(self):
        self._stop.set()

    def snapshot(self):
        """Consistent {score, reasoning, age_s, state} read by assess()."""
        now = time.monotonic()
        with self._lock:
            if self._score_mono is None:
                return {"score": None, "reasoning": None, "age_s": None,
                        "state": "no_result"}
            age = now - self._score_mono
            state = "ok" if age <= self.args.llm_stale_s else "stale"
            return {"score": self._score, "reasoning": self._reasoning,
                    "age_s": round(age, 2), "state": state}


# --------------------------------------------------------------------------
# Risk model (see DATA_MODEL.md)
#
# The bicycle publishes radar/geo frames; the DT receives a single risk
# grade 0-10 and acts on it. Per obstacle:
#   - TTC factor: time-to-collision = range / closing speed (radar speed
#     is used as closing speed when the obstacle is approaching).
#     TTC <= 1.5 s -> 1.0, TTC >= 8 s -> 0.0, linear in between.
#   - proximity factor: 1.0 at 0 m, 0.0 at >= 30 m, linear.
#   - class weight: heavy vehicles threaten a cyclist more than pedestrians.
#   - track confidence scales everything; static obstacles are damped.
# Obstacle risks combine as a probabilistic OR (two cars at TTC 3 s are
# worse than one). The channel quality then *qualifies the reliability*
# of the radar data reaching the DT: a degraded link amplifies the risk
# the radar has actually detected (the DT may not be able to warn in
# time), but on its own it does not create risk — an empty scene stays
# at zero whatever the link state.
# --------------------------------------------------------------------------
CLASS_WEIGHT = {"TRUCK": 1.0, "CAR": 0.9, "SCOOTER": 0.7, "BICYCLE": 0.6,
                "PERSON": 0.5, "UNKNOWN": 0.7, "OTHER": 0.7}
TTC_MIN_S, TTC_MAX_S = 1.5, 8.0     # full risk .. no risk
PROX_MAX_M = 30.0                   # beyond this an obstacle is ignored
STATIC_DAMP = 0.5
CHANNEL_BOOST_MAX = 0.3             # radar risk amplification with dead link
YAW_BOOST_MAX = 0.15                # extra risk (0-1 object scale) for an object
                                     # whose confirmed direction of motion (DROPMO
                                     # v1.1 yaw) heads back towards the bike, even
                                     # if range isn't (yet) clearly decreasing --
                                     # catches a crossing trajectory that range-rate
                                     # alone under-weights. 0 when yaw isn't valid.
THREAT_FLOOR = 0.05                 # per-object risk below this is noise
LEVELS = [(1.0, "none"), (3.0, "low"), (5.0, "medium"),
          (7.5, "high"), (11.0, "critical")]

# -- DT AI Environmental Risk (async LLM factor, see EnvRiskEstimator) -----
# Independent additive cap: unlike chan_penalty, this can contribute risk
# even when radar_risk is 0 (e.g. an empty scene next to a known blind
# corner) -- but it is capped low enough that it alone can never swing the
# risk level from none to critical.
ENV_RISK_BOOST_MAX = 2.0          # absolute cap, risk points (0-10 scale)
ENV_RISK_SCORE_MAX = 10.0         # the LLM is asked for a 0-10 score
ENV_RISK_GEOM_RADIUS_M = 120.0    # geometry features sent to the LLM: bbox around ego
ENV_RISK_GEOM_MAX_FEATURES = 40   # cap on geometry features embedded per prompt
LLM_SYSTEM_PROMPT = (
    "You are a risk-assessment assistant embedded in a bicycle safety system. "
    "You receive the current radar-detected traffic situation around a cyclist "
    "and static site geometry (nearby buildings and roads). Identify environmental "
    "hazards the radar cannot see on its own: blind corners created by buildings, "
    "tight junctions, narrow passages between structures, or road layouts that "
    "increase collision risk given the cyclist's current position and heading. "
    "Respond with ONLY a single JSON object, no other text, in this exact shape: "
    '{"env_risk": <number 0-10>, "reason": "<short phrase, max 20 words>"}. '
    "env_risk: 0 = no added environmental hazard beyond what radar already "
    "captures, 10 = severe environmental hazard (e.g. blind corner immediately "
    "ahead with no sightline). Use the full range proportionally to severity. "
    "If you are uncertain or the input is insufficient, respond with "
    '{"env_risk": 0, "reason": "insufficient data"}.'
)


def _yaw_closing(o, ego_lat, ego_lon):
    """0-1: how much the object's confirmed direction of motion (yaw) heads
    back towards the bike, regardless of current range-rate. 0 if yaw isn't
    valid, position is missing, or the object is moving away/sideways."""
    if not o.get("yaw_valid"):
        return 0.0
    yaw_deg, lat, lon = o.get("yaw_deg"), o.get("lat"), o.get("lon")
    if yaw_deg is None or lat is None or lon is None or ego_lat is None or ego_lon is None:
        return 0.0
    bearing_obj_to_ego = (_bearing_deg(ego_lat, ego_lon, lat, lon) + 180.0) % 360.0
    delta = (yaw_deg - bearing_obj_to_ego + 180.0) % 360.0 - 180.0  # wrap to [-180,180]
    return max(0.0, math.cos(math.radians(delta)))


def object_risk(o, ego_lat=None, ego_lon=None):
    """Risk 0-1 contributed by one radar object, plus its TTC."""
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
    """Best available channel among the monitored UEs (the bike's link)."""
    rank = {"ok": 0, "stale": 1, "no_score": 2, "unresolved": 3}
    best = min(ues, key=lambda u: (rank.get(u["state"], 9),
                                   -(u["score"] or 0.0))) if ues else None
    return best


def assess(frame, ues, env, args):
    now_us = int(time.time() * 1e6)
    ego_in = frame.get("ego") or {}
    lat, lon = ego_in.get("lat"), ego_in.get("lon")

    # per-object risks
    threats = []
    no_risk_prod = 1.0
    objs = frame.get("objects") or []
    for o in objs:
        risk, ttc = object_risk(o, lat, lon)
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

    # channel contribution: the link quality qualifies the reliability of
    # the radar data at the DT, so it amplifies detected risk instead of
    # adding risk of its own (empty scene -> 0 whatever the link)
    chan = pick_channel(ues)
    if chan and chan["state"] in ("ok", "stale") and chan["score"] is not None:
        link_quality = chan["score"] / 10.0
    else:
        link_quality = 0.0
    chan_penalty = radar_risk * CHANNEL_BOOST_MAX * (1.0 - link_quality)

    # DT AI Environmental Risk: independent LLM-derived contribution, see
    # EnvRiskEstimator. Stale/missing results contribute 0 (async by design).
    if env and env["state"] == "ok" and env["score"] is not None:
        dt_ai_env_risk = min(ENV_RISK_BOOST_MAX,
                              env["score"] / ENV_RISK_SCORE_MAX * ENV_RISK_BOOST_MAX)
    else:
        dt_ai_env_risk = 0.0

    risk = min(10.0, radar_risk + chan_penalty + dt_ai_env_risk)
    level = next(name for thr, name in LEVELS if risk < thr)

    t_cap = frame.get("t_capture_us")
    return {
        "service": "smi.risk",
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
        "dt_ai_env_risk": round(dt_ai_env_risk, 1),
        "env": ({"score": env["score"], "reasoning": env["reasoning"],
                 "age_s": env["age_s"], "state": env["state"]}
                if env else None),
        "ego": {
            "pos": [lat, lon] if lat is not None else None,
            "speed_mps": ego_in.get("speed_mps"),
            "fix": bool(ego_in.get("fix_ok")),
            "sim": bool(ego_in.get("simulated")),
        },
        "n_obj": len(objs),
        "threats": threats[:3],
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
                    help="TCP port where digital twin clients connect to "
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
    ap.add_argument("--llm-url", default="http://localhost:8000",
                    help="base URL of the vLLM OpenAI-compatible server "
                         "(POSTs to {url}/v1/chat/completions)")
    ap.add_argument("--llm-model", default="Qwen/Qwen2.5-7B-Instruct",
                    help="model name sent in the chat completion request "
                         "(must match what vLLM was started with)")
    ap.add_argument("--llm-retry-s", type=float, default=1.0,
                    help="DT AI Environmental Risk LLM calls run back-to-back "
                         "as fast as vLLM answers (no fixed period -- each "
                         "call's latency is logged); this only throttles "
                         "retries after a *failed* call (e.g. vLLM still "
                         "loading), so a down server isn't hammered")
    ap.add_argument("--llm-stale-s", type=float, default=12.0,
                    help="LLM result older than this contributes 0 to the risk")
    ap.add_argument("--llm-http-timeout", type=float, default=20.0,
                    help="HTTP timeout for the (slow) LLM call")
    ap.add_argument("--llm-max-tokens", type=int, default=200,
                    help="max_tokens requested from the LLM")
    ap.add_argument("--geometry-file", default=None,
                    help="path to a static local GeoJSON file with buildings/"
                         "roads around the deployment site (generate one "
                         "with fetch_geometry.py), given as context to the "
                         "LLM; omit to run without geometry context")
    ap.add_argument("--geometry-max-bytes", type=int, default=20_000_000,
                    help="startup size guard on --geometry-file")
    ap.add_argument("--llm-disable", action="store_true",
                    help="disable the DT AI Environmental Risk feature entirely")
    ap.add_argument("--radar-idle-s", type=float, default=5.0,
                    help="pause DT AI Environmental Risk LLM calls if no radar "
                         "frame has been fused in this many seconds (resumes "
                         "automatically once frames arrive again)")
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
    ap.add_argument("--dt-host", default=None,
                    help="optional UDP push: send each fused frame as one "
                         "datagram to this host as well")
    ap.add_argument("--dt-port", type=int, default=30491)
    ap.add_argument("--jsonl-out", default=None,
                    help="also append fused frames to this JSON Lines file")
    ap.add_argument("--stdout", action="store_true",
                    help="print each fused frame to stdout")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    geometry = load_geometry(args.geometry_file, args.geometry_max_bytes)

    net_state = NetworkState(args)
    net_state.start()

    env_estimator = EnvRiskEstimator(args, geometry)
    env_estimator.start()
    log.info("env-risk: %s, geometry: %d feature(s) from %s",
             "disabled" if args.llm_disable else "enabled",
             len(geometry), args.geometry_file or "(none)")

    dt_server = DTServer(args.serve_host, args.serve_port)
    dt_server.start()

    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind((args.listen_host, args.listen_port))
    log.info("listening for user traffic on udp://%s:%d (IMSIs: %s)",
             args.listen_host, args.listen_port, ",".join(args.imsi))

    tx = None
    if args.dt_host:
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        log.info("also pushing fused frames to udp://%s:%d",
                 args.dt_host, args.dt_port)

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
            record = assess(frame, net_state.snapshot(), env_estimator.snapshot(), args)
            env_estimator.update_context(record)
            payload = json.dumps(record, separators=(",", ":"))
            dt_server.broadcast(payload.encode() + b"\n")
            if tx:
                tx.sendto(payload.encode(), (args.dt_host, args.dt_port))
            if out_file:
                out_file.write(payload + "\n")
            if args.stdout:
                print(payload, flush=True)
            n_rx += 1
            if n_rx % 100 == 0:
                log.info("assessed %d frames (last seq=%s, risk=%.1f/%s, "
                         "dt_clients=%d)", n_rx, record["seq"],
                         record["risk_score"], record["risk_level"],
                         dt_server.n_clients)
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
