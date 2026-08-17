# Getting started — operator guide

Start to finish, from a fresh Mac to an unattended overnight grind. Roughly 19
active hours spread across two or three provisioning cycles.

Read `2026-reality-check.md` before anything else. The design runbook targets the
2014 game and two of its facts stop you dead on step one.

---

## Where things run

| Machine | Runs | Why |
|---|---|---|
| **iMac** | Claude Code, all Python, ffmpeg, iproxy, Xcode | It holds the USB cable |
| **iPhone** | Crossy Road and the XCUITest runner | It is the *target*, not a dev box |

**Claude Code does not run on the iPhone**, and you don't want it to. The whole
point is that Claude Code sits on the machine physically holding the cable, where
it can dump frames, open them, edit the CV code and read `deaths.csv` without you
screenshotting anything.

You *can* start and monitor Claude Code sessions from your phone — the Claude app
or claude.ai/code — but those run in a cloud container with no path to a USB
device on your desk. Useful for reading logs from bed at 2am; useless for the
device loop. Keep the real work in the local CLI.

```bash
curl -fsSL https://claude.ai/install.sh | bash
claude --version
claude doctor
cd ~/crossy && claude          # CLAUDE.md is already in the repo
```

---

## Day 0 — the go/no-go (15 minutes)

**Do not build anything else until this passes.** Crossy Road is a Unity title;
synthetic touches route through `backboardd` and normally reach Unity's input
layer — but not always, and if they don't the project is over.

```bash
brew install libimobiledevice ffmpeg
pip install -r requirements.txt
python -m pytest tests/ -q        # 46 tests, no device needed

idevice_id -l                     # your UDID
ideviceinstaller -l | grep -i crossy   # expect com.hipsterwhale.crossy
```

Build the Xcode project (`ios/README.md`, ~6 minutes of clicking), then:

```bash
export UDID=$(idevice_id -l)
ios/resign.sh
```

Run the tests **in order, watching the phone**:

| Test | Pass condition |
|---|---|
| `test01_ForwardTaps` | five forward hops |
| **`test02_LateralSwipes`** | **one visible step left, then two right** |
| `test03_SwipeTuningGrid` | only if 02 failed |
| `test04_SwipeUpAsForward` | three forward hops from swipe-up alone |

**Test 02 is the gate.** Lateral movement in Crossy Road is a swipe, and a
synthetic drag reaching Unity is far less certain than a tap. Without it the bot
can only move forward, can never line up with a log, and water is impassable.

If 02 fails, run 03 before giving up: a swipe that turns into a *forward hop*
means the pre-drag hold was too short, not that gestures are unsupported. If
nothing in the grid works, stop and close the file — there is no clean
workaround.

**Also measure now.** Film the screen at 240fps and count frames from the call to
the chicken moving. Do it for a tap and for a swipe separately; they will differ.

---

## Day 0 — device prep

- Display & Brightness → Auto-Lock → **Never**
- Settings → Developer → enable
- **Do Not Disturb on** — one notification banner corrupts a frame and can kill a
  300-row attempt
- Plugged in, hard surface, small fan pointed at it
- **Select a character in the ORIGINAL world.** Space swaps the eagle for UFOs,
  Dinosaur for a pterodactyl. The wrong world silently invalidates both your HSV
  calibration and the eagle model.
- **Check no limited-time event is running.** Hopside Down flips the entire
  screen; Crashy Cart replaces hopping with a cart. Either one overnight is a
  total-loss night.

### Ads

You're on CV-only handling, so expect ~160 interstitials a night and budget
75–90 minutes of the session for them. The universal non-gameplay timeout is what
stops one bad creative stranding the harness until morning; the close-button hunt
is best-effort on top.

If you change your mind, any regular character purchase (~$0.99) removes autoplay
ads forever and deletes this entire category of risk. It buys silence, not
points — the score is still earned.

---

## Phase 1–3 — capture and calibration (~3 hours)

```bash
python capture.py        # lists devices, then measures actual fps
```

Put the device index and the **measured** fps in `calib.yaml`. Do not assume 120:
Crossy Road is a 2014 Unity title, almost certainly 60fps-capped, and the
AVFoundation path typically delivers 60 regardless of panel refresh.

Then measure the geometry by hand from one clean frame on a grass field:
`px_per_row`, `px_per_col`, `chicken_screen_xy`, `row0_screen_y`,
`playfield_top_y`, `score_roi`.

**Sanity check:** hop forward once and confirm the field shifts by exactly
`px_per_row`. If it doesn't, capture is being scaled somewhere — fix that first.

Keep `playfield_top_y` **below the Dynamic Island**. An island that expands
mid-run over a tracked ROI looks exactly like a perception bug and will eat an
hour.

