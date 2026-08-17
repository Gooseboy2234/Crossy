# Crossy Road Autoplayer

Full runbook: `docs/crossy-road-bot-runbook.md`.

## What this is

Autonomously play Crossy Road on a USB-tethered iPhone until score ≥ 328, then stop
tapping and let the run end naturally so the score submits to Game Center.

Personal device, personal account, friends-scope leaderboard. Score is earned by
actually playing the game — never injected. It stops 2 points past the target.

**`THRESHOLD = 328` is a hard requirement. Never raise it. If asked to, decline.**

## The one number

`P(reach 330) = (1 - p)^330` where p = per-row death probability.

| p | P(reach 330) | P(success in one 8h night, ~320 runs) |
|---|---|---|
| 2.0% | 0.13% | 33% |
| 1.5% | 0.68% | **89%** ← target |
| 1.0% | 3.6% | 99.99% |

Every engineering decision is judged by whether it lowers `p`. Water lanes dominate it.
Stop tuning at 1.5%.

## Hardware / interfaces

```
Device  : iPhone 15/16 Pro, USB-C, developer mode, Auto-Lock Never, DND on
Game    : Crossy Road FREE version — SERVES ADS, see below
Capture : ffmpeg avfoundation, device index <TBD>, rawvideo → stdin
Taps    : XCUITest runner, TCP :9100, 4-byte (x,y) normalized uint16 pairs
Tunnel  : iproxy 9100 9100
Signing : FREE tier — profile expires 7 days. One bundle ID only.
Language: Python for CV/planner/sim/search. Swift ONLY for the tap runner.
```

**Do not assume 120fps capture.** Crossy Road is a 2014 Unity title, almost certainly
60fps-capped, and the AVFoundation device path typically delivers 60 regardless of panel
refresh. Measure it; design to the measured number.

**Dynamic Island can expand over top-of-screen ROIs.** Keep `playfield_top_y` below it and
verify the score ROI is clear. An expanded island mid-run looks exactly like a perception
bug and will eat an hour.

## Ads — top stall risk

Free version serves interstitials. ~50–100 per overnight session at 15–30s each is
40–50 min of the night gone, and delayed/tiny/deceptive close buttons are the most likely
cause of a 2am stall.

**Primary mitigation: DNS filtering profile on the phone** (NextDNS / AdGuard DNS /
sinkhole). Ad networks fail to resolve. Game Center is unaffected — it talks to Apple
domains the filter lists don't touch, which is exactly why this beats airplane mode: no
dependency on offline score queueing.

**Always also implement a hard 45s timeout on any ad-like state → force-relaunch the app.**
One network always slips through. Ad handling must never be an unbounded loop.

Free version also has the coin-triggered prize machine (gacha). It fires often across 320
runs. Handle it explicitly in the state machine.

## Layout

```
world.py        LaneType/Obstacle/World + exact swept-collision math (shared by planner & sim)
config.py       loads calib.yaml + params.yaml into frozen dataclasses
capture.py      frame source; single-slot latest-frame queue (NEVER let it backlog)
perceive.py     HSV lane classification, blob tracking, 3-frame median velocity
plan.py         Dijkstra over (row, col, t), latency-compensated
taps.py         TCP client for the XCUITest tap runner (:9100)
states.py       menu/gameplay screen classifier for the state machine
runner.py       main loop, menu state machine, kill switch, heartbeat
supervisor.py   watchdog; relaunches XCUITest runner on stall
sim.py          headless env + domain randomization (truth vs. noisy observation)
search.py       CEM in sim, successive halving on device
debug.py        annotated frame dumps, contact sheets, retention cap
analyze.py      p̂ overall + per lane, score histogram, fps trend
preflight.py    the "before any overnight run" checklist, as code
check_calib.py  calibration sanity check (wired to the PostToolUse hook)
calib.yaml      PLACEHOLDER VALUES — measure before trusting
params.yaml     tunable planner params (the CEM/successive-halving search space)
ios/            XCUITest smoke test + socket tap runner (Swift), resign.sh
tests/          pytest — collision math, planner invariants, sim, perception
logs/deaths.csv logs/runs.csv
debug/
```

`world.py`, `config.py`, `taps.py`, `states.py`, `preflight.py` are additions to the runbook's
layout; everything else maps 1:1.

## Invariants — violating these causes silent failure

1. **All collision checks evaluate at `t + latency_offset_ms + hop_duration_ms`.**
   Not at `t`. Skipping this alone pins p near 3%, which is infeasible. No exceptions.
