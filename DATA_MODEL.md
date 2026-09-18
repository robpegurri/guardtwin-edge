# SMI risk record — data model

One JSON object per radar frame (~10 Hz), emitted by `broker.py`. The
digital twin connects to the broker's TCP port and receives NDJSON (one
compact object per line); the same record is available as UDP push and
JSON Lines.

## Risk model

The risk is computed in four stages: (1) a risk value for every radar
obstacle, (2) aggregation of the obstacle risks into a scene risk,
(3) a penalty for the quality of the 5G link, (4) an asynchronous
environmental-hazard boost from an LLM given site geometry. All
thresholds quoted below are named constants at the top of `broker.py`,
meant to be tuned with road tests.

### 1. Per-obstacle risk (0–1)

Only obstacles within `PROX_MAX_M = 30 m` are considered; anything
farther is ignored. For each obstacle two danger factors are computed:

- **Time-to-collision — `f_ttc`, weight 0.6.** The dominant signal: how
  soon the obstacle will reach the bicycle if nothing changes.

  ```
  TTC = range_m / closing_speed          (closing speed = radar speed, m/s)
  ```

  Computed only when the obstacle is flagged `approaching` and its
  speed is above 0.1 m/s (otherwise `f_ttc = 0`). Mapping:

  | TTC | f_ttc |
  |---|---|
  | ≤ 1.5 s (`TTC_MIN_S`) | 1.0 — no time to react |
  | 1.5 s … 8 s | linear from 1.0 down to 0.0 |
  | ≥ 8 s (`TTC_MAX_S`) | 0.0 — plenty of time |

- **Proximity — `f_prox`, weight 0.4.** Distance alone, regardless of
  motion: something close is dangerous even if it is not approaching
  right now (it can turn, open a door, step off a sidewalk).

  ```
  f_prox = 1 − range_m / 30      (1.0 at 0 m, 0.0 at ≥ 30 m)
  ```

- **Yaw closing — `f_yaw`, weight `YAW_BOOST_MAX = 0.15`.** DROPMO v1.1
  publishes each object's confirmed direction of motion (`yaw`) once the
  tracker has seen sustained movement, not just an instantaneous
  velocity reading. `f_ttc` alone under-weights a *crossing* obstacle —
  its range may not be clearly decreasing yet even though it is walking
  or driving onto the bicycle's path. `f_yaw` catches that: it is how
  closely the obstacle's own heading points back towards the bicycle,
  independent of current range-rate.

  ```
  bearing = compass bearing from the obstacle's position back to the bicycle
  f_yaw = max(0, cos(yaw_deg − bearing))     (1.0 heading straight at the
                                               bicycle, 0.0 heading away
                                               or across)
  ```

  `f_yaw = 0` whenever the object's yaw isn't valid yet (untracked
  motion) or either position is unavailable — this is a strict addition,
  it never lowers what `f_ttc`/`f_prox` alone would give.

The weighted sum `0.6·f_ttc + 0.4·f_prox + 0.15·f_yaw` (capped at 1.0)
is then scaled by three multipliers:

| Multiplier | Values | Why |
|---|---|---|
| Object class | TRUCK 1.0, CAR 0.9, SCOOTER 0.7, UNKNOWN/OTHER 0.7, BICYCLE 0.6, PERSON 0.5 | for a cyclist, the mass of the opponent decides the outcome of a collision |
| Track confidence | 0–1 from the radar tracker | a shaky track must not trigger a strong reaction |
| Static damping | × 0.5 if the `static` flag is set (`STATIC_DAMP`) | a parked/fixed obstacle is a hazard, not an active threat |

Obstacles whose resulting risk is below `THREAT_FLOOR = 0.05` are
treated as noise and dropped (they neither enter the aggregate nor the
`threats[]` list).

### 2. Scene aggregation — `radar_risk` (0–10)

Obstacle risks combine as a **probabilistic OR**:

```
radar_risk = (1 − Π(1 − rᵢ)) × 10
```

Each rᵢ is treated as an independent "probability of trouble". This has
two properties a plain max or sum would not give:

- two simultaneous threats rank **higher** than the worst one alone
  (max would ignore the second car);
