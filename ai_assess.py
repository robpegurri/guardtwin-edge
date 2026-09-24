"""
    AI ENVIRONMENTAL RISK

Everything the broker needs from the LLM lives here: static site geometry,
the egocentric prompt, the vLLM call and the scoring of its answer.

Split of work:
  - the LLM *detects* named hazards in the map around the cyclist (blind
    corners, junctions, crossings, ...). It never sees the radar, which
    broker.py already scores: the two contributions do not overlap;
  - this module *scores* them (fixed weight per severity and per where the
    cited map feature is, noisy-OR like the radar objects), so every point
    of ai_env_risk traces back to a hazard with a type, the map feature it
    comes from, its direction and a short piece of evidence.

broker.py only uses: add_arguments(), load_geometry(), EnvRiskEstimator.
"""

import json
import logging
import math
import os
import re
import sys
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger("broker.env-risk")


# --------------------------------------------------------------------------
# Tunables
# --------------------------------------------------------------------------
# Contribution to the 0-10 risk score. Additive and independent of the radar
# (an empty scene next to a blind corner still gets some risk), but capped
# low enough that it alone can never move the level past "low".
ENV_RISK_BOOST_MAX = 2.0
SEVERITY = {"low": 0.2, "medium": 0.45, "high": 0.75}   # per hazard, 0-1
# ...times where the feature the hazard cites is (see describe_surroundings)
GROUP_WEIGHT = {"path": 1.0, "beside": 0.6, "elsewhere": 0.2}
# ...times how busy the model expects that place to be at this date and time
ACTIVITY = {"normal": 1.0, "quiet": 0.5, "busy": 1.4}   # "normal" first: the neutral default
# Hazards combine conservatively: one per cited feature (the strongest), the
# worst counting fully and the others at this weight, as a noisy-OR.
SECONDARY_WEIGHT = 0.5
MAX_HAZARDS = 3
EVIDENCE_MAX_CHARS = 60      # hard cap in the answer format: tokens = latency

# A call takes a while (~1.5 s for Gemma-4-12B QAT on an L4): the model is
# asked about the pose the bike will have when the answer lands, projected
# along the heading by speed x a running average of the latency. The result
# then holds around that pose, until the bike has moved or turned too much.
LATENCY_INIT_S = 2.0
MAX_DRIFT_M = 20.0
MAX_TURN_DEG = 45.0
# ...and there is no point asking again for (almost) the same pose.
SAME_POSE_M = 3.0
SAME_HEADING_DEG = 10.0
# ...unless it is getting old: the time of day is part of the context too.
TIME_REFRESH_S = 300.0

# What the prompt shows (see describe_surroundings).
GEOM_RADIUS_M = 80.0
HORIZON_S = 6.0              # "the next few seconds" the model reasons about
PATH_MIN_M = 25.0            # the stretch ahead: max(this, speed * HORIZON_S)
PATH_HALF_WIDTH_M = 5.0      # a point/area this close to the line is on the path
BESIDE_M = 15.0              # lateral reach of "beside your path"
GEOM_BEHIND_MAX_M = 15.0     # features behind the bike only matter this close
ON_ROAD_M = 8.0              # nearest parallel way within this = the one ridden
ELSEWHERE_MAX = 8            # cap on the (least relevant) "elsewhere" lines
MAX_GEOMETRY_FEATURES = 5000


# --------------------------------------------------------------------------
# Prompt and answer schema
# --------------------------------------------------------------------------
HAZARD_TYPES = ["blind_corner", "junction", "crossing", "narrow_passage",
                "fast_traffic", "conflict_zone", "poor_surface"]

