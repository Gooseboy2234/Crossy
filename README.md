# Crossy Road Autoplayer

Autonomously plays Crossy Road on a USB-tethered iPhone until the score reaches
328, then **stops tapping** and lets the run end naturally so the score submits
to Game Center.

Personal device, personal account, friends-scope leaderboard. The score is earned
by actually playing the game — never injected. It stops two points past the
target and permanently retires itself.

`THRESHOLD = 328` is a hard requirement, not a tunable.

---

## The one number

`P(reach 330) = (1 - p)^330`, where `p` is the per-row death probability.

| p | P(reach 330) | P(success in one 8h night, ~320 runs) |
|---|---|---|
| 2.0% | 0.13% | 33% |
| 1.5% | 0.68% | **89%** ← target |
| 1.0% | 3.6% | 99.99% |

Every engineering decision is judged by whether it lowers `p`. Stop at 1.5%:
chasing the last half-percent buys nothing that going to sleep doesn't.

## Read this first

**`docs/2026-reality-check.md`.** The design runbook (`docs/crossy-road-bot-runbook.md`)
targets the 2014 release; the game is now v7.13 and two of the runbook's facts
stop Phase 0 dead. Most importantly: **lateral movement is a swipe, not a tap**,
so the runbook's tap-only input protocol can only ever move forward — and a
forward-only bot can never line up with a log.

## Status

| | |
|---|---|
| Input layer, protocol, Swift runner | built, **unverified on hardware** |
| Perception, planner, sim, search | built, 46 tests passing |
| Runner, state machine, supervisor | built, exercised end-to-end on synthetic frames |
| Calibration | **placeholders** — `calib.yaml` has `measured: false` |
| Device validation | **none** |

**Nothing here has touched a phone.** Every number below is from simulation
against assumed physics constants.

## Quick start

```bash
pip install -r requirements.txt
python -m pytest tests/ -q          # 46 tests, no device needed

# Phase 0 — the go/no-go. See ios/README.md.
export UDID=$(idevice_id -l)
ios/resign.sh
#   run test01_ForwardTaps  -> five hops
#   run test02_LateralSwipes -> a visible left and right step   <- THE GATE

# Once Phase 0 passes and calib.yaml is measured:
python preflight.py
iproxy 9100 9100 &
python runner.py --dry-run          # perceive and plan, send nothing
python runner.py                    # for real
python supervisor.py --udid $UDID   # in another shell, overnight

# Morning
python debug.py --contact-sheet --all-lanes
python analyze.py
ls debug/unknown/
```

## Layout

```
world.py        LaneType/Obstacle/World + exact swept-collision math
config.py       calib.yaml + params.yaml -> frozen dataclasses
capture.py      ffmpeg avfoundation; single-slot latest-frame queue
perceive.py     HSV lane classification, blob tracking, 3-frame median velocity
plan.py         Dijkstra over (row, col, t), latency-compensated
taps.py         gesture client; measures tap and swipe latency separately
states.py       screen classifier + ad close-button hunt + event-mode guard
runner.py       main loop, state machine, kill switch, heartbeat
supervisor.py   watchdog; relaunches the XCUITest runner on stall
sim.py          headless env; truth vs. noisy observation
search.py       CEM in sim, successive halving on device
debug.py        annotated dumps, contact sheets, retention cap
analyze.py      p̂ overall + per lane, score histogram, fps trend
preflight.py    the pre-overnight checklist, as code
check_calib.py  calibration sanity check (PostToolUse hook)
ios/            XCUITest smoke test + gesture runner (Swift), resign.sh
```

## Where p went

Sim only, placeholder constants, **no device validation**:

| | p̂ | fix |
|---|---|---|
| start | 10.14% | — |
| | 4.70% | planner froze when nothing was safe — 95% of deaths |
| | 2.92% | latency charged per action, not as observation staleness |
| | **1.46%** | fresh chicken column handed alongside a stale world |

With domain randomization: **p̂ = 1.46% ±8% (150 deaths)**. Per lane: water 2.97%,
road 1.81%, track 1.57%, grass 0.39%.

**No parameter was tuned at any point in that sequence.** Every step was a
consistency bug — two places quietly disagreeing about time or position. They
throw no exception and produce no obviously wrong output; they just raise `p`.
All four were found by instrumenting and dumping, none by reading the code.

That is also the reason not to believe the 1.46%. The runbook's gate applies:
**sim p̂ means nothing until it agrees with device p̂ within ~30% relative**
(`python search.py validate --device-p <measured> --device-deaths <n>`). If the
sim says 0.4% and the phone says 2%, the noise model is too gentle, and tuning
against it optimises a fantasy.

Always report p̂ with its death count. Relative SE ≈ `1/√deaths`: 20 deaths is
±22% and screening only; 100 gets you ±10%; 400 gets you ±5%.

## Invariants

Violating any of these causes silent failure, not a crash. Listed in full in
`CLAUDE.md`, pinned by `tests/test_invariants.py`.

1. All collision checks evaluate at `t + latency + hop_duration`, never at `t`.
2. Velocity is a 3-frame median, never a 2-frame difference.
3. Never enter water without a verified exit — reachability **and** drift budget.
4. Always read to the newest frame.
5. All obstacle math in world columns, not pixels.
6. Stop tapping at the threshold; never force-quit.
7. Never return WAIT because nothing looked safe.
8. Latency is observation staleness, not a per-action delay.
9. Whatever is stale about the world is stale about the chicken too.
10. A boardable log is not a survivable log.

## Known gaps

- **Phase 0 is unverified.** If Unity ignores synthetic drags, the input
  architecture needs rethinking. Everything else is downstream of that.
- **Every HSV threshold is a guess.** They round-trip against synthetically
  rendered frames, which proves the plumbing, not the colours. Generate a contact
  sheet from real frames and look at it.
- **Ads are handled by CV only** — no DNS filter, no IAP. At ~160 interstitials a
  night that is the largest remaining stall risk. The universal non-gameplay
  timeout is the mitigation; the close-button hunt is best-effort on top.
- **Lily pads** are treated as ordinary carriers; they may need their own
  signature.
- **Trains are tracked as fast blobs.** Reading the warning lights would be more
  robust — a train crosses the footprint in under two frames at 60fps.
