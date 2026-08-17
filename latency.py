"""Measure TRUE end-to-end input latency: gesture issued -> pixels change.

THE QUESTION THIS ANSWERS

taps.py reports that a tap blocks for ~505ms inside the runner. ios/README.md
calls that "the floor on latency". That is very likely wrong, and the difference
decides whether this project is viable.

XCUITest's .tap() returns when it believes the app has settled. Crossy Road is a
Unity title rendering continuously at 60fps and arguably never settles, so the
505ms may be almost entirely a post-delivery quiescence wait — the touch landing
early and .tap() sitting on its hands afterwards. If so, true input latency is
far below 505ms, params.yaml's latency_offset_ms range of [80, 320] is fine, and
the planner works as designed. If instead pixels really do take ~505ms to move,
latency_offset_ms is ~2x above the top of the CEM search space and the whole
latency-compensation model needs rebuilding.

Only pixels can tell them apart, hence this.

HOW

The phone is AirPlay-mirrored to this Mac, so the screen capture contains the
phone. Frames are timestamped on ARRIVAL with the same monotonic clock used to
stamp the outgoing gesture, so the subtraction is meaningful without any clock
sync between host and device.

The number produced is end-to-end and INCLUSIVE of the AirPlay encode/transmit/
decode pipeline. That is deliberate: CLAUDE.md invariant 8 defines latency as
observation staleness, and under AirPlay the mirror hop is genuinely part of how
stale the planner's view is. It is an upper bound on what a capture card would
give, and the gap between them is exactly the cost of choosing AirPlay.

Requires: phone mirroring to this Mac, iproxy up, GestureRunner running.

    python latency.py --trials 8
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

import cv2
import numpy as np

import taps

log = logging.getLogger("latency")

SCREEN_DEVICE = "3"          # "Capture screen 0" in ffmpeg's avfoundation list
PHONE_ASPECT = 1206 / 2622   # native iPhone 16 Pro portrait
ASPECT_TOL = 0.03
PROBE_W, PROBE_H = 200, 434  # downscale for diffing; detail is irrelevant here

#: Where to tap for a timed event. Chosen so it is valid in BOTH states we can be
#: in: on the game-over screen it is the play button, and during a live run it is
#: an ordinary forward hop. Both produce a large visual change, which is all the
#: measurement needs.
#:
#: taps.py's default of (0.5, 0.62) is NOT usable here — on the game-over screen
#: it lands in the gap between the FREE GIFT banner and the buttons, hits nothing,
#: and reads identically to "the gesture never arrived".
#:
#: Do not drift this upward. y≈0.46 and y≈0.53 are the FREE and FREE GIFT
#: banners, which launch rewarded video.
TAP_XY = (0.5, 0.91)


@dataclass
class Crop:
    x: int
    y: int
    w: int
    h: int

    def vf(self) -> str:
        return f"crop={self.w}:{self.h}:{self.x}:{self.y},scale={PROBE_W}:{PROBE_H}"


def _grab_screen(path: str) -> Optional[np.ndarray]:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "avfoundation",
         "-capture_cursor", "0", "-framerate", "30", "-i", SCREEN_DEVICE,
         "-frames:v", "1", "-y", path],
        capture_output=True,
    )
    return cv2.imread(path)


def find_phone(path: str) -> Optional[Crop]:
    """Locate the mirrored phone as the bright region with the phone's aspect.

    A density threshold rather than any-nonzero-pixel: the AirPlay space is not
    perfectly black and a single stray lit pixel in a corner otherwise stretches
    the bounding box across the whole display.
    """
    img = _grab_screen(path)
    if img is None:
        return None
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    m = g > 12
    cols = np.where(m.mean(axis=0) > 0.05)[0]
    rows = np.where(m.mean(axis=1) > 0.05)[0]
    if not len(cols) or not len(rows):
        return None
    x0, x1, y0, y1 = int(cols[0]), int(cols[-1]), int(rows[0]), int(rows[-1])
    w, h = x1 - x0 + 1, y1 - y0 + 1
    aspect = w / h
    if abs(aspect - PHONE_ASPECT) > ASPECT_TOL:
        log.debug("content aspect %.3f != phone %.3f — not mirroring yet",
                  aspect, PHONE_ASPECT)
        return None
    # ffmpeg's crop filter rejects odd dimensions on some pixel formats.
    return Crop(x0, y0, w - (w % 2), h - (h % 2))


class Probe:
    """Cropped rawvideo off screen capture, timestamped as each frame arrives."""

    def __init__(self, crop: Crop, fps: int = 60):
        self.crop = crop
        self.fps = fps
        self.proc: Optional[subprocess.Popen] = None
        self.frames: List[Tuple[float, np.ndarray]] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def _cmd(self) -> List[str]:
        # -fps_mode passthrough is load-bearing. Without it ffmpeg emits
        # duplicated frames as fast as the CPU allows rather than being paced by
        # the capture device: measured 10,967 frames/sec, 2.8GB/s, 99.99% of them
        # byte-identical to their predecessor. Every timing derived from such a
        # stream is fiction — the "first changed frame" is whichever duplicate
        # happened to straddle the real update, so latency reads as ~0ms.
        #
        # -framerate is also omitted deliberately: the device rejects it
        # ("Configuration of video device failed, falling back to default") and
        # passing it only adds a misleading warning.
        return ["ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "avfoundation", "-capture_cursor", "0",
                "-i", SCREEN_DEVICE,
                "-vf", self.crop.vf(), "-pix_fmt", "bgr24",
                "-fps_mode", "passthrough",
                "-f", "rawvideo", "-"]

    def _read(self) -> None:
        n = PROBE_W * PROBE_H * 3
        assert self.proc and self.proc.stdout
        while not self._stop.is_set():
            buf = self.proc.stdout.read(n)
            if not buf or len(buf) < n:
                break
            t = time.monotonic()
            img = np.frombuffer(buf, np.uint8).reshape(PROBE_H, PROBE_W, 3)
            with self._lock:
                self.frames.append((t, img))

    def since(self, t: float) -> List[Tuple[float, np.ndarray]]:
        with self._lock:
            return [f for f in self.frames if f[0] >= t]

    def __enter__(self) -> "Probe":
        self.proc = subprocess.Popen(self._cmd(), stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL,
                                     bufsize=PROBE_W * PROBE_H * 3 * 4)
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self._thread:
            self._thread.join(timeout=2)


def ambient(probe: "Probe", window: float = 0.8) -> float:
    """Mean frame-to-frame delta over the last `window` seconds."""
    fs = probe.since(time.monotonic() - window)
    if len(fs) < 3:
        return 0.0
    return float(np.mean([
        np.abs(b[1].astype(np.int16) - a[1].astype(np.int16)).mean()
        for a, b in zip(fs, fs[1:])
    ]))


def wait_animated(probe: "Probe", floor: float = 3.0, timeout: float = 50.0) -> bool:
    """Block until the screen is actually moving.

    Crossy Road animates traffic on every playable screen including game-over, so
    ambient delta sits around 10. An interstitial ad is a still image and sits
    near 0.25. Gating on motion means trials only fire when the game can actually
    respond, instead of burning attempts into an ad and calling it a failure.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ambient(probe) > floor:
            return True
        time.sleep(0.5)
    return False