SYSTEM_PROMPT = """\
You assess the surroundings of a cyclist for a bicycle safety system.
You get the local date and time, the cyclist's heading and speed, and the
mapped features around them, numbered, each with its distance and direction
relative to where the cyclist is going.
A radar tracks the vehicles and people actually there: do not guess about them.
Report the hazards that the layout of the place creates for the next few
seconds of riding, at this time of day.

Use the date and time. The same place can be harmless at one hour and a hazard
at another: judge how busy each place usually is right now (schools at entry
and exit times on school days, universities during term, bus stops at rush
hour, shops, cafes and bars in their usual hours, everything quieter at night,
on Sundays and on holidays) and whether it is dark (unlit roads, corners that
are even harder to see). When the time matters, say so in the evidence.

Hazard types:
- blind_corner: a building close to the cyclist's path right next to a junction
  or crossing ahead, hiding whoever comes out of it
- junction: another road meets or crosses the cyclist's path ahead
- crossing: a pedestrian or cycle crossing on the cyclist's path
- narrow_passage: the path squeezes between buildings, steps or a narrow way
- fast_traffic: riding on or entering a road with a high speed limit or many lanes
- conflict_zone: a place where people or vehicles join the road (school, bus
  stop, car park entrance, shops)
- poor_surface: tram tracks, cobbles/sett, gravel, unlit road in the dark

The features come in three groups, already sorted out for you:
- "On your path": the road being ridden and what the cyclist will ride through.
  Hazards come from here.
- "Beside your path": what flanks that stretch. It matters when it makes a path
  feature worse, e.g. a building right next to a junction or crossing on the
  path hides it: that is a blind_corner.
- "Elsewhere": not on the path. Report it only if clearly relevant, as "low".

Answer with one line per hazard, at most 3, most severe first, each from a
different feature, or the single word "none" if the path is plain:
<type> <feature number> <activity> <evidence> | <severity>
- type: one of the hazard types above.
- feature number: the listed feature the hazard comes from (for a blind_corner:
  the junction or crossing it hides).
- activity: how busy that place usually is at this date and time: normal, quiet
  or busy. Think of who is around then (pupils, students, commuters,
  customers) and whether it is dark.
- evidence: why, in at most 8 words, naming features in words.
- severity: high = on the path within the first half of the stretch, with poor
  visibility or fast traffic; medium = on the path; low = mild, or not on the
  path.
A road or crossing on your path is usually worth reporting.
Example: junction 3 busy side street hidden by corner building | high
"""

# The answer format, enforced by vLLM while decoding: compact lines rather than
# JSON, because output tokens are what the latency is made of (a JSON object
# per hazard costs ~37 tokens, a line ~11). Evidence comes before severity, so
# the model grounds itself before grading.
_LINE = r"(%s) ([1-9][0-9]?) (%s) ([^|\n]{3,%d}) \| (%s)" % (
    "|".join(HAZARD_TYPES), "|".join(ACTIVITY), EVIDENCE_MAX_CHARS, "|".join(SEVERITY))
RESPONSE_REGEX = r"none\n|(%s\n){1,%d}" % (_LINE, MAX_HAZARDS)
_LINE_RE = re.compile(_LINE)


def add_arguments(ap):
    g = ap.add_argument_group("AI environmental risk")
    g.add_argument("--llm-url", default="http://localhost:8000",
                   help="base URL of the vLLM OpenAI-compatible server "
                        "(POSTs to {url}/v1/chat/completions)")
    g.add_argument("--llm-model", default="google/gemma-4-12B-it-qat-w4a16-ct",
                   help="model name sent in the chat completion request "
                        "(must match what vLLM was started with)")
    g.add_argument("--llm-retry-s", type=float, default=1.0,
                   help="LLM calls run back-to-back as fast as vLLM answers, "
                        "whenever the bike has moved; this only throttles "
                        "retries after a *failed* call (e.g. vLLM still "
                        "loading), so a down server isn't hammered")
    g.add_argument("--llm-stale-s", type=float, default=12.0,
                   help="safety net: a result not refreshed for this long "
                        "contributes 0 (a stationary bike keeps refreshing it)")
    g.add_argument("--llm-http-timeout", type=float, default=20.0,
                   help="HTTP timeout for the (slow) LLM call")
    g.add_argument("--llm-max-tokens", type=int, default=100,
                   help="max_tokens requested from the LLM")
    g.add_argument("--geometry-file", default=None,
                   help="path to a static local GeoJSON file with buildings/"
                        "roads around the deployment site (generate one with "
                        "tools/fetch_geometry.py); without it the feature is off")
    g.add_argument("--geometry-max-bytes", type=int, default=20_000_000,
                   help="startup size guard on --geometry-file")
    g.add_argument("--ai-context-max", type=float, default=ENV_RISK_BOOST_MAX,
                   help="most points the AI Context Score adds to the risk")
    g.add_argument("--llm-disable", action="store_true",
                   help="disable the AI Environmental Risk feature entirely")
    g.add_argument("--radar-idle-s", type=float, default=5.0,
                   help="pause LLM calls if no radar frame has been fused in "
                        "this many seconds (resumes automatically)")
    g.add_argument("--site-tz", default="Europe/Rome",
                   help="time zone of the deployment site: the LLM is told the "
                        "local date and time there (containers usually run "
                        "in UTC)")
    g.add_argument("--llm-clock", default=None, metavar="YYYY-MM-DDTHH:MM",
                   help="tell the LLM this fixed local time instead of the "
                        "real one, e.g. to replay a ride as if at 07:55 on a "
                        "school day (tests, evaluation)")


