# Capture: what works, what is dead, and why

Measured 2026-08-16 on macOS 27.0 (build 26A5406e, arm64), Xcode 27.0 beta
(27A5228h), iPhone 16 Pro (iPhone17,1) on iOS 27.0, connected by USB **and**
paired over the network.

`capture.py` is written against a premise that no longer holds on this machine:

> A USB-tethered iPhone appears to macOS as an AVCaptureDevice (this is what
> QuickTime's "Movie Recording → iPhone" uses): native resolution, ~60fps, low
> latency.

The phone still *has* the capability. The macOS side of the path is gone.

---

## What was tried

### 1. `ffmpeg -f avfoundation` — DEAD

`ffmpeg -f avfoundation -list_devices true -i ""` returns:

```
[0] FaceTime HD Camera
[1] Goose's iPhone Desk View Camera
[2] Goose's iPhone Camera
[3] Capture screen 0
```

Indices 1 and 2 are Continuity Camera — the phone's *camera*, not its screen.
There is no screen device. This is the trap `find_iphone_index()` originally fell
into: its regex matched `/iPhone|Apple/` and returned **1**, the Desk View
Camera. Perception would have run against a webcam view of the desk, silently.

### 2. Native `AVCaptureDevice` discovery with the CoreMediaIO flag — DEAD

The documented trick is that iOS screen-capture devices are hidden until a
process sets `kCMIOHardwarePropertyAllowScreenCaptureDevices` on the CoreMediaIO
system object. QuickTime sets it; ffmpeg has no flag to. `tools/screencap` sets
it and then enumerates every device type across `.muxed` and `.video`:

```
47B4B64B-…  FaceTime HD Camera      [BuiltInWideAngleCamera]  media=vide
DC4F3EE0-…  Goose's iPhone Camera   [External]                media=vide
```

No device with `uniqueID` equal to the phone's UDID (`00008140-001A59E1263B801C`).
The screen is not exposed. The reason:

```
/System/Library/CoreMediaIO/Plug-Ins/DAL/   → does not exist
/Library/CoreMediaIO/Plug-Ins/DAL/          → third-party only, empty
systemextensionsctl list                    → 0 extensions
```

Apple's DAL plug-in directory is gone. The property flag is not broken — it has
nothing left to unhide. **Setting it is not the missing piece; the plug-in is.**

### 3. `devicectl device capture screen-record` — UNSUPPORTED

```
ERROR: The capability "Screen Recording" is not supported by this device.
       CapabilityFeatureIdentifier = com.apple.coredevice.feature.screenrecording
```

The device's own capability list (64 entries) confirms it. Notably it *does*
advertise `com.apple.coredevice.feature.startvideooutput` and
`com.apple.coredevice.feature.viewdevicescreen` — so the hardware can stream, and
some Apple-internal client can consume it. `devicectl` exposes no CLI for either.

### 4. `devicectl device capture screenshot` — WORKS, 2.2fps ceiling

Native resolution 1206×2622 PNG over USB, ~460ms each. Reliable.

Concurrency does not help: six parallel invocations returned **one** file in
0.62s. The device serialises screenshot requests and drops the losers. 2.2fps is
a hard ceiling, not a tuning parameter.

This is enough to read Phase 0 test outcomes (see `phase0.py`) and nowhere near
enough for the planner, which needs ~60fps.

### 5. Capturing a mirrored window off the Mac's own screen — BLOCKED, UNVERIFIED

If QuickTime or AirPlay can still display the phone's screen in a macOS window,
`ffmpeg -f avfoundation -i "3"` (Capture screen 0) could grab that window at
60fps. This adds a compositing hop and unknown latency, which matters a great
deal for a project whose whole objective function is latency-sensitive.

Untested, because ffmpeg screen capture hung for 2 minutes on a `-t 2` capture
with no output — the signature of a blocked TCC **Screen Recording** prompt.
Granting that permission is a prerequisite before this path can even be assessed.

---

### 6. AirPlay + ScreenCaptureKit window capture — DEAD END

Attempted, because AirPlay Receiver renders fullscreen on its own Space and
`ffmpeg -i "Capture screen 0"` follows whichever Space is *displayed* — so the
moment the operator switches back to their desktop the capture silently records
the desktop instead of the phone.

`tools/screencap/wincap.swift` uses `SCContentFilter(desktopIndependentWindow:)`,
which does keep delivering frames for windows on inactive Spaces. It builds, it
enumerates windows across Spaces correctly, and Screen Recording permission is
inherited from the parent process. It still cannot solve this:

**AirPlay to a Mac only receives while its window is open and frontmost, and the
receiver window cannot be un-fullscreened.** Switching away does not merely hide
the window — the mirroring session stops. There is no stream left to capture, so
no capture API can help. This is a property of AirPlay Receiver, not a limitation
of ScreenCaptureKit.

Consequence: **AirPlay and using the Mac are mutually exclusive, permanently.**
Plan around it rather than trying to engineer past it.

`wincap` is kept because it is correct and would work against any real window
source (a capture-card preview, a windowed mirror on some future OS), but it is
NOT a path to using AirPlay in the background.

### 7. ReplayKit broadcast extension — NOT ATTEMPTED, the real fix

An on-device Broadcast Upload Extension captures the whole screen system-wide at
up to 60fps and would stream frames off the phone over the same iproxy socket the
gesture runner already uses (device listens, host connects — proven at 2ms RTT).
It never touches the Mac's display, so the operator keeps their machine.

Note this is NOT what CLAUDE.md:146 rejects. That rules out *WDA screenshots*,
a per-frame request/response API that tops out at 2-5fps. A broadcast extension
is a continuous hardware-backed pipeline; the objection does not transfer.

Costs: a host app target plus an extension target, one more App ID, and one more
of the three free-provisioning app slots. Extensions also run under a hard ~50MB
memory limit, so frames must be downscaled/encoded and forwarded, never buffered.

## Working arrangement (2026-08-16)

Given the above, the project runs split:

| Activity | Source | Mac usable? |
|---|---|---|
| Calibration, state-classifier checks, frame inspection, Phase 0 tests | `devicectl` screenshots, 2.2fps (`phase0.py`) | **yes** |
| Live planner runs, p̂ measurement, overnight grind | AirPlay mirror, ~30fps | no — dedicated |

The 2.2fps path is sufficient for everything except the planner loop itself.

## Status

| Path | 60fps? | Status |
|---|---|---|
| ffmpeg avfoundation → iPhone screen | — | dead, no DAL plug-in |
| native AVCaptureDevice + CMIO flag | — | dead, same cause |
| `devicectl` screen-record | — | capability unsupported |
| `devicectl` screenshot | no, 2.2fps | **works**, used by `phase0.py` |
| mirror-to-Mac + screen capture | maybe | blocked on TCC permission |
| HDMI capture card over USB-C AV adapter | yes | hardware not on hand |

**Nothing in this project's perception, planning, calibration or p̂ measurement
can proceed until one of the 60fps rows turns green.** `phase0.py` is unblocked
and sufficient for Phase 0 only.

Do not "make progress" by running `search.py` in the meantime. `search.py`'s own
docstring is explicit that sim numbers are worthless until validated against
device p̂ within ~30% relative, and device p̂ needs capture. Tuning against an
unvalidated sim produces confident numbers that mean nothing.