- the total **saturates** instead of exploding: ten mild threats do not
  fake a critical scene the way a plain sum would.

### 3. Channel reliability boost — `chan_penalty`

The DT closes the loop over the 5G network: it receives the risk and
sends warnings/actuations back. The link quality therefore **qualifies
the reliability of the radar data reaching the DT** — it is not an
autonomous risk source. A degraded link amplifies the risk the radar
has actually detected (the DT may not be able to warn in time), while
an empty scene stays at zero whatever the link state. The broker takes
the best available link among the monitored UEs (state `ok`/`stale`,
highest channel score) and computes:

```
chan_penalty = radar_risk × CHANNEL_BOOST_MAX × (1 − score/10)   # boost = 0.3
risk_score   = min(10, radar_risk + chan_penalty)
             = min(10, radar_risk × (1 + 0.3 × (1 − score/10)))
```

- perfect channel (score 10) → no amplification;
- dead link (UE `unresolved`, `no_score`, or score 0) → detected radar
  risk amplified by up to **+30 %**;
- no radar risk → no risk, regardless of the link: a degraded channel
  alone does not raise the risk.

`chan_penalty` in the record is the resulting additive delta
(`risk_score − radar_risk`), so the DT can still see the two
contributions separately.

The channel score itself (0–10) comes from the metrics REST server and
is channel-only by construction: weighted RSRP / CQI / SNR / BLER, no
MCS (MCS also depends on traffic load).

### 4. Environmental risk boost — `dt_ai_env_risk`

A "DT AI Environmental Risk" contribution computed **asynchronously** by
an LLM (served locally via vLLM, OpenAI-compatible API). Given the
current radar/channel situation and static site geometry (buildings,
roads) around the deployment, the model looks for hazards radar alone
cannot see — a blind corner behind a building, a tight junction, a
narrow passage — and reports a 0–10 severity score.

Unlike `chan_penalty`, this contribution is **not** proportional to
`radar_risk`: it can add risk to an otherwise empty scene (radar sees
nothing yet, but the cyclist is approaching a known blind corner). It is
capped by an absolute constant so it can never dominate the score on its
own:

```
dt_ai_env_risk = min(ENV_RISK_BOOST_MAX, llm_score / 10 × ENV_RISK_BOOST_MAX)   # boost = 2.0
risk_score      = min(10, radar_risk + chan_penalty + dt_ai_env_risk)
```

- LLM score 10/10 (severe environmental hazard) → full +2.0 points;
- LLM score 0/10 (no added hazard) → +0;
- linear in between.

**This is inherently asynchronous.** LLM inference is far slower than
the ~10 Hz radar rate, so it runs in a background thread, independent of
the per-frame `assess()` call, calling vLLM **back-to-back** — the next
call starts as soon as the previous one returns, with no fixed period to
tune (the blocking HTTP call itself paces the loop). Each call's latency
is logged (`env-risk: LLM call took N.NNs`) so the real prompt-to-answer
time is directly observable. Only a *failed* call is throttled
(`--llm-retry-s`, default 1 s), so a vLLM server that's still loading or
briefly down doesn't get hammered with a fast retry loop. `assess()`
always uses whatever result is currently cached:

| `env.state` | Meaning | Contribution |
|---|---|---|
| `ok` | a result was obtained within `--llm-stale-s` (default 12 s) | as computed above |
| `stale` | last result is older than `--llm-stale-s` | `0` |
| `no_result` | no successful call yet (startup, vLLM down/loading, or `--llm-disable`) | `0` |

An unreachable or slow vLLM server, a malformed LLM reply, or the
feature being disabled all degrade the same way — `dt_ai_env_risk` is
`0` and the radar/channel stream is never interrupted, exactly like the
broker already tolerates AMF/metrics being unreachable.

**Environmental context**: a static, per-deployment GeoJSON file
(`--geometry-file`) — buildings, roads and amenities around the fixed
sensor site, not queried live — is loaded once at startup and reduced to
compact `{type, name, centroid}` entries. On each LLM call, only the
features within `ENV_RISK_GEOM_RADIUS_M` (120 m) of the cyclist's
current position are included in the prompt (capped at
`ENV_RISK_GEOM_MAX_FEATURES` = 40, closest first), so prompt size stays
bounded regardless of how large the site survey file is. The file is
optional — omitting `--geometry-file` runs the feature with radar/channel
context only.