def site_clock(args):
    """Callable returning the local datetime at the site. Exits on a bad
    --site-tz/--llm-clock: a silently wrong hour would mislead the LLM."""
    try:
        tz = ZoneInfo(args.site_tz)
    except (ZoneInfoNotFoundError, ValueError) as e:
        log.critical("unknown --site-tz %r (%s)", args.site_tz, e)
        sys.exit(2)
    if args.llm_clock is None:
        return lambda: datetime.now(tz)
    try:
        fixed = datetime.strptime(args.llm_clock.replace(" ", "T"),
                                  "%Y-%m-%dT%H:%M").replace(tzinfo=tz)
    except ValueError:
        log.critical("--llm-clock %r is not YYYY-MM-DDTHH:MM", args.llm_clock)
        sys.exit(2)
    log.warning("--llm-clock: the LLM is told it is always %s", fixed)
    return lambda: fixed


# --------------------------------------------------------------------------
# Geometry: OSM features -> short descriptions + shapes in local meters
# --------------------------------------------------------------------------
ROADS = {"motorway": "motorway", "trunk": "trunk road",
         "primary": "primary road", "secondary": "secondary road",
         "tertiary": "tertiary road", "unclassified": "minor road",
         "residential": "residential street", "living_street": "living street",
         "service": "service road"}
PATHS = {"cycleway": "cycleway", "footway": "footway",
         "pedestrian": "pedestrian area", "path": "path", "steps": "steps",
         "track": "track"}
ROAD_NODES = {"crossing": "pedestrian crossing",
              "traffic_signals": "traffic lights", "stop": "stop sign",
              "give_way": "give-way sign", "bus_stop": "bus stop",
              "mini_roundabout": "mini roundabout"}
AMENITIES = {"school", "kindergarten", "college", "university", "hospital",
             "clinic", "bus_station", "parking", "parking_entrance", "fuel",
             "marketplace", "cafe", "restaurant", "bar", "pub", "fast_food",
             "place_of_worship", "theatre", "cinema"}
ROUGH_SURFACES = {"sett", "cobblestone", "unhewn_cobblestone", "gravel",
                  "fine_gravel", "dirt", "ground", "grass", "sand", "metal",
                  "wood", "compacted", "unpaved"}
CYCLE_INFRA = {"lane": "cycle lane", "track": "cycle track",
               "shared_lane": "shared cycle lane",
               "share_busway": "shared bus lane"}

_M_PER_DEG = 111320.0


def _way_details(p):
    out = []
    if p.get("maxspeed"):
        out.append(f"limit {p['maxspeed']}")
    if p.get("lanes"):
        out.append(f"{p['lanes']} lane" + ("" if p["lanes"] == "1" else "s"))
    if p.get("oneway") in ("yes", "-1"):
        out.append("one-way")
    if p.get("junction") == "roundabout":
        out.append("roundabout")
    for key in ("cycleway", "cycleway:both", "cycleway:right", "cycleway:left"):
        if p.get(key) in CYCLE_INFRA:
            out.append(CYCLE_INFRA[p[key]])
            break
    if p.get("surface") in ROUGH_SURFACES:
        out.append(f"{p['surface'].replace('_', ' ')} surface")
    if p.get("lit") == "no":
        out.append("unlit")
    if p.get("tunnel") == "yes":
        out.append("tunnel")
    return out


def describe(props):
    """(name, details, linear) for an OSM feature worth showing the LLM,
    else None. linear: roads, paths and tracks, for which we also report
    whether they cross the cyclist's path."""
    hw = props.get("highway")
    details = []
    if hw in ROADS or hw in PATHS:
        linear = True
        if hw == "footway" and props.get("footway") == "crossing":
            label = "pedestrian crossing"
            if props.get("crossing"):
                details.append(props["crossing"].replace("_", " "))
        else:
            label = ROADS.get(hw) or PATHS[hw]
            details += _way_details(props)
    elif props.get("railway") == "tram":
        label, linear = "tram tracks", True
    elif hw in ROAD_NODES:
        label, linear = ROAD_NODES[hw], False
        if hw == "crossing" and props.get("crossing"):
            details.append(props["crossing"].replace("_", " "))
    elif props.get("building"):
        label, linear = "building", False
        levels = props.get("building:levels")
        if levels:
            label = f"{levels}-storey building"
    elif props.get("amenity") in AMENITIES:
        label, linear = props["amenity"].replace("_", " "), False
    else:
        return None
    if props.get("name"):
        label += f' "{props["name"]}"'
    return label, ", ".join(details), linear


