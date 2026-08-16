# Crossy Road Autoplayer — End-to-End Runbook

**Goal:** Autonomously play Crossy Road on a USB-tethered iPhone until score ≥ 328, then stop tapping and let the run end naturally. Hard-disable afterward.

**Non-goal:** Beating the global leaderboard. Threshold cap is a design requirement, not an afterthought.

---

## 0. The number that governs everything

Reaching 330 requires surviving 330 consecutive row transitions. If per-row death probability is `p`:

`P(reach 330) = (1 - p)^330`

| p (per-row death) | P(reach 330) | Expected runs | Wall clock @ ~1.5 min/run |
|---|---|---|---|
| 3.0% | 0.004% | ~23,000 | infeasible |
| 2.0% | 0.13% | ~790 | ~20 hours |
| 1.5% | 0.68% | ~147 | ~3.7 hours |
| 1.0% | 3.6% | ~28 | ~45 min |
| 0.75% | 8.3% | ~12 | ~18 min |
| 0.5% | 19% | ~5 | ~8 min |

**Read this table as the project plan.** Going from p=2% to p=1% converts a 20-hour grind into a 45-minute one. Every engineering hour should be spent driving `p` down, and `p` is dominated by water lanes. Nothing else on this list matters as much.

### Overnight capacity changes the target

With ~8 hours of unattended runtime available, you get roughly 320 attempts per night. Probability of at least one success in a single night:

| p | P(success in one 8h night) | Nights needed |
|---|---|---|
| 2.0% | 33% | 2–3 |
| 1.5% | **89%** | 1 |
| 1.0% | 99.99% | 1 |

**p = 1.5% is the real target, not 0.75%.** Chasing the last half-percent buys you nothing you can't get by going to sleep. Tune to 1.5%, verify it on device, and start the grind — you have a ~9-in-10 shot on the first night. This is worth several hours of saved tuning.

The catch: 8 unattended hours is a *reliability* problem, not just a throughput one. See Phase 10.

---

## Phase 0 — Go/No-Go Smoke Test (15 min)

**Do not build anything else until this passes.**

Crossy Road is a Unity title. Synthetic touches from XCUITest route through `backboardd` and normally reach Unity's input layer — but not always, and if they don't, the entire project is dead.

1. Create a new iOS UI Testing Bundle target in Xcode. Set the bundle ID to `com.yesterday.crossyroad` (verify with `ideviceinstaller -l`).
2. Write exactly this:

```swift
import XCTest

final class SmokeTest: XCTestCase {
    func testTap() throws {
        let app = XCUIApplication(bundleIdentifier: "com.yesterday.crossyroad")
        app.activate()
        sleep(5)  // let it load past splash

        let c = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        for _ in 0..<5 {
            c.tap()
            usleep(400_000)
        }
        sleep(3)
    }
}
```

3. Run on device. Watch the screen.

**PASS:** chicken hops five times. Continue.
**FAIL:** nothing moves. Stop — there's no clean workaround. Close the file.

**Also measure now:** film the screen at 240fps (slo-mo) and count frames between the `tap()` call and the chicken starting to move. That's your `latency_offset_ms` seed value. Expect 80–200ms.

---

## Phase 1 — Environment (1 hour)

```bash
brew install libimobiledevice ffmpeg
pip install opencv-python numpy scipy pyyaml
```

**Free provisioning — this is a real project constraint, not a footnote.**

Your test runner is signed with a free personal team profile, which expires 7 days after signing. Consequences you have to design around:

- **Reuse exactly one bundle ID for the entire project.** Free accounts have a limited App ID quota per rolling 7-day window, and creating a fresh ID every time you experiment will exhaust it and lock you out mid-project. Pick `com.<you>.crossyrunner` and never change it.
- **Re-sign at the start of every work session**, not when it breaks. Make it muscle memory:
  ```bash
  # resign.sh
  xcodebuild build-for-testing \
    -scheme CrossyRunner -destination 'platform=iOS,id=<UDID>' \
    -derivedDataPath ./dd
  echo "$(date -u +%s)" > .last_sign
  ```
- **Never start an overnight grind on day 6 or 7 of a profile.** The runner will die at 3am and you'll lose the night. Add a preflight check:
  ```python
  age_days = (time.time() - float(open(".last_sign").read())) / 86400
  if age_days > 4.5:
      sys.exit("Profile too old for an overnight run. Re-sign first.")
  ```
- Budget the calendar accordingly: ~17.5 active hours across evenings will span **two to three expiry cycles**. Factor in ~10 minutes of re-signing friction each time.

**Ad blocking — do this now, it's the difference between a clean night and a stalled one.**

The free version serves interstitials that will cost you ~40–50 minutes per overnight
session and are the most likely cause of a 2am stall. Install a filtering DNS
configuration profile (NextDNS, AdGuard DNS, or a local sinkhole) before you build
anything. Ad networks fail to resolve; Game Center keeps working because it talks to
Apple domains the filter lists don't touch.

Verify with 10 manual runs and count the ads you still see. Full rationale and fallbacks
in Appendix A.10.

**Device prep:**
- Settings → Display & Brightness → Auto-Lock → **Never**
- Settings → Developer → enable
- Do Not Disturb **on** (a notification banner mid-run corrupts a frame and can kill a 300-row attempt)
- Plugged in, on a hard surface, ideally with a small fan pointed at it