The file itself is generated offline with `fetch_geometry.py` (downloads
OSM buildings/roads/amenities around a point via the Overpass API, once,
by hand) rather than by `broker.py` fetching it live at every startup —
that was tried and reverted: the public Overpass instance 504s often
enough under load that it wasn't worth doing on every restart for
geometry that doesn't change at runtime anyway.

**vLLM call**: plain chat completion, no `response_format`/guided-JSON
constraint — tested against `vllm/vllm-openai:v0.6.3` serving
`Qwen/Qwen2.5-7B-Instruct`, `response_format: {"type": "json_object"}`
made the model's `outlines`-backed guided decoding produce *malformed*
output (garbled keys/whitespace) instead of the requested shape, while
plain prompted instruction-following reliably returned clean JSON.
Response parsing stays defensive regardless (tolerates code fences,
surrounding chatter, or a non-JSON reply) since this is prompt
convention, not a guarantee.

### Risk levels

| `risk_score` | `risk_level` |
|---|---|
| < 1 | `none` |
| 1 – 3 | `low` |
| 3 – 5 | `medium` |
| 5 – 7.5 | `high` |
| ≥ 7.5 | `critical` |

### Worked examples

- Empty scene, any channel state: `radar_risk = 0` →
  **risk 0.0, `none`** (a degraded link alone adds nothing).
- Four approaching obstacles (CAR 50 km/h at 25 m → TTC 1.8 s, SCOOTER
  TTC 1.7 s, BICYCLE TTC 1.6 s, PERSON at 4 m), channel score 5.9:
  per-obstacle risks ≈ 0.5 each, OR-combined `radar_risk = 9.2`;
  boost 9.2 × 0.3 × 0.41 ≈ 1.1 → **risk 10.0 (capped), `critical`**.
- Single car at TTC 3 s, `radar_risk = 5.0`: score 10 → risk 5.0;
  score 5 → risk 5.75; link dead → risk 6.5 (`high` in every case, but
  the DT sees the reliability of the figure via `chan_penalty` and
  `channel.state`).

### Tuning summary

| Constant | Default | Meaning |
|---|---|---|
| `PROX_MAX_M` | 30 m | radar horizon considered for risk |
| `TTC_MIN_S` / `TTC_MAX_S` | 1.5 s / 8 s | full-risk and zero-risk TTC bounds |
| `YAW_BOOST_MAX` | 0.15 | max additive weight for an object whose confirmed yaw heads back towards the bicycle |
| `CLASS_WEIGHT` | table above | per-class danger multiplier |
| `STATIC_DAMP` | 0.5 | damping for static obstacles |
| `CHANNEL_BOOST_MAX` | 0.3 | max amplification of the detected radar risk with a dead link (+30 %) |
| `THREAT_FLOOR` | 0.05 | per-obstacle noise floor |
| `ENV_RISK_BOOST_MAX` | 2.0 | max additive contribution (risk points) from `dt_ai_env_risk` |
| `ENV_RISK_GEOM_RADIUS_M` | 120 m | geometry features sent to the LLM: bbox around ego |
| `ENV_RISK_GEOM_MAX_FEATURES` | 40 | cap on geometry features embedded per prompt |

## Structure