def load_geometry(path, max_bytes):
    """
    Loads a static GeoJSON FeatureCollection at startup (see
    tools/fetch_geometry.py), keeps the features describe() cares about and
    projects their shapes to local east/north meters, so that per-call
    distances are plain 2-D geometry. Returns a dict, or None when there
    is no geometry (the feature is then off).
    """
    if path is None:
        log.info("no --geometry-file given, AI environmental risk is off")
        return None
    try:
        size = os.path.getsize(path)
    except OSError as e:
        log.critical("cannot stat --geometry-file %s: %s", path, e)
        sys.exit(2)
    if size > max_bytes:
        log.critical("--geometry-file %s is %d bytes, over --geometry-max-bytes %d",
                     path, size, max_bytes)
        sys.exit(2)
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log.critical("cannot read/parse --geometry-file %s: %s", path, e)
        sys.exit(2)
    if (not isinstance(doc, dict) or doc.get("type") != "FeatureCollection"
            or not isinstance(doc.get("features"), list)):
        log.critical("--geometry-file %s is not a GeoJSON FeatureCollection", path)
        sys.exit(2)

    # Every shape as a list of parts, each a list of [lon, lat] vertices
    raw = []
    for feat in doc["features"]:
        geom = feat.get("geometry") if isinstance(feat, dict) else None
        if not isinstance(geom, dict):
            continue
        d = describe(feat.get("properties") or {})
        if d is None:
            continue
        c = geom.get("coordinates")
        parts = {"Point": lambda: [[c]],
                 "LineString": lambda: [c],
                 "MultiPoint": lambda: [[p] for p in c],
                 "Polygon": lambda: c,
                 "MultiLineString": lambda: c,
                 "MultiPolygon": lambda: [r for poly in c for r in poly],
                 }.get(geom.get("type"), lambda: [])()
        parts = [p for p in parts if p]
        if parts:
            # rideable: a road or path the bike can be riding along
            rideable = d[2] and bool((feat.get("properties") or {}).get("highway"))
            raw.append((d, rideable, parts))
        if len(raw) >= MAX_GEOMETRY_FEATURES:
            log.warning("--geometry-file has more than %d usable features, "
                        "truncating", MAX_GEOMETRY_FEATURES)
            break
    if not raw:
        log.warning("--geometry-file %s has no usable features, AI "
                    "environmental risk is off", path)
        return None

    firsts = [parts[0][0] for _, _, parts in raw]
    lat0 = sum(p[1] for p in firsts) / len(firsts)
    lon0 = sum(p[0] for p in firsts) / len(firsts)
    geo = {"lat0": lat0, "lon0": lon0,
           "kx": _M_PER_DEG * math.cos(math.radians(lat0)), "features": []}
    for (name, details, linear), rideable, parts in raw:
        geo["features"].append({
            "name": name, "details": details, "linear": linear,
            "rideable": rideable,
            "parts": [[_to_local(geo, lat, lon) for lon, lat in part]
                      for part in parts],
        })
    log.info("loaded %d relevant geometry features from %s (of %d)",
             len(raw), path, len(doc["features"]))
    return geo


def _to_local(geo, lat, lon):
    return ((lon - geo["lon0"]) * geo["kx"], (lat - geo["lat0"]) * _M_PER_DEG)


def _to_latlon(geo, x, y):
    return [round(geo["lat0"] + y / _M_PER_DEG, 7), round(geo["lon0"] + x / geo["kx"], 7)]


def _nearest(parts, x, y):
    """(distance, nearest point) from (x, y) to a shape given as parts of
    vertices."""
    best = (math.inf, None)
    for part in parts:
        segs = zip(part, part[1:]) if len(part) > 1 else [(part[0], part[0])]
        for (ax, ay), (bx, by) in segs:
            dx, dy = bx - ax, by - ay
            l2 = dx * dx + dy * dy
            t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / l2))
            qx, qy = ax + t * dx, ay + t * dy
            d = math.hypot(x - qx, y - qy)
            if d < best[0]:
                best = (d, (qx, qy))
    return best


def _compass(dx, dy):
    return math.degrees(math.atan2(dx, dy)) % 360.0