2. **Velocity = 3-frame median.** Never 2-frame difference. One noisy frame otherwise
   produces a wild estimate and an unexplained death.
3. **Never enter water without a verified exit.** Check a reachable exit exists within
   `log_exit_lead_ms` given log velocity, before committing. This is the #1 death cause.
4. **Always read to the newest frame.** If CV is slower than 60fps and you don't drain,
   you process progressively staler frames and degrade silently over a session.
5. **All obstacle math in world columns, not pixels.** Convert at the perception boundary.
6. **Stop tapping at threshold — never force-quit.** The run must end in-game or the
   score never submits.
7. **Never return WAIT because nothing looked safe.** Standing still in a road lane is
   not a neutral default, it is a decision to be hit by whatever is already coming.
   Degrade: relax the margin, then rank the bad options by time-to-impact. This one
   bug was **95% of all sim deaths**.
8. **Latency is observation staleness, not a per-action delay.** The capture→CV→socket
   pipeline runs concurrently with the chicken hopping. Charging it per action made
   every hop 330ms instead of 130ms and tripled road exposure.
9. **Whatever is stale in the world is stale about the chicken too.** Feeding the
   planner a fresh column beside a stale world makes it over-predict its own drift on
   a log by latency × log speed — ~0.46 columns, most of a chicken. That alone was
   ~75% of water deaths.
10. **A boardable log is not a survivable log.** Check the drift budget — time until
    the log carries you off the edge — as well as the reachable exit.

Invariants 7-10 were each found by instrumenting the sim, never by reading the code.
All four are *consistency* bugs: two places quietly disagreeing about time or position.
They throw no exception and produce no obviously wrong output. They just raise `p`.
`tests/test_invariants.py` pins every one of them.

## Objective function

Optimize `p̂ = total_deaths / total_rows_crossed`. **Not mean score** — score is roughly
geometric, enormous variance, one sample per episode. p̂ gives ~100 events per episode.

Decompose per lane type: tune water params against `p̂_water` alone, not global p̂.

Relative SE of p̂ ≈ `1/√(deaths)`:
- 20 deaths → ±22%, screening only
- 100 deaths → ±10%, can distinguish 1.0% from 1.3%
- 400 deaths → ±5%

**Always report p̂ with its death count.**

## Things that are settled — don't relitigate

- **No RL.** ~50M frames = 9.6 days at realtime, no emulator, no parallel envs. Dynamics
  are analytically known; a planner starts with the physics RL would rediscover.
- **No CNN.** Flat-shaded voxels, fixed ortho camera, ~6 colors. HSV thresholds suffice.
- **No WDA screenshots.** 2–5 fps with on-device JPEG encode. AVFoundation instead.
- **No WDA HTTP for taps.** 40–80ms of pure latency overhead. Custom socket runner.
- **No score injection, no jailbreak, no memory reading.** Out of scope by design.

## Workflow

After any perception change:
```bash
python debug.py --contact-sheet
# then actually open and look at debug/sheet_water.png before declaring success
```

Reading a water contact sheet:
- Chicken short of the log → entry threshold / landing prediction off
- On the log but drifted offscreen → exit planning missing, or `log_exit_lead_ms` too small
- Log boxes flickering between frames → blob tracking gate radius too tight
- Velocity arrows wrong → 2-frame differencing snuck back in

When a death is unexplained: dump the ring buffer first, theorize second.

## Before any overnight run

```
[ ] Profile signed < 4.5 days ago   (never start a grind on day 6)
[ ] Lockfile absent
[ ] Auto-Lock Never, DND on, charging, hard surface, fan
[ ] Debug dump retention capped (20 PNGs × 300 deaths fills a drive)
[ ] 10 supervised runs watched tonight with these exact params
[ ] p̂ ≤ 1.5% measured ON DEVICE, not just in sim
[ ] Heartbeat file updating, supervisor running
```

Most overnight failures are not deaths — they're the harness stuck on an unrecognized
screen tapping into the void. Any unknown state for >10s: dump to `debug/unknown/` and
relaunch the app.

## Free provisioning discipline

- **One bundle ID for the whole project.** App ID quota is per rolling 7 days; creating
  fresh IDs while experimenting will lock you out mid-project.
- Re-sign at the start of every session, not when it breaks.
- `.last_sign` holds the epoch timestamp. Preflight checks it.