def measured_fps(frames: List[Tuple[float, np.ndarray]]) -> float:
    if len(frames) < 2:
        return 0.0
    span = frames[-1][0] - frames[0][0]
    return (len(frames) - 1) / span if span > 0 else 0.0


def run_trials(probe: Probe, client: taps.TapClient, trials: int,
               settle: float = 1.2) -> List[float]:
    """Fire a tap, then find the first frame-to-frame delta SPIKE.

    The obvious detector — compare each frame to a static pre-tap reference and
    threshold on absolute difference — does not work on this game, and fails in a
    way that looks like "the gesture never landed".

    Crossy Road animates continuously: traffic keeps moving even on the game-over
    screen, so every frame differs from any fixed reference and the difference
    grows without bound as cars drive on. A threshold set as a multiple of that
    "noise" lands around 60 mean-|diff|, while an actual hop only moves the whole-
    frame mean by 10-20. The bar sits permanently above the signal.

    What distinguishes a hop is not magnitude against a reference but a
    discontinuity: traffic produces a steady frame-to-frame delta, and a hop
    scrolls the entire camera in one step, spiking that delta well above its
    running level. So the baseline is the distribution of consecutive deltas
    during idle, and the event is the first delta that breaks out of it.
    """
    out: List[float] = []
    misses = 0
    i = -1
    attempts = 0
    while len(out) < trials and attempts < trials * 4:
        i += 1
        attempts += 1
        time.sleep(settle)
        if not wait_animated(probe):
            # Static screen. Overwhelmingly an interstitial: reality-check §3 puts
            # them at roughly one per two runs, and a still ad is indistinguishable
            # from a frozen mirror by pixel statistics alone.
            log.warning("trial %d: screen static — ad or stall, nudging app", i)
            client.activate()
            continue
        base = probe.since(time.monotonic() - 0.8)
        if len(base) < 6:
            log.warning("trial %d: too few baseline frames, skipping", i)
            continue
        deltas = [
            float(np.abs(b[1].astype(np.int16) - a[1].astype(np.int16)).mean())
            for a, b in zip(base, base[1:])
        ]
        mu, sd = float(np.mean(deltas)), float(np.std(deltas))
        # Floor the threshold at 35% above ambient so a very steady animation
        # (tiny sd) cannot make the detector hair-trigger.
        thresh = mu + max(4.0 * sd, 0.35 * mu)

        t0 = time.monotonic()
        client.tap(*TAP_XY)
        time.sleep(2.0)   # collect, then analyse offline — no polling races

        fs = probe.since(t0 - 0.15)
        found = None
        for a, b in zip(fs, fs[1:]):
            if b[0] < t0:
                continue
            d = float(np.abs(b[1].astype(np.int16) - a[1].astype(np.int16)).mean())
            if d > thresh:
                found = (b[0] - t0) * 1000.0
                break

        if found is None:
            misses += 1
            log.warning("trial %d: no delta spike (ambient mu=%.2f thresh=%.2f)",
                        i, mu, thresh)
            # Distinguish the two causes rather than guessing. A dropped mirror
            # means the crop now frames desktop wallpaper and every later number
            # is garbage; a live mirror showing an unresponsive screen just means
            # keep waiting. Earlier this aborted on the first three misses and
            # blamed the mirror for what was actually an interstitial.
            if misses >= 2:
                if find_phone("/tmp/_lat_recheck.png") is None:
                    raise SystemExit(
                        "aborting: mirror is gone (crop no longer contains a "
                        "phone-shaped region). Re-mirror and rerun; do NOT trust "
                        "a median assembled from frames captured before it dropped."
                    )
                log.info("  mirror still up — treating as unresponsive screen")
                misses = 0
                client.activate()
        else:
            misses = 0
            log.info("trial %d: %.0f ms (ambient mu=%.2f thresh=%.2f)",
                     i, found, mu, thresh)
            out.append(found)
    return out


