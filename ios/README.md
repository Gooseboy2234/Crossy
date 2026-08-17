# iOS runner — setup and Phase 0

There is no `.xcodeproj` checked in: a free-provisioning project is bound to your
team ID and UDID, so a committed one would only ever be wrong on your machine.
Six minutes of clicking, once.

## 1. Create the project

1. Xcode → new iOS App, product name **CrossyRunner**, bundle ID
   `com.<you>.crossyrunner`. **Pick this once and never change it** — free
   accounts have an App ID quota per rolling 7-day window and minting a fresh ID
   per experiment will lock you out mid-project.
2. Signing & Capabilities → your personal team, automatic signing.
3. File → New → Target → **UI Testing Bundle**, name it `CrossyRunnerUITests`.
4. Delete the generated test file. Drag `SmokeTest.swift` and
   `GestureRunner.swift` into that target (Target Membership: UI test bundle
   only).
5. Run once on the device so iOS registers the developer certificate. Settings →
   General → VPN & Device Management → trust it.

`SmokeTest.swift` defines `kCrossyBundleID`; `GestureRunner.swift` uses it.

## 2. Confirm the game's bundle ID

```bash
brew install libimobiledevice
idevice_id -l                 # your UDID
ideviceinstaller -l | grep -i crossy
```

Expect `com.hipsterwhale.crossy`. The runbook's `com.yesterday.crossyroad` is
wrong and `XCUIApplication(bundleIdentifier:)` fails silently-ish on a bad ID —
you get a foreground timeout, not a clear error.

## 3. Phase 0 — the go/no-go

```bash
export UDID=$(idevice_id -l)
./resign.sh
```

Then run the tests **in order**, watching the phone, not the console:

| Test | Pass condition |
|---|---|
| `test01_ForwardTaps` | chicken hops forward five times |
| `test02_LateralSwipes` | one visible step **left**, then two visible steps **right** |
| `test03_SwipeTuningGrid` | only if 02 failed — note which duration/travel works |
| `test04_SwipeUpAsForward` | three forward hops from swipe-up alone |

**Test 02 is the actual gate.** The runbook only ever tested taps, which is the
easy case — Unity almost always accepts synthetic taps. A synthetic *drag*
reaching Unity's input layer through `backboardd` is a much less certain thing,
and lateral movement in Crossy Road is a swipe. Without it the bot can only move
forward, can never line up with a log, and the project is over.

If 02 fails, run 03 before giving up. Unity's gesture recogniser has thresholds
on travel distance and pre-drag hold time; a drag that is too slow reads as a pan
and gets discarded, and one that is too short reads as a tap. **A swipe that
turns into a forward hop is the diagnostic signature of "too short"** — the
chicken moves, just in the wrong direction.

If 04 passes, prefer swipe-up for `forward`: all four actions then share one
gesture path and therefore one latency distribution, which makes latency
compensation meaningfully easier than mixing a fast tap with a slow drag.

### Record what you measured

Each test prints its own synthesis time — that is XCUITest's cost, the floor on
latency, and no socket tuning gets under it. Film the screen at 240fps and count
frames from the call to the chicken moving for true end-to-end latency.

Put the numbers in `params.yaml`:

- tap end-to-end → `latency_offset_ms`
- swipe end-to-end minus tap end-to-end → `swipe_extra_latency_ms`
- hop animation duration → `fixed.hop_duration_ms`

Once the runner is live, `taps.py` keeps measuring both from ack round-trips, so
these are seeds rather than final values.

## 4. Run the persistent runner

```bash
iproxy 9100 9100 &
xcodebuild test-without-building \
  -xctestrun dd/Build/Products/CrossyRunner_iphoneos*.xctestrun \
  -destination "platform=iOS,id=$UDID"
```

Then from the repo root:

```bash
python3 taps.py     # activates the app, 5 taps, 4 swipes, prints latency stats
```

Protocol is in `docs/tap-protocol.md`.