Set `measured: true`, then:

```bash
python check_calib.py
```

### Tune the HSV thresholds

Every colour range in `calib.yaml` is a placeholder. The tests prove the
plumbing, not the colours.

```bash
python runner.py --dry-run       # perceives and plans, sends nothing
python debug.py --contact-sheet --all-lanes
```

**Then actually open `debug/sheet_water.png` and look at it** before declaring
success. Reading it:

| What you see | What it means |
|---|---|
| Chicken short of the log | entry threshold or landing prediction off |
| On the log, drifted offscreen | exit planning, or `log_exit_lead_ms` too small |
| Boxes flickering between frames | blob tracking gate radius too tight |
| Velocity arrows wrong | 2-frame differencing snuck back in |

---

## Phase 4 — supervised runs

```bash
iproxy 9100 9100 &
# ...start the XCUITest runner (ios/README.md)
python taps.py                   # 5 taps, 4 swipes, prints measured latency
```

Put those numbers in `params.yaml`: tap end-to-end → `latency_offset_ms`, the
swipe/tap difference → `swipe_extra_latency_ms`, hop animation →
`fixed.hop_duration_ms`.

Then watch it play. **Ten supervised runs minimum**, and actually watch them —
this is where you find the screens the state machine doesn't know about.

```bash
python runner.py
python analyze.py
ls debug/unknown/
```

---

## Phase 5 — validate the sim, then tune

**Do not skip this.** The sim currently reports p̂ = 1.46%, and that number means
nothing until it agrees with the phone.

```bash
python search.py validate --device-p 0.021 --device-deaths 120
```

Within ~30% relative and you can optimise against it. If the sim says 0.4% and
the phone says 2%, the noise model is too gentle — fix `sim.Noise` first, or you
will tune to razor-thin margins that shatter on real perception noise while the
sim reports excellent numbers.

```bash
python search.py cem --group water --generations 8
python search.py cem --group all
```

Water first: it is where nearly all of `p` lives.

**Report p̂ with its death count, always.** Relative SE ≈ `1/√deaths` — 20 deaths
is ±22% and screening only, 100 gets ±10%, 400 gets ±5%.

**Stop at p̂ ≤ 1.5%.** That's ~89% success in a single night. Chasing 1.0% buys
you nothing that sleeping doesn't.

---

## Phase 6 — the overnight grind

```bash
python preflight.py              # refuses if anything blocking fails
```

It checks what it can and asks you for the rest. The two that actually bite:

- **Never start a grind on day 6 of a 7-day profile.** The runner dies at 3am and
  you lose the night. Preflight blocks past 4.5 days.
- **Cap debug retention.** 20 PNGs × 300 deaths fills a drive by morning.

Then, in three shells:

```bash
iproxy 9100 9100
xcodebuild test-without-building -xctestrun ios/dd/Build/Products/*.xctestrun \
  -destination "platform=iOS,id=$UDID"
python runner.py & python supervisor.py --udid $UDID
```

Go to sleep.

### Morning triage

```bash
python analyze.py
python debug.py --contact-sheet --all-lanes
ls debug/unknown/
```

Three questions, in order:

1. **Did it hit 328?** If so, `~/.crossy_bot_done` exists and the bot has retired
   itself. Confirm the score actually posted to Game Center **before deleting the
   debug dumps.**
2. **What was the measured p̂ versus what you tuned to?**
3. **Did it actually run all night**, or stall at 1am and spend seven hours
   tapping a menu? `analyze.py` flags this — hundreds of runs scoring 0 is the
   signature. Anything in `debug/unknown/` is a screen to add to the state
   machine.

If scores trend downward across the night with no code change, that's thermal
throttling, not variance. Duty-cycle the grind or add cooling.

---

## When something breaks

| Symptom | Likely cause |
|---|---|
| Chicken hops but never sideways | Swipes not reaching Unity — back to Phase 0 |
| Taps register but the chicken is late | `latency_offset_ms` too low |
| Dies almost entirely in water | `log_entry_min_overlap` too permissive, or exit planning off |
| Progressively worse within a session | Frame backlog or thermal throttle |
| Random deaths on empty lanes | 2-frame velocity differencing crept back in |
| Sim p̂ ≪ device p̂ | Domain randomization too gentle |
| Hundreds of runs at score 0 | Stuck on an unhandled screen — check `debug/unknown/` |
| Everything breaks after a week | Provisioning profile expired. Re-sign. |
| Perception fails only near the top of the screen | Dynamic Island expanded over an ROI |

**When a death is unexplained: dump the ring buffer first, theorize second.**
Every bug found in this codebase so far was found by instrumenting, not by
reading the code — and every one was two places quietly disagreeing about time or
position, throwing no exception and producing no obviously wrong output.