**Provisioning warning:** free Apple Developer profiles expire in **7 days**. You will re-sign mid-project. If you have a paid account use it; if not, plan around it.

**Set up the tap channel.** WebDriverAgent works but its HTTP/JSON round-trip adds 40–80ms of pure overhead, and latency is your enemy. Better: extend the smoke test into a persistent runner that opens a TCP socket and taps on incoming bytes.

```swift
// Sketch — runner loop reading 4-byte (x,y) pairs as UInt16 pairs
func serve(port: UInt16) {
    let listener = try! NWListener(using: .tcp, on: NWEndpoint.Port(rawValue: port)!)
    // on receive: decode x,y normalized ints, call
    //   app.coordinate(withNormalizedOffset: CGVector(dx: x/10000, dy: y/10000)).tap()
    // keep the XCTest alive with a long expectation / RunLoop.
}
```

Tunnel it: `iproxy 9100 9100`. Now a tap is a 4-byte write on a warm socket.

---

## Phase 1.5 — Claude Code as the Dev Loop (30 min) — *do this early*

The chat interface can't see your phone. Claude Code runs on the iMac that's physically holding the USB cable, with real shell and filesystem access — it can dump frames, open them, edit the CV code, run the sim, and read `deaths.csv` without you screenshotting anything.

### Install

<cite index="8-1">The native installer is recommended and requires no Node.js:</cite>

```bash
curl -fsSL https://claude.ai/install.sh | bash
claude --version
claude doctor      # diagnoses install type, auth, config
```

<cite index="8-1">Runs on macOS 13+.</cite> <cite index="5-1">The npm path (`npm install -g @anthropic-ai/claude-code`) is deprecated.</cite> Authenticate on first launch — install without auth gets you nothing.

### Project setup

```bash
mkdir ~/crossy && cd ~/crossy && claude
```

Then run `/init` inside the session to generate a `CLAUDE.md`. Replace its contents with something like:

```markdown
# Crossy Road Autoplayer

## Hardware
iPhone tethered via USB. Capture: ffmpeg avfoundation device index 1.
Taps: XCUITest runner on TCP :9100 (tunnel with `iproxy 9100 9100`).

## Objective
Minimize per-row death probability p. Target p <= 0.75%.
Score cap 328 is a HARD requirement — never raise it.

## Layout
capture.py    frame source, single-slot latest-frame queue
perceive.py   HSV lane classification, blob tracking, velocity
plan.py       BFS over (row, col, t), latency-compensated
runner.py     main loop, kill switch, logging
sim.py        headless env with domain randomization
search.py     CEM + successive halving
debug.py      annotated frame dumps + contact sheets
logs/deaths.csv
debug/

## Conventions
- All obstacle math in world columns, not pixels. Convert at the perception boundary.
- Every collision check evaluates at t + latency_offset_ms + hop_duration_ms. No exceptions.
- Velocity = 3-frame median, never 2-frame difference.
- Never edit calib.yaml without re-running the calibration check.

## Workflow
After any perception change, run `python debug.py --contact-sheet` and
inspect debug/latest_sheet.png before trusting the change.
```

### Why this matters most

The bottleneck in this project is **perception debugging**, and it's visual. "The bot died in water at row 84" is nearly useless. An annotated frame showing the log was classified as water because its highlight fell outside your saturation threshold is immediately actionable. Claude Code can generate that overlay, open it, and fix the threshold in one loop.

### Useful hook

Auto-run the calibration sanity check after any edit to the perception layer:

```json
// .claude/settings.json
{
  "hooks": {
    "PostToolUse": [{
      "matcher": "Edit|Write",
      "hooks": [{
        "type": "command",
        "command": "case \"$FILE_PATH\" in *perceive.py|*calib.yaml) python check_calib.py || true;; esac"
      }]
    }]
  }
}
```

### Division of labor

| Surface | Good for |
|---|---|
| Claude Code (iMac) | Everything touching the device, frames, or logs. The inner loop. |
| Chat | Architecture, planner algorithm design, interpreting contact sheets you upload, search strategy |

---

## Phase 2 — Capture (2 hours)

**Do not use screenshot endpoints.** WDA screenshots run 2–5 fps with on-device JPEG encode. Useless.