def diagnose(crop: Crop, out: str = "debug/mirror_check.png") -> None:
    """Prove the probe sees live, changing pixels before trusting any timing.

    Two failure modes are indistinguishable from a bare "no visible change":
    the mirror stopped (crop now shows wallpaper), or gestures are not reaching
    the game. This dumps frames so the difference is visible rather than inferred.
    """
    from pathlib import Path
    Path(out).parent.mkdir(parents=True, exist_ok=True)

    with Probe(crop) as probe:
        time.sleep(2.0)
        with taps.TapClient() as client:
            client.activate()
            time.sleep(2.5)
            for _ in range(5):
                client.tap()
                time.sleep(1.0)
        time.sleep(0.5)

    frames = probe.frames
    if not frames:
        raise SystemExit("probe produced zero frames")

    distinct = sum(
        1 for a, b in zip(frames, frames[1:])
        if not np.array_equal(a[1], b[1])
    )
    fps = measured_fps(frames)
    log.info("frames=%d  fps=%.1f  distinct-consecutive=%d (%.0f%%)",
             len(frames), fps, distinct, 100.0 * distinct / max(1, len(frames) - 1))

    ref = frames[0][1].astype(np.int16)
    diffs = [float(np.abs(f[1].astype(np.int16) - ref).mean()) for f in frames]
    log.info("frame-vs-first mean|diff|: min=%.2f med=%.2f max=%.2f",
             min(diffs), sorted(diffs)[len(diffs) // 2], max(diffs))

    idx = np.linspace(0, len(frames) - 1, 12).astype(int)
    tiles = [cv2.resize(frames[i][1], (PROBE_W, PROBE_H)) for i in idx]
    for t, i in zip(tiles, idx):
        cv2.putText(t, f"{(frames[i][0]-frames[0][0])*1000:.0f}ms", (4, 16),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 255), 1)
    rows = [np.hstack(tiles[i:i + 6]) for i in range(0, 12, 6)]
    cv2.imwrite(out, np.vstack(rows))
    log.info("wrote %s", out)


def main() -> None:
    ap = argparse.ArgumentParser(description="Measure true end-to-end input latency")
    ap.add_argument("--trials", type=int, default=8)
    ap.add_argument("--wait", type=int, default=180,
                    help="seconds to wait for the phone to start mirroring")
    ap.add_argument("--diagnose", action="store_true",
                    help="dump frames + change stats instead of timing anything")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    log.info("waiting for AirPlay mirror (up to %ds)...", args.wait)
    crop = None
    deadline = time.time() + args.wait
    while time.time() < deadline:
        crop = find_phone("/tmp/_lat_probe.png")
        if crop:
            break
        time.sleep(3)
    if not crop:
        raise SystemExit("phone never appeared — is Screen Mirroring on?")
    log.info("phone at %dx%d+%d+%d", crop.w, crop.h, crop.x, crop.y)

    if args.diagnose:
        diagnose(crop)
        return

    with Probe(crop) as probe:
        time.sleep(3.0)
        if not probe.frames:
            raise SystemExit("no frames from screen capture")
        with taps.TapClient() as client:
            client.activate()
            time.sleep(3.0)
            results = run_trials(probe, client, args.trials)
        fps = measured_fps(probe.frames)

    log.info("")
    log.info("capture fps: %.1f over %d frames", fps, len(probe.frames))
    if results:
        results.sort()
        med = results[len(results) // 2]
        log.info("end-to-end latency: median %.0f ms  min %.0f  max %.0f  (n=%d)",
                 med, results[0], results[-1], len(results))
        log.info("runner-side .tap() block was ~505 ms; delta = %.0f ms", 505 - med)
    else:
        log.error("no successful trials")


if __name__ == "__main__":
    main()