def _angle_diff(a, b):
    return abs((a - b + 180.0) % 360.0 - 180.0)


REL_SECTORS = ["ahead", "ahead-right", "right", "behind-right",
               "behind", "behind-left", "left", "ahead-left"]
ABS_SECTORS = ["north", "north-east", "east", "south-east",
               "south", "south-west", "west", "north-west"]


def _sector(bearing, heading):
    if heading is None:
        return ABS_SECTORS[int(((bearing + 22.5) % 360.0) // 45.0)]
    return REL_SECTORS[int(((bearing - heading + 22.5) % 360.0) // 45.0)]


def _crossing_ahead(parts, x, y, heading):
    """Distance along the direction of travel at which a line crosses it,
    or None. A straight-ahead approximation, fine within GEOM_RADIUS_M."""
    ux, uy = math.sin(math.radians(heading)), math.cos(math.radians(heading))
    best = None
    for part in parts:
        for (ax, ay), (bx, by) in zip(part, part[1:]):
            # Solve (x, y) + t*u = a + s*(b - a), t >= 0, 0 <= s <= 1
            dx, dy = bx - ax, by - ay
            den = dx * uy - dy * ux
            if abs(den) < 1e-9:
                continue                    # parallel to the direction of travel
            t = (dx * (ay - y) - dy * (ax - x)) / den
            s_ = (ux * (ay - y) - uy * (ax - x)) / den
            # t < 2 m: the way right under the bike, not one ahead of it
            if t >= 2.0 and 0.0 <= s_ <= 1.0 and (best is None or t < best):
                best = t
    return best


def describe_surroundings(geo, lat, lon, heading, reach):
    """
    Egocentric view of the map at a pose: (riding_on, items), items being
    {"text", "group", "direction", "pos"} dicts, nearest first within each
    group (pos: [lat, lon] of the point meant, e.g. where a road crosses).
    Positions are those of the *nearest point* of each shape (a building's
    wall, not its centroid). The code does the geometry so that the model
    only has to judge what the features mean for a cyclist (a 7B model is
    poor at spatial filtering):
      path       what the bike will ride through in the next `reach` meters
      beside     what flanks that stretch (e.g. what hides a side street)
      elsewhere  the rest within GEOM_RADIUS_M
    Without a heading everything is "elsewhere", with compass directions.
    """
    x, y = _to_local(geo, lat, lon)
    if heading is not None:
        ux, uy = math.sin(math.radians(heading)), math.cos(math.radians(heading))
    near = []
    for f in geo["features"]:
        d, q = _nearest(f["parts"], x, y)
        if d > GEOM_RADIUS_M:
            continue
        rx, ry = q[0] - x, q[1] - y
        sector = _sector(_compass(rx, ry), heading) if d > 0.5 else "here"
        cross = (_crossing_ahead(f["parts"], x, y, heading)
                 if f["linear"] and heading is not None else None)
        if heading is None:
            near.append((d, d, f, "elsewhere", f"{d:.0f} m {sector}", sector, sector, q))
            continue
        along = rx * ux + ry * uy
        side = rx * uy - ry * ux                 # > 0: to the right
        if cross is not None and cross <= reach:
            near.append((cross, d, f, "path", f"{cross:.0f} m ahead", None, "ahead",
                         (x + cross * ux, y + cross * uy)))
        elif (not f["linear"] and 0.0 <= along <= reach
                and abs(side) <= PATH_HALF_WIDTH_M):
            near.append((along, d, f, "path", f"{along:.0f} m ahead", None, "ahead", q))
        elif 0.0 <= along <= reach and abs(side) <= BESIDE_M:
            lr = "right" if side > 0 else "left"
            where = f"{along:.0f} m ahead, {abs(side):.0f} m {lr}"
            # one per kind, side and 10 m of stretch
            near.append((d, d, f, "beside", where, (lr, int(along // 10)),
                         sector if sector != "here" else lr, q))
        else:
            if sector.startswith("behind") and d > GEOM_BEHIND_MAX_M:
                continue
            near.append((d, d, f, "elsewhere", f"{d:.0f} m {sector}", sector, sector, q))
    near.sort(key=lambda n: n[0])

    # The way being ridden: the nearest road/path, if the bike is on it, or
    # very close and not cutting across the direction of travel (tram tracks
    # along the road are never "ridden", or the choice would flip-flop)
    riding_on = next((f for _, d, f, group, *_ in near if f["rideable"] and (
        d < 1.0 or (d <= ON_ROAD_M and group != "path"))), None)

    items, seen, n_elsewhere = [], set(), 0
    for key_d, _, f, group, where, bucket, direction, pt in near:
        if f is riding_on or (riding_on and f["name"] == riding_on["name"]
                              and group != "path"):
            continue
        # OSM splits one street into many ways and a block into many
        # buildings: keep the nearest per group and position bucket
        kind = "building" if f["name"].endswith("building") else f["name"]
        key = (kind, group, bucket if group != "path" else int(key_d // 5))
        if key in seen:
            continue
        seen.add(key)
        if group == "elsewhere":
            n_elsewhere += 1
            if n_elsewhere > ELSEWHERE_MAX:
                continue
        text = f"{where}: {f['name']}"
        if f["details"]:
            text += f" ({f['details']})"
        if group == "path" and f["linear"]:
            text += ", crosses your path"
        items.append({"text": text, "group": group, "direction": direction,
                      "pos": _to_latlon(geo, *pt)})
    return riding_on, items


def build_prompt(geo, pose, now):
    """(system, user, items): items[n - 1] is the feature numbered n in the
    prompt, which is what the model cites. now: local datetime at the site."""
    lat, lon, heading, speed = pose["lat"], pose["lon"], pose["heading"], pose["speed"]
    reach = max(PATH_MIN_M, (speed or 0.0) * HORIZON_S)
    riding_on, items = describe_surroundings(geo, lat, lon, heading, reach)
    if heading is None:
        head = "heading unknown (directions below are compass directions)"
    else:
        head = (f"heading {heading:.0f}° "
                f"({ABS_SECTORS[int(((heading + 22.5) % 360.0) // 45.0)]})")
    if speed is not None:
        head += f", {speed:.1f} m/s"
    road = "no mapped road"
    if riding_on:
        road = riding_on["name"] + (f" ({riding_on['details']})"
                                    if riding_on["details"] else "")
    items = [{"text": f"riding along: {road}", "group": "path",
              "direction": "here", "pos": [lat, lon]}] + items
    groups = ("path", "beside", "elsewhere") if heading is not None else ("path", "elsewhere")
    items = [it for group in groups for it in items if it["group"] == group]

    titles = {
        "path": f"On your path, next {reach:.0f} m:",
        "beside": "Beside your path (meters ahead, meters left/right of it):",
        "elsewhere": f"Elsewhere within {GEOM_RADIUS_M:.0f} m (not on your path):",
    }
    msg = [f"Local time: {now:%A %d %B %Y, %H:%M}.", f"Cyclist: {head}."]
    for group in groups:
        msg += ["", titles[group]]
        lines = [f"[{n}] {it['text']}" for n, it in enumerate(items, 1)
                 if it["group"] == group]
        msg += lines or ["- nothing mapped"]
    msg += ["", f"List the hazards for the next {HORIZON_S:.0f} s of riding."]
    return SYSTEM_PROMPT, "\n".join(msg), items


# --------------------------------------------------------------------------
# LLM call and scoring
# --------------------------------------------------------------------------
def call_llm(args, system_msg, user_msg):
    body = json.dumps({
        "model": args.llm_model,
        "messages": [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ],
        "max_tokens": args.llm_max_tokens,
        "temperature": 0.0,
        # No hidden reasoning: it would cost seconds on every call. (Off by
        # default for Gemma 4 anyway; other templates just ignore it.)
        "chat_template_kwargs": {"enable_thinking": False},
        # vLLM structured output: decoding is constrained so that the answer
        # always matches RESPONSE_REGEX (a vLLM extension, not OpenAI API).
        "structured_outputs": {"regex": RESPONSE_REGEX},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{args.llm_url}/v1/chat/completions", data=body, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=args.llm_http_timeout) as r:
        doc = json.load(r)
    return doc["choices"][0]["message"]["content"]


def parse_hazards(text, items):
    """
    Hazards from the model's answer lines, each resolved against the feature
    it cites: the direction and group come from the code, not the model. A
    hazard citing no listed feature is dropped as ungrounded. None if the
    answer does not follow the format (the constrained decoding makes that
    rare, except when max_tokens cuts it short).
    """
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    if lines == ["none"]:
        return []
    hazards = []
    for ln in lines:
        m = _LINE_RE.fullmatch(ln.strip())
        if m is None:
            return None
        htype, n, activity, evidence, severity = m.groups()
        n = int(n)
        if not 1 <= n <= len(items):
            log.debug("dropping ungrounded hazard %r", ln)
            continue
        it = items[n - 1]
        hazards.append({"type": htype, "severity": severity,
                        "activity": activity, "direction": it["direction"],
                        "group": it["group"], "feature": it["text"],
                        "pos": it["pos"], "evidence": evidence.strip()})
    return hazards


def score_hazards(hazards, max_points=ENV_RISK_BOOST_MAX):
    """
    Risk points (0..max_points). Each hazard is a 0-1 risk (severity
    x where its feature is x how busy it is now); a feature cited more than
    once counts once, at its strongest; the worst feature counts fully and
    the others at SECONDARY_WEIGHT, combined as a noisy-OR.
    """
    per_feature = {}
    for h in hazards:
        r = min(0.95, SEVERITY[h["severity"]] * GROUP_WEIGHT[h["group"]]
                * ACTIVITY[h["activity"]])
        per_feature[h["feature"]] = max(r, per_feature.get(h["feature"], 0.0))
    no_risk = 1.0
    for rank, r in enumerate(sorted(per_feature.values(), reverse=True)):
        no_risk *= 1.0 - r * (1.0 if rank == 0 else SECONDARY_WEIGHT)
    return (1.0 - no_risk) * max_points


def _pose(ego):
    heading = ego.get("heading_deg")
    return {"lat": ego.get("lat"), "lon": ego.get("lon"),
            "heading": (heading % 360.0) if heading is not None else None,
            "speed": ego.get("speed_mps")}


def _project(pose, lead_m):
    # The pose lead_m further along the heading (flat earth, like the rest)
    if pose["heading"] is None or lead_m <= 0.0:
        return pose
    h = math.radians(pose["heading"])
    return dict(pose,
                lat=pose["lat"] + lead_m * math.cos(h) / _M_PER_DEG,
                lon=pose["lon"] + lead_m * math.sin(h)
                / (_M_PER_DEG * math.cos(math.radians(pose["lat"]))))


def _moved(geo, a, b):
    # (distance m, turn deg or 0 if a heading is unknown) between two poses
    ax, ay = _to_local(geo, a["lat"], a["lon"])
    bx, by = _to_local(geo, b["lat"], b["lon"])
    turn = (_angle_diff(a["heading"], b["heading"])
            if a["heading"] is not None and b["heading"] is not None else 0.0)
    return math.hypot(ax - bx, ay - by), turn


class EnvRiskEstimator:
    """
    Background worker asking the LLM about the latest pose (projected to
    when the answer lands), back-to-back as fast as it answers. It skips the
    call when the pose has not really changed (a stopped bike costs nothing)
    and pauses when radar frames stop arriving or there is no GPS fix.
    """

    def __init__(self, args, geo):
        self.args = args
        self.geo = geo
        self.enabled = geo is not None and not args.llm_disable
        self._lock = threading.Lock()
        self._pose = None            # latest pose from the radar frames
        self._pose_mono = None
        self._result = None          # {"pose", "hazards", "points", "mono", "asked"}
        self._latency_s = LATENCY_INIT_S   # running average, for the lookahead
        self._clock = site_clock(args)
        self._calls = deque(maxlen=60)     # (start mono, latency s, ok), for stats
        self._n_calls = self._n_failed = 0
        self._stop = threading.Event()

    def update_pose(self, ego):
        with self._lock:
            self._pose = _pose(ego)
            self._pose_mono = time.monotonic()

    def run(self):
        paused = None
        while not self._stop.is_set():
            with self._lock:
                pose, pose_mono, result = self._pose, self._pose_mono, self._result
            if pose is None:
                why = "no radar frame received yet"
            elif time.monotonic() - pose_mono > self.args.radar_idle_s:
                why = f"no radar frame in >{self.args.radar_idle_s:.0f}s"
            elif pose["lat"] is None or pose["lon"] is None:
                why = "no ego position fix"
            else:
                why = None
            if why != paused:
                if why:
                    log.info("%s, pausing LLM calls", why)
                else:
                    log.info("fresh pose, LLM calls resumed")
                paused = why
            if why:
                self._stop.wait(0.2)
                continue

            if result is not None:
                dist, turn = _moved(self.geo, pose, result["pose"])
                if (dist < SAME_POSE_M and turn < SAME_HEADING_DEG
                        and time.monotonic() - result["asked"] < TIME_REFRESH_S):
                    # Still where the last answer was computed: it stays valid
                    with self._lock:
                        self._result["mono"] = time.monotonic()
                    self._stop.wait(0.2)
                    continue

            lead_m = (pose["speed"] or 0.0) * self._latency_s
            query = _project(pose, lead_m)
            now = self._clock()
            t0 = time.monotonic()
            try:
                system_msg, user_msg, items = build_prompt(self.geo, query, now)
                log.debug("prompt:\n%s", user_msg)
                raw = call_llm(self.args, system_msg, user_msg)
            except Exception as e:
                log.warning("LLM call failed after %.2fs (%s)",
                            time.monotonic() - t0, e)
                self._record_call(t0, time.monotonic() - t0, False)
                self._stop.wait(self.args.llm_retry_s)
                continue
            latency_s = time.monotonic() - t0
            self._latency_s = 0.7 * self._latency_s + 0.3 * latency_s
            hazards = parse_hazards(raw, items)
            self._record_call(t0, latency_s, hazards is not None)
            if hazards is None:
                log.warning("could not parse LLM answer after %.2fs: %r",
                            latency_s, raw[:200])
                continue
            points = score_hazards(hazards, self.args.ai_context_max)
            # What the model saw and said, for debugging views (the system
            # prompt is constant, so only the per-call part is kept)
            call = {"latency_s": round(latency_s, 2), "lead_m": round(lead_m, 1),
                    "clock": f"{now:%a %d %b %Y %H:%M}", "prompt": user_msg,
                    "answer": raw.strip()}
            with self._lock:
                self._result = {"pose": query, "hazards": hazards,
                                "points": points, "mono": time.monotonic(),
                                "asked": t0, "call": call}
            log.info("LLM %.1fs, asked %.0f m ahead -> %.1f pts: %s",
                     latency_s, lead_m, points,
                     "; ".join(f"{h['type']} {h['direction']} ({h['severity']}, "
                               f"{h['group']}, {h['activity']})" for h in hazards)
                     or "no hazards")

    def _record_call(self, t0, latency_s, ok):
        with self._lock:
            self._calls.append((t0, latency_s, ok))
            self._n_calls += 1
            self._n_failed += not ok

    def _stats(self):
        """Timing of the recent calls (call under self._lock)."""
        lat = sorted(l for _, l, ok in self._calls if ok)
        starts = [t for t, _, _ in self._calls]
        gaps = [b - a for a, b in zip(starts[-21:], starts[-20:])]
        now = time.monotonic()
        return {
            "calls": self._n_calls, "failed": self._n_failed,
            "latency_last_s": round(self._calls[-1][1], 2) if self._calls else None,
            "latency_avg_s": round(sum(lat) / len(lat), 2) if lat else None,
            "latency_p95_s": round(lat[min(len(lat) - 1, int(0.95 * len(lat)))], 2) if lat else None,
            "interval_avg_s": round(sum(gaps) / len(gaps), 2) if gaps else None,
            "calls_last_min": sum(1 for t in starts if now - t <= 60.0),
            "since_last_call_s": round(now - starts[-1], 1) if starts else None,
        }

    def start(self):
        if not self.enabled:
            log.info("disabled (%s)", "--llm-disable" if self.args.llm_disable
                     else "no geometry")
            return
        threading.Thread(target=self.run, name="env-risk", daemon=True).start()

    def stop(self):
        self._stop.set()

    def snapshot(self, ego):
        """
        The contribution for a frame at pose `ego`: {"points", "state",
        "age_s", "hazards", ...}. points is 0 unless state is "ok": the result
        must be recent *and* computed for (about) the current pose. Also
        "call" (prompt, answer, latency of the call behind the result) and
        "stats" (timing of the recent calls), for debugging views.
        """
        if not self.enabled:
            return {"points": 0.0, "state": "disabled", "age_s": None,
                    "hazards": None}
        with self._lock:
            result = self._result
            stats = self._stats()
        if result is None:
            return {"points": 0.0, "state": "no_result", "age_s": None,
                    "hazards": None, "stats": stats}
        age = time.monotonic() - result["mono"]
        pose = _pose(ego)
        if age > self.args.llm_stale_s:
            state = "stale"
        elif pose["lat"] is None or pose["lon"] is None:
            state = "no_fix"
        else:
            dist, turn = _moved(self.geo, pose, result["pose"])
            state = "ok" if dist <= MAX_DRIFT_M and turn <= MAX_TURN_DEG else "moved"
        return {"points": result["points"] if state == "ok" else 0.0,
                "state": state, "age_s": round(age, 2),
                "hazards": result["hazards"],
                "asked_pos": [result["pose"]["lat"], result["pose"]["lon"]],
                "call": result["call"], "stats": stats}