A USB-connected iPhone appears to macOS as an `AVCaptureDevice` (this is what QuickTime's "Movie Recording → iPhone" uses). You get native-res, ~60fps, low latency.

```bash
ffmpeg -f avfoundation -list_devices true -i ""   # find the iPhone's index
```

Pipe raw frames into Python:

```python
import subprocess, numpy as np

W, H = 886, 1920   # confirm from ffprobe; downscale here if you want the CPU back

proc = subprocess.Popen([
    "ffmpeg", "-f", "avfoundation", "-framerate", "60",
    "-i", "1",                      # your device index
    "-vf", f"scale={W}:{H}",
    "-pix_fmt", "bgr24", "-f", "rawvideo", "-"
], stdout=subprocess.PIPE, bufsize=W*H*3*4)

def frames():
    n = W * H * 3
    while True:
        buf = proc.stdout.read(n)
        if len(buf) < n: break
        yield np.frombuffer(buf, np.uint8).reshape(H, W, 3)
```

**Drain the buffer.** If your CV loop is slower than 60fps you'll process stale frames and drift progressively further behind — the classic silent failure. Either read-and-discard to the newest available frame each tick, or run capture in a thread that keeps only the latest frame in a single-slot queue.

**Downscale aggressively.** Half resolution is plenty for blob detection and roughly quarters your CV cost.

---

## Phase 3 — Calibration (1 hour, one time)

Crossy Road uses a fixed **orthographic** camera. This is the gift that makes the whole project tractable: no perspective distortion, so screen-Y → world-row is a constant affine map, forever.

Capture one clean frame on a grass field and measure by hand:

```yaml
calib:
  px_per_row: 78.0          # vertical pixels between lane centers
  px_per_col: 78.0          # horizontal pixels between column centers
  chicken_screen_xy: [443, 1180]   # chicken is near-fixed; camera follows
  row0_screen_y: 1180
  playfield_top_y: 300      # ignore HUD above this
  score_roi: [40, 90, 260, 170]    # x1,y1,x2,y2
```

Sanity check: hop forward once, confirm the visual field shifts by exactly `px_per_row`. If it doesn't, your capture is being scaled somewhere — fix that before continuing.

---

## Phase 4 — Perception (3 hours)

Flat-shaded voxels, six colors, fixed lighting. **HSV thresholds and connected components.** No CNN. A neural net here is a labeling project with no upside.

### 4.1 Lane classification

Sample a horizontal strip at each row's screen-Y, take the modal hue, classify:

| Lane | Signature |
|---|---|
| grass | high-sat green, no motion |
| road | low-sat dark gray, asphalt texture |
| water | mid-blue, subtle animated texture |
| track | brown/gray with bright rail highlights |

Build `lanes[row] -> LaneType` and refresh every frame (rows scroll).

### 4.2 Obstacle detection & velocity

Per lane, threshold to non-background, run `cv2.connectedComponentsWithStats`, filter by area. Match centroids to the previous frame's by nearest-neighbor within a gate radius. Velocity = Δx / Δt.

**Use a 3-frame median for velocity, not a 2-frame difference.** One noisy frame producing a wild velocity estimate is a leading cause of unexplained deaths.

```python
@dataclass
class Obstacle:
    row: int
    x: float          # world column units, fractional
    width: float
    vx: float         # columns per second, signed

    def x_at(self, dt):
        return self.x + self.vx * dt
```

### 4.3 Score

OCR the ROI, or — cheaper and more robust — **just count successful forward row advances.** No Tesseract dependency, no font issues, and it's exactly the quantity the kill switch needs. Cross-check against OCR once per run if you want belt-and-braces.

---

## Phase 5 — Planner (4 hours) — *this is where p lives*

### 5.1 Latency compensation (non-negotiable)

Your decision executes ~200ms after the frame it was based on. Every collision check must evaluate obstacle positions at `t + latency_offset_ms + hop_duration_ms`, not at `t`. Skipping this alone will pin you around p=3%.

### 5.2 Search

Greedy per-row gets you ~3%. You need lookahead.

BFS/Dijkstra over states `(row, col, t)`:

- **Actions:** forward, left, right, wait (each advances `t` by its duration)
- **Transition:** project all obstacles forward under constant velocity; reject states that collide
- **Depth:** `bfs_depth` rows (start at 3)
- **Cost:** time elapsed, plus `column_center_bias × |col - center|`, plus `lateral_move_cost` per sideways move
- **Replan** every `replan_interval_frames` — do not execute a stale plan

### 5.3 Water — the thing that will actually kill you

Water is where greedy bots die and where nearly all of your `p` reduction is available.

- You must land **on** a log, not between logs.
- Once on it, you drift with it. Your world-column changes without you acting.
- If it carries you offscreen, you die.

Rules:
1. **Never enter water without a planned exit.** Before committing, verify a reachable exit exists within `log_exit_lead_ms` given the log's velocity. If not, wait on the bank.
2. Require `log_entry_min_overlap` of the chicken's footprint on the log at predicted landing time.
3. While riding, continuously re-check the exit. Recompute drift every frame.
4. Cap `water_max_consecutive` — refuse to plan more than N consecutive water rows in one committed sequence.

### 5.4 Eagle

Idle too long and the eagle takes you. Track time since last forward progress; past `eagle_timeout_ms`, force a move using a reduced `eagle_panic_margin_ms`. Camping is a guaranteed death; a risky hop is not.

---

## Phase 6 — Kill Switch (1 hour)

Three independent layers. Any one failing should not defeat the cap.

```python
THRESHOLD = 328          # she's at 326. 328 is enough. don't get cute.
MAX_RUNS  = 500
LOCKFILE  = Path.home() / ".crossy_bot_done"

if LOCKFILE.exists():
    sys.exit("Already succeeded. Bot is retired.")

if score >= THRESHOLD:
    stop_tapping()                      # 1. cease input; eagle ends the run naturally
    wait_for_run_end()                  # let the score submit
    LOCKFILE.write_text(f"{score} @ {datetime.now()}")   # 2. permanent disable
    teardown_xctest_runner()            # 3. kill the tap channel entirely
    sys.exit(0)
```

Stop *tapping* rather than force-quitting — the run needs to end in-game for the score to submit to Game Center. Force-quitting mid-run loses it.

---

## Phase 7 — Instrumentation (1 hour)

Log every death. This is your actual improvement gradient.

```python
# deaths.csv
run_id, final_score, death_row, lane_type, cause, params_hash, timestamp
# cause ∈ {car, train, water_gap, log_offscreen, eagle, unknown}
```

Also log per-lane-type crossing counts so you can compute **per-lane p**, which is far more statistically efficient than global p (see Phase 9).

After 50 runs, plot deaths by lane type. The distribution tells you exactly what to fix. If it's 70% water, no amount of road-parameter tuning will help you.

### 7.1 Debug artifact generator

Keep a rolling buffer of the last N frames plus the perception state that was derived from each. On death, dump the buffer with overlays drawn on. This is what makes a death diagnosable instead of merely countable.

```python
# debug.py
import cv2, numpy as np, collections, json
from pathlib import Path

RING = collections.deque(maxlen=20)   # (frame, state) tuples

LANE_COLOR = {"grass": (0,200,0), "road": (60,60,60),
              "water": (220,120,0), "track": (0,90,180), "unknown": (0,0,255)}

def record(frame, state):
    RING.append((frame.copy(), state))

def annotate(frame, st, calib):
    img = frame.copy()
    for row, lane in st["lanes"].items():
        y = int(calib["row0_screen_y"] - row * calib["px_per_row"])
        cv2.line(img, (0,y), (img.shape[1],y), LANE_COLOR.get(lane,(0,0,255)), 1)
        cv2.putText(img, f"{row}:{lane}", (4,y-4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, LANE_COLOR.get(lane,(0,0,255)), 1)

    for ob in st["obstacles"]:
        x = int(ob["x"] * calib["px_per_col"])
        y = int(calib["row0_screen_y"] - ob["row"] * calib["px_per_row"])
        w = int(ob["width"] * calib["px_per_col"])
        cv2.rectangle(img, (x-w//2, y-24), (x+w//2, y+24), (0,255,255), 2)
        # velocity vector — scaled to 500ms of travel
        vx = int(ob["vx"] * calib["px_per_col"] * 0.5)
        cv2.arrowedLine(img, (x,y), (x+vx,y), (0,255,255), 2, tipLength=0.3)

    # planned path
    pts = [(int(c*calib["px_per_col"]),
            int(calib["row0_screen_y"] - r*calib["px_per_row"]))
           for r, c in st.get("plan", [])]
    for a, b in zip(pts, pts[1:]):
        cv2.line(img, a, b, (255,0,255), 3)

    hud = (f"score={st['score']}  lat={st['latency_ms']}ms  "
           f"fps={st['fps']:.1f}  action={st['action']}")
    cv2.putText(img, hud, (10, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255,255,255), 2)
    return img

def dump_death(run_id, cause, lane_type, calib, outdir=Path("debug")):
    d = outdir / f"{run_id}_{lane_type}_{cause}"
    d.mkdir(parents=True, exist_ok=True)
    for i, (f, st) in enumerate(RING):
        cv2.imwrite(str(d / f"{i:02d}.png"), annotate(f, st, calib))
        (d / f"{i:02d}.json").write_text(json.dumps(st, default=str))
    return d
```

### 7.2 Contact sheets

One tiled image per lane type, showing the final frame of the six most recent deaths there. This is the single highest-value artifact — patterns jump out instantly that no CSV will reveal.

```python
def contact_sheet(lane_type, outdir=Path("debug"), cols=3, tile=(420, 900)):
    dirs = sorted(outdir.glob(f"*_{lane_type}_*"), key=lambda p: p.stat().st_mtime)[-6:]
    tiles = []
    for d in dirs:
        last = sorted(d.glob("*.png"))[-1]
        img = cv2.resize(cv2.imread(str(last)), tile)
        cv2.putText(img, d.name.split("_", 1)[1], (8, 28),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255,255,255), 2)
        tiles.append(img)
    while len(tiles) % cols:
        tiles.append(np.zeros((tile[1], tile[0], 3), np.uint8))
    rows = [np.hstack(tiles[i:i+cols]) for i in range(0, len(tiles), cols)]
    out = outdir / f"sheet_{lane_type}.png"
    cv2.imwrite(str(out), np.vstack(rows))
    return out
```

Run `python debug.py --contact-sheet` after every session. Two files — the water sheet and `deaths.csv` — carry a full session's worth of diagnostic information.

**Reading a water sheet:**
- Chicken visibly short of the log → entry threshold or landing prediction is off
- Chicken on the log but drifted offscreen → exit planning missing or `log_exit_lead_ms` too small
- Log boxes flickering between frames → blob tracking gate radius too tight
- Velocity arrows wrong length or direction → 2-frame differencing sneaking back in

---

## Phase 8 — Build the Simulator (2 hours) — *biggest single win*

You already have a forward model — the planner uses one. Reuse it as an environment.

Procedurally generate lane sequences (sample lane types and obstacle densities/velocities from what you logged on-device), then run the planner headless. **~10,000× realtime.** Parameter search becomes minutes instead of days.

**Domain randomization is mandatory or the transfer will fail.** A noiseless sim will happily tune you to razor-thin margins that shatter on real perception noise. Inject:

- Obstacle position jitter: ±3–5 px, Gaussian
- Velocity estimate error: ±8%
- Latency jitter: `latency_offset_ms ± 40ms`
- Random frame drops: 2% of ticks return a stale frame
- Occasional lane misclassification: 0.5%

Tune against the *noisy* sim. Those parameters transfer.

**Validate before trusting it.** Run your current on-device parameters through the sim and compare predicted p against measured on-device p. Within ~30% relative is good enough to optimize against. If the sim says 0.4% and the phone says 2%, your noise model is too gentle — fix that first, or you'll optimize a fantasy.

---

## Phase 9 — Parameter Search

### 9.1 The parameters

```yaml
# Timing
safety_margin_ms:        120     # [40, 400]
latency_offset_ms:       200     # [80, 320]

# Water
log_entry_min_overlap:   0.55    # [0.35, 0.85]
log_exit_lead_ms:        400     # [150, 900]
water_max_consecutive:   4       # [2, 8]

# Eagle
eagle_timeout_ms:        2200    # [1200, 3200]
eagle_panic_margin_ms:   60      # [10, 150]

# Search
bfs_depth:               3       # [2, 5]
replan_interval_frames:  2       # [1, 6]
column_center_bias:      0.3     # [0.0, 1.5]
lateral_move_cost:       1.2     # [0.5, 3.0]
```

### 9.2 Objective — use p̂, not mean score

Mean score is a terrible objective. Run length is roughly geometric, so its variance is enormous; distinguishing candidates needs hundreds of episodes.

Instead estimate the death rate directly:

```
p̂ = total_deaths / total_rows_crossed
```

One episode yields ~100 row-crossing events instead of one score sample. Vastly more information per unit time.

Relative standard error of `p̂` ≈ `1/√(deaths)`. So:
- 20 deaths → ±22% — coarse screening only
- 100 deaths → ±10% — can distinguish 1.0% from 1.3%
- 400 deaths → ±5% — fine tuning

### 9.3 Decompose per lane type

The real efficiency unlock. Compute `p̂_water`, `p̂_road`, `p̂_track`, `p̂_eagle` separately, and tune each parameter group against its own sub-metric:

- water params → `p̂_water` only
- road/timing params → `p̂_road` only
- eagle params → `p̂_eagle` only

You get the deaths you need for a tight water estimate in a fraction of the episodes, because you're not diluting the signal with unrelated events.

### 9.4 Algorithm: CEM in sim, successive halving on device

**In simulator — Cross-Entropy Method:**

```python
mu, sigma = init_from_current_params()
for gen in range(12):
    cands = [clip(np.random.normal(mu, sigma)) for _ in range(60)]
    scores = [-eval_p_hat(c, deaths=400) for c in cands]   # sim: cheap
    elite = [cands[i] for i in np.argsort(scores)[-9:]]    # top 15%
    mu    = np.mean(elite, axis=0)
    sigma = np.std(elite, axis=0) + 0.02   # floor prevents premature collapse
```

12 generations × 60 candidates × 400 deaths is trivial in sim. Minutes.

**On device — successive halving.** Sim-tuned parameters need real validation, but on-device evaluation is expensive. Don't give every candidate a full budget:

1. Take the top 8 candidates from CEM
2. Round 1: 15 deaths each (~10 min each) → keep top 4
3. Round 2: 40 deaths each → keep top 2
4. Round 3: 150 deaths each → pick the winner

Total ≈ 6–8 hours, mostly unattended overnight. Compare against your baseline throughout — if none of the sim-tuned candidates beat the hand-tuned baseline on device, your noise model is wrong. Go back to Phase 8.

### 9.5 Stopping rule

Stop tuning when `p̂ ≤ 1.5%`. That's an ~89% chance of success in a single overnight session — see §0. Pushing to 1.0% is nice if it falls out of the search cheaply, but do not spend an extra evening on it. Sleep is a cheaper resource than tuning time.

Re-tune only if a full night fails to produce a hit, since that's evidence your on-device `p̂` is worse than your measurement suggested.

---

## Phase 10 — Unattended Overnight Operation

Eight hours alone is a different engineering problem from eight hours supervised. The bot doesn't need to be good at Crossy Road for that long — it needs to be good at *recovering*.

### 10.1 The menu state machine

Most unattended failures aren't deaths. They're the harness sitting on a screen it doesn't recognize, tapping into the void. Enumerate every non-gameplay state and give each an escape:

| State | Detection | Action |
|---|---|---|
| Gameplay | chicken blob present, lanes classify | play |
| Death screen | score overlay + button row | tap continue |
| Main menu | logo region matches template | tap play |
| Ad interstitial | full-screen, no lane structure | find close button, tap; **hard 45s timeout → relaunch app** |
| Prize machine (gacha) | distinctive color histogram | tap through or back out — free version only, fires often |
| Crashed to springboard | no Crossy UI at all | relaunch app |
| Unknown | none of the above for >10s | dump frame to `debug/unknown/`, relaunch app |

That last row matters most. **Log every unknown state with a frame dump.** Your first night will surface two or three screens you didn't know existed, and the dumps are how you add them.

### 10.2 Supervisor process

Do not trust the XCUITest runner to live 8 hours. Test runners get killed by timeouts, memory pressure, and iOS itself. Run a separate supervisor that owns the lifecycle:

```python
# supervisor.py
HEARTBEAT = Path("run/heartbeat")     # harness touches this every loop
STALL_S   = 45

def healthy():
    return HEARTBEAT.exists() and (time.time() - HEARTBEAT.stat().st_mtime) < STALL_S

def restart_runner():
    subprocess.run(["pkill", "-f", "xcodebuild"], check=False)
    time.sleep(3)
    subprocess.Popen(["xcodebuild", "test-without-building",
                      "-xctestrun", "dd/Build/Products/CrossyRunner.xctestrun",
                      "-destination", f"platform=iOS,id={UDID}"])
    time.sleep(20)   # runner boot + socket bind

while not LOCKFILE.exists() and runs < MAX_RUNS:
    if not healthy():
        log("stall detected, restarting runner")
        restart_runner()
    if not device_connected():
        log("device gone"); break
    time.sleep(5)
```

Also set `executionTimeAllowance` generously in the test's `setUp()` — the default test timeout will otherwise kill your runner on its own schedule, mid-night, for no visible reason.

### 10.3 Preflight checklist

Run through this before walking away:

```
[ ] Profile signed < 4.5 days ago
[ ] Lockfile absent
[ ] Auto-Lock = Never, DND on, charging
[ ] Phone on hard surface, fan on
[ ] Debug dump retention capped (see below)
[ ] Disk space checked
[ ] Watched 10 supervised runs tonight with these exact params
[ ] p-hat measured <= 1.5% on device, not just in sim
[ ] Heartbeat file updating
[ ] DNS filter profile active — 10 manual runs, ad count verified
```

The disk item bites people: 20 annotated PNGs per death across ~300 deaths will fill a drive overnight. Cap retention in the dumper to the last 6 per lane type and delete the rest as you go.

### 10.4 Thermal reality

Hours of Unity rendering plus continuous USB capture will throttle the phone. Frame rate sags, effective latency climbs, and `p` silently degrades — you will see scores trend downward across the night with no code change whatsoever.

Two options: duty-cycle the grind (45 min on, 10 min idle), or just log frame rate per run and accept the decay. If your death histogram shows 3am runs dying earlier than 9pm runs, that is thermals, not variance.

### 10.5 Morning triage

```bash
python debug.py --contact-sheet --all-lanes
python analyze.py logs/deaths.csv     # p-hat overall + per lane, score histogram, fps trend
ls debug/unknown/                     # new screens to handle
```

Three questions, in order. Did it hit 328? If not, what was the measured p-hat versus what you tuned to? And did it actually run all night, or stall at 1am and spend seven hours tapping a menu?

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Taps register but chicken is late/wrong | Latency compensation missing or `latency_offset_ms` too low |
| Dies almost exclusively in water | Exit-planning rule not implemented, or `log_entry_min_overlap` too permissive |
| Progressively worse within a session | Frame buffer backlog (not draining to newest) or thermal throttle |
| Random deaths on empty lanes | Velocity estimated from 2 frames instead of 3-frame median |
| Sim p ≪ device p | Domain randomization too gentle — increase noise, re-tune |
| Frequent eagle deaths | `eagle_timeout_ms` too high, or planner deadlocking on an unreachable goal |
| Everything breaks after a week | Provisioning profile expired. Re-sign. |
| Overnight run produced 3 results then nothing | Runner died and supervisor wasn't running, or heartbeat never wired up |
| Hundreds of runs logged with score 0 | Stuck on an unhandled menu state — check `debug/unknown/` |
| Scores trend down across the night | Thermal throttling. Duty-cycle or add cooling. |
| Runner dies at a consistent interval | `executionTimeAllowance` default timeout |
| Can't create a new App ID | Free-tier quota exhausted. Reuse one bundle ID. |
| Stalls repeatedly on full-screen non-game content | Ad interstitial with no timeout fallback. Add the hard 45s relaunch. |
| Capture is 60fps on a 120Hz phone | Expected. Crossy Road is a 2014 Unity title, likely 60fps-capped. Design to measured fps. |
| Perception breaks only sometimes, near top of screen | Dynamic Island expanded over a tracked ROI |

---

## Time Budget

| Phase | Hours |
|---|---|
| 0 — Smoke test | 0.25 |
| 1 — Environment | 1 |
| 1.5 — Claude Code loop | 0.5 |
| 2 — Capture | 2 |
| 3 — Calibration | 1 |
| 4 — Perception | 3 |
| 5 — Planner | 4 |
| 6 — Kill switch | 1 |
| 7 — Instrumentation | 1 |
| 8 — Simulator | 2 |
| 9 — Param search | 1 active + overnight |
| 10 — Unattended harness + grind | 2 |
| **Total** | **~19 active hours** (spanning 2–3 provisioning cycles) |

Nineteen hours to win by two points. The engineering is the point; the two points are the excuse.

---

# Appendix A — Full Context

*Everything a fresh session needs to pick this up cold.*

## A.1 Origin

Game Center leaderboard, Crossy Road, friends scope:

| Rank | Player | Score |
|---|---|---|
| 1 | Bumper Needer (Cobalt3363) | 326 |
| 2 | Dane Rv There Yet Judge (Rolls Pizza) | 285 |
| 3 | **SomewhatGoose (you)** | **222** |

Rank 1 is an ex — broken up 2 years, no contact 1 year, amicable, both partnered now. Game Center pushes a notification when someone beats your score. The entire objective is triggering that notification once.

**Tone check:** this is a joke with a hard ceiling, and the ceiling is the part that keeps it a joke. It's a personal account, a personal device, and a friends-scope leaderboard. The score is produced by actually playing the game, not injected. It stops two points past hers. Keep all four of those properties true and this stays in "funny engineering project" territory.

## A.2 Confirmed constraints

| Item | Value |
|---|---|
| Goal | Build it properly, then ship it |
| Dev machine | M4 iMac, macOS |
| Overnight runtime | Available — ~8h unattended |
| Apple Developer | Free tier — 7-day provisioning expiry |
| Target device | iPhone, USB-tethered, developer mode enabled |
| Score cap | 328 (hard) |
| p target | 1.5% per-row death |
| Dev loop | Claude Code on the iMac |

Operator background: Swift/iOS (shipped a SwiftUI app), comfortable with multi-GPU Linux infrastructure and Python tooling. Assume high technical fluency; skip the hand-holding, don't skip the reasoning.

## A.3 Open questions — *fill these in before Phase 2*

```yaml
iphone_model:        iPhone 15/16 Pro     # CONFIRMED — USB-C, A17/A18 Pro, ProMotion
crossy_version:      free (ads)           # CONFIRMED — see A.10, this is the big one
cv_language:         python + opencv      # CONFIRMED — swift only for the tap runner

# Still to measure:
screen_px:                  # exact native res; values in this doc are PLACEHOLDERS
character:                  # affects biome / lane mix — see A.4
xcode_version:
measured_tap_latency_ms:    # from Phase 0 slo-mo
measured_capture_fps:       # do NOT assume 120 — see A.9
avfoundation_device_index:
```

## A.4 Unexplored lever: character selection

Crossy Road characters can alter the biome — different lane-type mixes, different obstacle sets, sometimes different visual palettes. Since **water is where nearly all your `p` lives**, a character whose biome is water-light is a direct, free reduction in death rate that costs zero engineering.

Before Phase 4, play a few manual runs across several characters and log the lane-type distribution. If one biome is meaningfully drier, use it. This could plausibly be worth more than several hours of planner tuning.

Caveat: confirm the alternate biome still submits to the same "Score" leaderboard, and that its palette doesn't break the HSV thresholds you're about to calibrate. A biome with an unusual color scheme trades a perception problem for a planning one.

## A.9 Hardware notes — iPhone 15/16 Pro

**ProMotion is probably irrelevant.** Crossy Road is a 2014 Unity title and is almost
certainly capped at 60fps; a 120Hz panel does not manufacture information the game
never rendered. The AVFoundation device-capture path also typically delivers 60fps
regardless of panel refresh. **Measure `measured_capture_fps` in Phase 2 and design to
the measured number, not the spec sheet.** If it does come through at 120, great — halved
perception latency — but don't build assuming it.

**USB-C is a genuine advantage.** Better capture bandwidth than Lightning; you're less
likely to be resolution-limited on the capture path.

**A17/A18 Pro thermals are better but not immune.** Sustained performance is meaningfully
stronger than base models, which matters over an 8-hour grind, but hours of continuous
Unity rendering plus USB capture will still throttle. Keep logging fps per run (§10.4).

**Dynamic Island — check your ROIs.** It occupies top-center and can expand for Live
Activities, charging indicators, and background audio. Two mitigations: place the score
ROI clear of it (score is top-*left*, so likely fine — verify), and make sure
`playfield_top_y` excludes it entirely. An expanding island mid-run that overlaps a
tracked region will look like a perception failure and cost you an hour of debugging.

**Native resolution is high.** Downscale aggressively at the ffmpeg stage — half res
quarters your CV cost and loses you nothing for blob detection.

---

## A.10 Ads — the largest new complication

The free version serves interstitials, and this is the single biggest threat to
unattended operation.

**Direct cost:** roughly 50–100 ads across 320 overnight runs at 15–30s each is
**40–50 minutes of your night**, which is 5–8% of your attempts, gone.

**Indirect cost is worse.** Ad close buttons appear on a delay, sit at variable
positions, are sometimes deliberately tiny or misleading, and occasionally lead to a
second screen. This is genuinely adversarial CV, and it's the most likely thing to
strand the harness at 2am.

### Strategy 1 (preferred): DNS-level ad blocking

Install a filtering DNS configuration profile on the phone (NextDNS, AdGuard DNS, or a
local sinkhole). Ad networks — Unity Ads, AppLovin, ironSource, AdMob — resolve to
nothing and fail fast, so the game usually skips straight past.

**Game Center is unaffected**, because it talks to Apple domains that filtering lists
don't touch. That's the whole reason this beats airplane mode: you keep score submission
online and never have to trust offline queueing.

Verify before relying on it: play 10 manual runs with the profile active and count ads.
If you still see them, the app is using a domain your list misses — check the DNS query
log and add it.

### Strategy 2 (fallback): airplane mode grind

No network, no ads, guaranteed. But score submission needs connectivity, so you're
betting on Crossy Road caching the local best and submitting on next launch with network.
That's the normal behavior and it usually works — but "usually" is doing real work in that
sentence, and you'd find out you lost a 328 only after the fact.

If you go this route: the kill switch stops at 328 and writes the lockfile as usual, the
score sits locally overnight, and you restore network and relaunch in the morning.
**Do not delete the debug dumps until you've confirmed the score posted.**

### Strategy 3: handle them in CV

Last resort. Detect the interstitial (full-screen, no lane structure), hunt for a close
button, tap it, and — critically — always have a timed fallback that force-relaunches the
app if you're still stuck after 45s. Never let ad handling be an unbounded loop.

Do this *in addition* to Strategy 1 regardless, because one ad network will always slip
through.

### Also free-version-only: the prize machine

Coins accumulate during play and trigger a gacha screen. Over 320 runs you will hit this
many times. Add it to the state machine explicitly (§10.1) — it's a recognizable,
easily-handled screen, but only if you know it's coming.

---

## A.5 Decision log

| Decision | Rationale | Reconsider if |
|---|---|---|
| No reinforcement learning | ~50M frames = 9.6 days at 60fps realtime, no emulator, no parallel envs. Dynamics are analytically known — a planner starts with the physics RL would spend a week rediscovering. | Never, for this project |
| No CNN for perception | Flat-shaded voxels, fixed ortho camera, ~6 colors. HSV thresholds suffice. A CNN is a labeling project with no upside. | Alternate biome with ambiguous palette |
| Custom XCUITest socket runner over WebDriverAgent | WDA's HTTP/JSON round-trip adds 40–80ms of pure latency. Latency directly drives `p`. | Socket runner proves unstable overnight |
| AVFoundation capture, not WDA screenshots | Screenshots are 2–5 fps with on-device JPEG encode. Unusable. | Never |
| BFS over (row, col, t), depth 3 | Greedy ≈ 3% p. Lookahead is the difference between 3% and 1%. | Deeper search if CPU allows |
| Optimize p̂, not mean score | Score is ~geometric; enormous variance, one sample per episode. p̂ gives ~100 events per episode. | Never |
| Per-lane-type p̂ decomposition | Tuning water params against global p̂ dilutes the signal with unrelated deaths | Never |
| Sim + domain randomization before device tuning | ~10,000x realtime. Device evaluation is the scarce resource. | Sim fails validation against device p̂ |
| CEM in sim → successive halving on device | Device evals cost ~10 min each; don't give every candidate full budget | — |
| p target 1.5%, not 0.75% | 8h unattended = ~320 attempts = 89% success in one night. Further tuning buys nothing sleep doesn't. | One full night fails |
| Stop tapping (not force-quit) at threshold | Run must end in-game for the score to submit to Game Center | Never |
| Python for CV/planner/sim, Swift only for the tap runner | Iteration speed dominates; you'll rewrite water logic 20×. NumPy/SciPy own the sim + CEM layer. CV is ~5–15ms on M4 — not the bottleneck. Swift where mandatory only. | Never |
| DNS ad-blocking over airplane mode | Keeps Game Center online; no dependency on offline score queueing | Filtering fails to stop ads |
| Count row advances instead of OCR for score | No Tesseract dependency, no font brittleness, and it's exactly what the kill switch needs | — |

## A.6 Failure modes ranked by expected cost

1. **Unity rejects synthetic touches** — project is dead. Phase 0 answers this in 15 minutes. Do it first.
2. **Overnight stall on an unhandled menu** — lose an entire night for zero information. Supervisor + `debug/unknown/` dumps mitigate.
3. **Latency compensation omitted or mis-tuned** — pins p near 3%, which is infeasible. The single highest-leverage correctness detail.
4. **Water exit-planning missing** — dominant death cause. Entering water without a verified exit is the classic greedy-bot failure.
5. **Frame buffer backlog** — CV falls progressively behind, degrades silently over a session. Always read to the newest frame.
6. **Sim too clean** — parameters tuned to razor margins that shatter on real noise. Validate sim p̂ against device p̂ before trusting search output.
7. **2-frame velocity differencing** — one noisy frame produces a wild estimate and an unexplained death. Use a 3-frame median.
8. **Provisioning expiry mid-grind** — lose a night. Preflight check.
9. **Thermal throttle** — silent p degradation across a long session. Log fps per run.
10. **Disk exhaustion from debug dumps** — cap retention.

## A.7 Rejected alternatives

- **Score injection / packet manipulation** — out of scope by design. The score must be earned by gameplay.
- **Jailbreak / Frida / memory reading** — unnecessary and destroys the "actually played it" property.
- **Simulator instead of physical device** — Game Center scores from Simulator won't submit meaningfully, and the game may not run.
- **Screen mirroring to Mac, then automating the mirror** — added latency, no input path back.
- **Just playing it manually** — considered, genuinely viable, explicitly declined. The engineering is the point.

## A.8 Glossary

| Term | Meaning |
|---|---|
| `p` | Per-row death probability. The one number that matters. |
| `p̂` | Estimated p = total_deaths / total_rows_crossed |
| Row | One lane advance forward. Score == rows advanced. |
| Column | Lateral position, world units, fractional |
| Latency budget | capture + CV + tap round-trip, ~200ms. All collision math evaluates at t + this. |
| Ring buffer | Last ~20 (frame, state) pairs kept for death forensics |
| Contact sheet | Tiled final-frames of the 6 most recent deaths per lane type |
| Kill switch | Threshold stop at 328 + lockfile + runner teardown |
| Successive halving | Eliminate weak candidates on small budgets, escalate survivors |
| Domain randomization | Injected sim noise so tuned params survive real perception error |

## A.9 Working agreements for Claude Code sessions

- **Never raise `THRESHOLD` above 328.** If asked to, decline and point at A.1.
- Report `p̂` with its death count. `p̂` from 20 deaths is ±22% and can't distinguish 1.0% from 1.3%.
- After any perception change, generate a contact sheet and actually look at it before declaring success.
- When a death is unexplained, dump the ring buffer before theorizing.
- Prefer measurement over intuition: latency, fps, and per-lane p̂ are all cheap to instrument.
- `calib.yaml` values in this document are placeholders. Measure them.