```json
{
  "service": "smi.risk",
  "src": "001010000167802",
  "ts_us": 1789573456123456,
  "seq": 42,
  "age_ms": 23.5,
  "risk_score": 6.4,
  "risk_level": "high",
  "radar_risk": 5.5,
  "chan_penalty": 0.9,
  "channel": {
    "imsi": "001010000167802",
    "ue_id": 2,
    "score": 5.7,
    "state": "ok"
  },
  "dt_ai_env_risk": 1.2,
  "env": {
    "score": 6.0,
    "reasoning": "blind corner near building",
    "age_s": 1.5,
    "state": "ok"
  },
  "ego": {
    "pos": [44.6478, 10.9257],
    "speed_mps": 0.0,
    "fix": true,
    "sim": true
  },
  "n_obj": 4,
  "threats": [
    {"cls": "CAR", "pos": [44.64757, 10.92572], "range_m": 25.3,
     "ttc_s": 1.8, "appr": true, "yaw_valid": true, "yaw_deg": 231.4, "risk": 5.2},
    {"cls": "SCOOTER", "pos": [44.64767, 10.92568], "range_m": 14.3,
     "ttc_s": 1.7, "appr": true, "yaw_valid": false, "yaw_deg": null, "risk": 4.6}
  ]
}
```

## Fields

| Field | Type | Description |
|---|---|---|
| `service` | string | Record type, always `smi.risk`. |
| `src` | string | IMSI of the bicycle's UE (the one in `channel`; falls back to the first configured IMSI while unresolved). |
| `ts_us` | int | Assessment instant, µs UTC since epoch. |
| `seq` | int | Source DROPMO frame sequence (loss detection). |
| `age_ms` | float\|null | Radar capture → assessment latency, ms. |
| `risk_score` | float | **Overall risk 0–10** (10 = maximum danger). The value the DT acts on. |
| `risk_level` | string | `none` / `low` / `medium` / `high` / `critical`. |
| `radar_risk` | float | Risk from the radar scene alone, 0–10. |
| `chan_penalty` | float | Extra risk due to the degraded channel: `risk_score − radar_risk`, proportional to `radar_risk` (up to +30 % of it). 0 when the scene is empty or the link is perfect. |
| `channel.imsi` | string | UE whose link was used (best among the monitored list). |
| `channel.ue_id` | int\|null | Its `ranUeNgapID` (`null` if unresolved). |
| `channel.score` | float\|null | Channel-only quality score 0–10 (RSRP/CQI/SNR/BLER, no MCS). |
| `channel.state` | string | `ok` / `stale` / `no_score` / `unresolved`. |
| `dt_ai_env_risk` | float | Additive contribution from the DT AI Environmental Risk LLM, 0–`ENV_RISK_BOOST_MAX`. `0` whenever `env.state` is not `ok`. |
| `env.score` | float\|null | Raw 0–10 severity score last returned by the LLM (`null` if none yet). |
| `env.reasoning` | string\|null | Short phrase from the LLM explaining the score. |
| `env.age_s` | float\|null | Age of the cached LLM result, seconds. |
| `env.state` | string | `ok` / `stale` / `no_result`. |
| `ego.pos` | [lat, lon]\|null | Bicycle position (WGS84). `null` without GPS fix. |
| `ego.speed_mps` | float\|null | Bicycle ground speed. |
| `ego.fix` | bool | Valid position and heading at capture time. |
| `ego.sim` | bool | `true` = dummy GPS, positions are fake. |
| `n_obj` | int | Obstacles seen by the radar in this frame. |
| `threats` | array | Up to 3 obstacles with non-negligible risk, highest first. |
| `threats[].cls` | string | Obstacle class. |
| `threats[].pos` | [lat, lon]\|null | Absolute position, if georeferenceable. |
| `threats[].range_m` | float | Distance from the bicycle. |
| `threats[].ttc_s` | float\|null | Time-to-collision, s (`null` if not approaching). |
| `threats[].appr` | bool | Approaching. |
| `threats[].yaw_valid` | bool | `true` once DROPMO has confirmed sustained motion for this object (see `dropmo/yaw.py`); `false` for objects that haven't moved enough yet to have a meaningful direction. |
| `threats[].yaw_deg` | float\|null | Confirmed direction of motion, degrees from North clockwise (world heading). `null` when `yaw_valid` is `false`. |
| `threats[].risk` | float | This obstacle's contribution, 0–10. |

Typical size: ~350 B with 2 threats — well within any MTU, ~3× smaller
than the v2 fused frame.

## Transport

- **TCP (primary)**: DT clients connect to `--serve-port`; one NDJSON
  line per record. A stalled client is dropped.
- **UDP push (optional, `--dt-host`)**: one datagram per record.
