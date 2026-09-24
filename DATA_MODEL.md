# Risk model

How GUARD-TWIN Edge turns what the bike senses into a risk score and a risk
level. For the project context and how to run the stack, see
[`README.md`](README.md).

- [Overview](#overview)
- [Inputs](#inputs)
- [Deterministic Score (radar)](#deterministic-score-radar)
- [Channel Penalty](#channel-penalty)
- [AI Context Score (LLM)](#ai-context-score-llm)
- [From score to level and to risk-events](#from-score-to-level-and-to-risk-events)
- [Worked example](#worked-example)
- [Parameters](#parameters)
- [The risk record](#the-risk-record)

## Overview

The broker computes a risk score from 0 to 10 for **every frame** the bike
sends (about 10 per second), as the sum of three contributions:

```
risk_score = min(10, Deterministic Score + Channel Penalty + AI Context Score)
                     0 – 10                0 – 3 (default)    0 – 2 (default)
```

| Contribution | Answers the question | Source | Code |
|---|---|---|---|
| **Deterministic Score** | How dangerous are the objects the radar sees right now? | radar objects in the bike's frames | `object_risk()`, `assess()` in [`broker.py`](broker.py) |
| **Channel Penalty** | Can we trust that warnings will get through in time? | channel score of the bike's 5G link | `NetworkState`, `assess()` in [`broker.py`](broker.py) |
| **AI Context Score** | How dangerous is this place, at this time of day? | site map + local date and time, judged by an LLM | [`ai_assess.py`](ai_assess.py) |

The three contributions are designed to complement each other:

- The **radar** covers what is actually moving around the bike. It is the
  main term and the only one that can reach 10 alone.
- The **channel** does not add risk of its own: it *amplifies* the radar risk
  when the link is poor, and an empty scene stays at 0 whatever the link.
- The **AI** adds a small, bounded amount for what the place implies (a
  blind corner, a school at entry time, tram tracks). It never sees the
  radar, so nothing is counted twice.

## Inputs

**Radar frames** arrive over UDP (default port 30491), one JSON object per
frame:

```jsonc
{
  "seq": 1234, "t_capture_us": 1790183213000000,
  "ego": {"lat": 45.0649, "lon": 7.6597, "heading_deg": 205.0, "speed_mps": 5.0,
          "fix_ok": true, "simulated": false},
  "objects": [
    {"object_id": 3, "class": "CAR", "lat": 45.06498, "lon": 7.65971,
     "range_m": 10.0, "speed_kmh": 18.0, "approaching": true, "static": false,
     "track_confidence": 0.9, "yaw_deg": 180.0, "yaw_valid": true}
  ]
}
```

- `heading_deg` is a compass heading: degrees from north, clockwise.
- `yaw_deg` is an object's direction of motion, on the same compass scale.
- Frames whose ego position lies outside the AoI are dropped.

The **channel score** (0–10, higher is better) of the bike's link comes from
the 5G core's metrics server. The broker first asks the AMF which
`ranUeNgapID` the bike's IMSI has, then polls that UE's score.

The **site map** is `files/geometry.geojson`, an OpenStreetMap extract made
with `tools/fetch_geometry.py`. It is read once, at startup.

## Deterministic Score (radar)

### One object

Each radar object gets a risk between 0 and 1:

```
risk = confidence × class_weight × (0.6 × f_ttc + 0.4 × f_prox + 0.15 × f_yaw)
risk = risk × 0.5        if the object is static
risk = min(1, risk)
```

| Factor | Definition |
|---|---|
| `f_prox` | proximity: `1 − range / 30 m`. Objects farther than **30 m** are ignored (risk 0). |
| `f_ttc` | time to collision, only for approaching objects. `TTC = range / closing speed`, with `speed_kmh` taken as the closing speed; objects closing slower than 0.1 m/s get no TTC. `f_ttc` is 1 at TTC ≤ **1.5 s**, 0 at TTC ≥ **8 s**, and linear in between. |
| `f_yaw` | 0–1, how directly the object's confirmed direction of motion points at the bike: `max(0, cos(Δ))`, where Δ is the angle between its heading and the direction towards the bike. It counts only when the radar marks the heading as valid (`yaw_valid`), and adds up to **0.15**. |
| `class_weight` | TRUCK 1.0 · CAR 0.9 · SCOOTER 0.7 · BICYCLE 0.6 · PERSON 0.5 · UNKNOWN/OTHER/anything else 0.7 |
| `confidence` | the radar's `track_confidence` (1 if missing), so doubtful tracks count less. |
| static | stationary objects (e.g. parked cars) count half. |

Objects below **0.05** (0.5 on the 0–10 scale) are treated as noise.

### All objects together

Objects combine as a *probabilistic OR*: two cars at 3 s TTC are worse than
one, but the result never exceeds 1.

```
Deterministic Score = 10 × (1 − Π (1 − risk_i))
```

The three riskiest objects are reported in the record as `threats`. Every
located object is also listed in `objects`, for map views.

## Channel Penalty

### Channel state

The broker monitors the bike's IMSI(s):

- It resolves IMSI → `ranUeNgapID` with the AMF every 60 s, and immediately
  when unknown.
- It polls the channel score every **1 s**.

Each UE is in one of four states:

| State | Meaning |
|---|---|
| `ok` | fresh score |
| `stale` | last score older than **5 s** (still used) |
| `no_score` | UE resolved, but the metrics server has no score |
| `unresolved` | the AMF does not know the IMSI, or cannot be reached |

With several IMSIs, the best one is used: the best state first, then the
highest score.

### Penalty

```
link_quality    = score / 10   if the state is ok or stale, otherwise 0
Channel Penalty = (Deterministic Score / 10) × CHAN_PENALTY_MAX × (1 − link_quality)
```

- The penalty scales with the radar risk, so it is **0 when the radar sees
  nothing**, whatever the link.
- It reaches **`CHAN_PENALTY_MAX`** (default 3 points) only with radar risk 10
  and a dead link.
- **An unknown channel counts as a dead link.** If the AMF or the metrics
  server is unreachable, the penalty is at its maximum for the current radar
  risk. The system errs on the side of caution when it cannot vouch for the
  link.

## AI Context Score (LLM)

This score captures what the **place and the moment** add to the risk: the
street layout around the bike and how busy it is likely to be at this date
and hour. An LLM (Gemma 4 12B, 4-bit, served by vLLM on the edge server)
*detects and explains* hazards from a description of the map; the code
*locates and scores* them. So every point can be traced back to a named
hazard, a map feature and a reason.

### 1. What the model is shown

The code turns the site map into a short description of the bike's
surroundings, relative to where it is heading. For example:

```
Local time: Wednesday 23 September 2026, 07:55.
Cyclist: heading 117° (south-east), 5.0 m/s.

On your path, next 30 m:
[1] riding along: residential street "Corso Rodolfo Montevecchio" (limit 50, one-way)
[2] 20 m ahead: residential street "Via Luigi Colli" (one-way), crosses your path
[3] 26 m ahead: pedestrian crossing (marked)

Beside your path (meters ahead, meters left/right of it):
[4] 0 m ahead, 7 m right: school "Liceo Scientifico Galileo Ferraris"
[5] 11 m ahead, 7 m right: 4-storey building

Elsewhere within 80 m (not on your path):
[6] 29 m ahead-left: pedestrian crossing (marked)
...
```

- **Features kept:** streets (with speed limit, lanes, one-way, cycle lanes,
  rough surfaces, unlit), paths and steps, crossings, traffic lights, stop
  and give-way signs, bus stops, tram tracks, buildings (with storeys), and
  places that draw people or vehicles (schools, universities, hospitals, car
  parks, cafés, shops…). Benches, bins and similar are dropped.
- **Distances** are to the **nearest point** of each shape (a building's
  wall, not its centre).
- **Groups:** the code sorts features into three groups, because a small
  model is poor at spatial filtering:

| Group | Contains |
|---|---|
| *On your path* | the street being ridden; roads and paths that cross the direction of travel within the stretch ahead; points within 5 m of the line of travel. The stretch is `max(25 m, speed × 6 s)`. |
| *Beside your path* | features within 15 m to the side of that stretch, e.g. the buildings that hide a side street. |
| *Elsewhere* | the rest within 80 m. Things behind the bike count only within 15 m, and at most 8 features are listed. |

- **Deduplication:** OSM splits streets into many pieces and blocks into many
  buildings, so only the nearest of each kind is kept per group and position.
- **Time:** the local date and time is given in the site's time zone
  (`--site-tz`, default Europe/Rome), or a fixed one for tests (`LLM_CLOCK`).

### 2. What the model answers

The answer is **constrained while it is generated**: vLLM enforces a regular
expression, so the answer always parses. It contains at most 3 lines, or the
word `none`:

```
<type> <feature number> <activity> <evidence> | <severity>
junction 2 busy side street hidden by corner building | high
```

| Field | Values |
|---|---|
| type | `blind_corner`, `junction`, `crossing`, `narrow_passage`, `fast_traffic`, `conflict_zone`, `poor_surface` |
| feature number | the listed feature the hazard comes from |
| activity | how busy that place usually is at this date and time: `quiet`, `normal`, `busy` |
| evidence | the reason, at most 8 words (60 characters) |
| severity | `low`, `medium`, `high` |

The model runs at temperature 0, with "thinking" off. A compact line format is
used instead of JSON because output tokens are what the latency is made of:
about 11 tokens per hazard, against about 37 in JSON.

### 3. Grounding

Each hazard must cite a listed feature by its number. The code then takes the
hazard's **direction**, **group** and **map position** from that feature, not
from the model. A hazard citing a number that doesn't exist is dropped.

### 4. Scoring

Each hazard gets a risk between 0 and 1:

```
hazard risk = min(0.95, severity × group weight × activity)
```

| severity | | group | | activity | |
|---|---|---|---|---|---|
| low | 0.20 | on your path | 1.0 | quiet | ×0.5 |
| medium | 0.45 | beside your path | 0.6 | normal | ×1.0 |
| high | 0.75 | elsewhere | 0.2 | busy | ×1.4 |

The hazards then combine **conservatively**:

1. **One risk per feature:** if the same feature is cited several times (e.g.
   as a junction *and* a blind corner), only the strongest counts.
2. **The worst feature counts fully, the others at half weight,** combined as
   a probabilistic OR:

```
AI Context Score = AI_CONTEXT_MAX × (1 − (1 − r₁) × Π_{i≥2} (1 − 0.5 × rᵢ))     r₁ ≥ r₂ ≥ …
```

With the default maximum of 2 points, the AI Context Score alone can at most
raise an empty scene to *low*.

### 5. When the model is asked

- **Not on a timer.** A new call starts as soon as the previous answer
  arrives, while the bike moves.
- **Same position, no call.** No new call is made while the bike stays within
  3 m and 10° of the last question, except every **5 minutes**, because the
  time of day is part of the context.
- **Paused** when no radar frame has arrived for 5 s, or when there is no
  position fix.
- **Lookahead.** A call takes 0.2–2 s, during which the bike keeps moving. So
  the model is asked about the position the bike **will be at** when the
  answer arrives: the current position projected along the heading by
  `speed × average latency`. The average is running, each new call weighing
  30%, and starts at 2 s. At 5 m/s with ~1.5 s answers, that's about 7–8 m
  ahead.

### 6. When a result counts

The last result contributes to a frame only if its state is **`ok`**:

| State | Meaning | Points |
|---|---|---|
| `ok` | the bike is within **20 m** and **45°** of the position the result was computed for, and the result is at most **12 s** old | the result's score |
| `moved` | the bike has left that position (e.g. it turned around) | 0 |
| `stale` | no fresh result for 12 s (e.g. the LLM is down); a stationary bike keeps its result fresh | 0 |
| `no_fix` | no position in the current frame | 0 |
| `no_result` / `disabled` | nothing computed yet, or the feature is off (`LLM_DISABLE`, or no site map) | 0 |

The record always includes the result's hazards, the call's prompt and raw
answer, and timing statistics, including when the points are 0.

## From score to level and to risk-events

The broker names a **level** from the total score:

| risk_score | level |
|---|---|
| < 1.0 | none |
| 1.0 – < 3.0 | low |
| 3.0 – < 5.0 | medium |
| 5.0 – < 7.5 | high |
| ≥ 7.5 | critical |

The escalator maps the score to the Risk Escalation Service's **`riskLevel`
1–5**, as `round(risk_score / 2)` rounding halves up, then clamped to 1–5.
It sends a risk-event **only when `riskLevel` changes**, addressed to the
devices the Device Location API reports in the AoI, and skips the event when
there are none.

> The two scales use different cut points. For example, 5.0 is *high* for
> the broker but `riskLevel` 3; 7.5 is *critical* but `riskLevel` 4, and
> `riskLevel` 5 starts at 9.0.

## Worked example

Bike heading north at the AoI, channel score 3/10, 07:55 on a school day.
All numbers below come from the code, with default parameters:

| Object | Details | Risk (×10) |
|---|---|---|
| car | 10 m ahead, closing at 18 km/h (TTC 2.0 s), heading straight at the bike, confidence 0.9 | 7.9 |
| pedestrian | 15 m away, closing at 5 km/h (TTC 10.8 s, so no TTC factor) | 0.9 |
| parked car | 6 m away, static | 1.3 |

- **Deterministic Score** = 10 × (1 − (1 − 0.786)(1 − 0.090)(1 − 0.130)) = **8.3**
- **Channel Penalty** = 8.3 / 10 × 3 × (1 − 0.3) = **1.7**
- **AI Context Score:** the model reports three hazards:
  - a `high` hazard on the path at a `busy` time: min(0.95, 0.75 × 1.0 × 1.4) = 0.95
  - a `medium` hazard on the path, `normal`: 0.45
  - a `low` hazard beside the path, `normal`: 0.2 × 0.6 = 0.12

  AI Context Score = 2 × (1 − 0.05 × (1 − 0.225) × (1 − 0.06)) = **1.9**
- **Total** = min(10, 8.3 + 1.7 + 1.9) = **10.0**: level *critical*,
  `riskLevel` 5.

With the pedestrian alone and no AI hazards, the same frame would score
0.9 + 0.2 = **1.1** (*low*).

## Parameters

| Parameter | Default | Set with |
|---|---|---|
| Channel Penalty maximum | 3.0 points | `CHAN_PENALTY_MAX` (compose/.env, dashboard) → `--chan-penalty-max` |
| AI Context Score maximum | 2.0 points | `AI_CONTEXT_MAX` (compose/.env, dashboard) → `--ai-context-max` |
| Fixed LLM clock | real time | `LLM_CLOCK` (compose/.env, dashboard) → `--llm-clock` |
| AI result validity | 12 s | `--llm-stale-s` |
| LLM pause without frames | 5 s | `--radar-idle-s` |
| Channel score poll / stale | 1 s / 5 s | `--score-period` / `--score-stale-s` |
| IMSI re-resolution | 60 s | `--resolve-period` |
| TTC range, proximity range, class weights, static damping, yaw boost, noise floor, level thresholds | see above | constants in `broker.py` |
| severity, group and activity weights; secondary weight; max hazards; drift, turn and refresh limits; prompt geometry | see above | constants at the top of `ai_assess.py` |

## The risk record

The broker publishes one NDJSON record per frame on TCP port 30500. The
escalator and the dashboard read it. The main fields:

| Field | Content |
|---|---|
| `seq`, `ts_us`, `age_ms` | frame sequence number, time of assessment, frame age at assessment |
| `risk_score`, `risk_level` | total score (0–10) and level name |
| `radar_risk`, `chan_penalty`, `ai_env_risk` | Deterministic Score, Channel Penalty, AI Context Score |
| `limits` | the maximum of each contribution (for gauges) |
| `channel` | IMSI, `ranUeNgapID`, channel score and state used |
| `env` | AI result: `state`, `age_s`, `hazards` (type, severity, activity, direction, group, cited feature, map position, evidence), `asked_pos` (lookahead position), `call` (prompt, raw answer, latency, clock), `stats` (call timing) |
| `ego` | bike position, heading, speed, fix, simulated flag |
| `n_obj`, `threats`, `objects` | object count, the 3 riskiest objects (with TTC), and every located object |
