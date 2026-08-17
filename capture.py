"""Frame source. Single-slot latest-frame queue — NEVER let it backlog.

INVARIANT 4. If the CV loop is slower than the capture rate and you read frames
in order, you fall progressively further behind and every decision is made on
older and older data. Nothing breaks; `p` just quietly degrades across a session,
which is indistinguishable from thermal throttle and from a dozen other things.

The fix is structural rather than disciplined: the reader thread keeps exactly
one frame. A slow consumer drops frames, it never lags.

Not WDA screenshots — 2-5 fps with on-device JPEG encode. A USB-tethered iPhone
appears to macOS as an AVCaptureDevice (this is what QuickTime's "Movie Recording
→ iPhone" uses): native resolution, ~60fps, low latency.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from collections import deque
from dataclasses import dataclass
from typing import Deque, Iterator, Optional, Tuple

import numpy as np

log = logging.getLogger("capture")


@dataclass
class Frame:
    img: np.ndarray          # HxWx3, BGR
    t_ms: float              # host monotonic clock at read time
    index: int


class FrameSource:
    """Interface shared by the device capture and the offline replay sources."""

    def start(self) -> "FrameSource":
        return self

    def latest(self, timeout: float = 1.0) -> Optional[Frame]:
        raise NotImplementedError

    def stop(self) -> None:
        pass

    def __enter__(self) -> "FrameSource":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


class FpsMeter:
    """Rolling frame rate. Log it per run — a sagging fps trend across a night is
    thermal throttle, not variance (runbook §10.4)."""

    def __init__(self, window: int = 120):
        self.stamps: Deque[float] = deque(maxlen=window)

    def tick(self, t_ms: float) -> None:
        self.stamps.append(t_ms)

    @property
    def fps(self) -> float:
        if len(self.stamps) < 2:
            return 0.0
        span = self.stamps[-1] - self.stamps[0]
        return 1000.0 * (len(self.stamps) - 1) / span if span > 0 else 0.0


def list_devices() -> str:
    """`ffmpeg -f avfoundation -list_devices true -i ""` — find the iPhone's index."""
    proc = subprocess.run(
        ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        capture_output=True, text=True,
    )
    return proc.stderr


#: Devices whose names contain "iPhone" but which are emphatically NOT the phone's
#: screen. A Continuity Camera is the phone's *camera*, pointed at your desk.
_NOT_THE_SCREEN = re.compile(r"desk view|continuity|camera|microphone", re.I)


def find_iphone_index() -> Optional[int]:
    """Locate the iPhone SCREEN in ffmpeg's avfoundation device list.

    Returns None far more often than the original version of this function did,
    and that is the point. The old regex accepted any device whose name matched
    /iPhone|Apple/, which on a Mac with Continuity Camera enabled matches
    "<name>'s iPhone Desk View Camera" — a webcam aimed at the desk. It would be
    selected silently, perception would run HSV lane classification on furniture,
    and nothing anywhere would raise. Refusing to guess is worth more than a
    plausible index.

    NOTE (measured 2026-08-16, macOS 27.0): on this machine no iPhone screen
    device is enumerable at all. /System/Library/CoreMediaIO/Plug-Ins/DAL/ no
    longer exists, so kCMIOHardwarePropertyAllowScreenCaptureDevices has nothing
    to unhide, and neither ffmpeg nor a native AVCaptureDevice discovery session
    can see the screen. See docs/capture-paths.md before spending time here.
    """
    for line in list_devices().splitlines():
        m = re.search(r"\[(\d+)\]\s+(.+)", line)
        if not m:
            continue
        name = m.group(2).strip()
        if not re.search(r"iphone", name, re.I):
            continue
        if _NOT_THE_SCREEN.search(name):
            log.debug("skipping %r — camera/mic, not the screen", name)
            continue
        log.info("found candidate screen device: %s", name)
        return int(m.group(1))
    log.error(
        "no iPhone screen device in the avfoundation list. This is expected on "
        "current macOS — the CoreMediaIO DAL path is gone. Do NOT fall back to a "
        "Continuity Camera; see docs/capture-paths.md."
    )
    return None


class AVFoundationCapture(FrameSource):
    """ffmpeg avfoundation -> rawvideo on stdout -> numpy, in a reader thread."""

    def __init__(
        self,
        device_index: int,
        width: int = 886,
        height: int = 1920,
        fps: int = 60,
        pix_fmt: str = "bgr24",
    ):
        self.device_index = device_index
        self.w, self.h = width, height
        self.fps = fps
        self.pix_fmt = pix_fmt
        self.proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        # The single slot. Guarded by _cv; only ever holds the newest frame.
        self._slot: Optional[Frame] = None
        self._cv = threading.Condition()
        self._count = 0
        self.dropped = 0
        self.meter = FpsMeter()

    def _cmd(self) -> list[str]:
        return [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "avfoundation",
            "-framerate", str(self.fps),
            "-i", str(self.device_index),
            "-vf", f"scale={self.w}:{self.h}",
            "-pix_fmt", self.pix_fmt,
            "-f", "rawvideo", "-",
        ]

    def start(self) -> "AVFoundationCapture":
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg not on PATH (brew install ffmpeg)")
        nbytes = self.w * self.h * 3
        self.proc = subprocess.Popen(
            self._cmd(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=nbytes * 4,
        )
        self._stop.clear()
        self._thread = threading.Thread(target=self._read_loop, args=(nbytes,), daemon=True)
        self._thread.start()
        return self

    def _read_loop(self, nbytes: int) -> None:
        assert self.proc and self.proc.stdout
        stream = self.proc.stdout
        while not self._stop.is_set():
            buf = stream.read(nbytes)
            if buf is None or len(buf) < nbytes:
                log.warning("capture stream ended (%s bytes)", len(buf) if buf else 0)
                break
            img = np.frombuffer(buf, np.uint8).reshape(self.h, self.w, 3)
            t = time.monotonic() * 1000.0
            self.meter.tick(t)
            with self._cv:
                if self._slot is not None:
                    # A frame the consumer never looked at. Counting these is how
                    # you learn the CV loop is too slow, rather than guessing.
                    self.dropped += 1
                self._count += 1
                self._slot = Frame(img=img, t_ms=t, index=self._count)
                self._cv.notify()

    def latest(self, timeout: float = 1.0) -> Optional[Frame]:
        """Block until a frame is available, then take the newest one and clear."""
        with self._cv:
            if self._slot is None:
                self._cv.wait(timeout)
            frame, self._slot = self._slot, None
            return frame

    def stop(self) -> None:
        self._stop.set()
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None
        if self._thread:
            self._thread.join(timeout=2)


#: ffmpeg's avfoundation index for "Capture screen 0". Verify with list_devices()
#: if the Mac's display configuration changes.
SCREEN_DEVICE = "3"

#: The phone's native portrait resolution. AirPlay mirrors at a SLIGHTLY smaller
#: size (measured 1188x2574 vs 1206x2622, ~0.985), so MirrorCapture rescales back
#: to native. That keeps calib.yaml expressed in one coordinate system regardless
#: of which capture path produced the frame — otherwise every calibrated pixel
#: value silently shifts by 1.5% the day the capture path changes, which is
#: exactly the kind of two-places-disagree bug CLAUDE.md's invariants are about.
NATIVE_W, NATIVE_H = 1206, 2622
_PHONE_ASPECT = NATIVE_W / NATIVE_H


class Crop:
    __slots__ = ("x", "y", "w", "h")

    def __init__(self, x: int, y: int, w: int, h: int):
        self.x, self.y, self.w, self.h = x, y, w, h

    def __repr__(self) -> str:
        return f"Crop({self.w}x{self.h}+{self.x}+{self.y})"


def find_mirrored_phone(tmp_png: str = "/tmp/_mirror_probe.png",
                        tol: float = 0.03) -> Optional[Crop]:
    """Locate an AirPlay-mirrored iPhone inside a screenshot of this Mac.

    Requires cv2, and requires the phone to actually be mirroring — AirPlay
    Receiver renders it fullscreen on its own Space, letterboxed in black.

    Matching on ASPECT rather than "the biggest bright blob" is deliberate: when
    the mirror is not up, the crop would otherwise silently frame desktop
    wallpaper and every downstream frame would be garbage that still looks like
    valid video. Requiring the phone's aspect ratio means "not mirroring" returns
    None instead of nonsense.
    """
    import cv2  # local import: only the mirror path needs it

    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "avfoundation",
         "-capture_cursor", "0", "-i", SCREEN_DEVICE, "-frames:v", "1",
         "-y", tmp_png],
        capture_output=True,
    )
    img = cv2.imread(tmp_png)
    if img is None:
        return None
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    lit = g > 12
    # Density threshold, not any-nonzero: the AirPlay space is not perfectly
    # black and one stray lit pixel stretches the bbox across the whole display.
    cols = np.where(lit.mean(axis=0) > 0.05)[0]
    rows = np.where(lit.mean(axis=1) > 0.05)[0]
    if not len(cols) or not len(rows):
        return None
    x0, x1 = int(cols[0]), int(cols[-1])
    y0, y1 = int(rows[0]), int(rows[-1])
    w, h = x1 - x0 + 1, y1 - y0 + 1
    if h <= 0 or abs(w / h - _PHONE_ASPECT) > tol:
        log.debug("content aspect %.3f != phone %.3f — not mirroring",
                  w / max(h, 1), _PHONE_ASPECT)
        return None
    # ffmpeg's crop filter rejects odd dimensions for some pixel formats.
    return Crop(x0, y0, w - (w % 2), h - (h % 2))


class MirrorCapture(AVFoundationCapture):
    """Frames of an AirPlay-mirrored iPhone, taken off this Mac's own screen.

    Exists because the direct path does not: Apple removed the CoreMediaIO DAL
    plug-in that exposed a tethered iPhone as an AVCaptureDevice, so
    find_iphone_index() can never succeed on current macOS. See
    docs/capture-paths.md for everything that was tried.

    Inherits the single-slot latest-frame queue from AVFoundationCapture, which
    is invariant 4 and matters more here than on the original path: the AirPlay
    hop adds its own buffering, so a backlog compounds staleness the planner
    cannot see.

    COST: this measures ~30fps, not the 60 CLAUDE.md assumes, and adds AirPlay
    encode/transmit/decode latency to observation staleness. Both are real and
    both hurt `p`. A UVC capture stick would remove them.
    """

    def __init__(self, crop: Crop, width: int = NATIVE_W, height: int = NATIVE_H,
                 fps: int = 60, pix_fmt: str = "bgr24"):
        super().__init__(device_index=0, width=width, height=height,
                         fps=fps, pix_fmt=pix_fmt)
        self.crop = crop

    def _cmd(self) -> list[str]:
        # -fps_mode passthrough is load-bearing. Without it ffmpeg emits
        # duplicated frames as fast as the CPU allows rather than at the rate the
        # device produces them — measured 10,967 frames/sec, 2.8GB/s, 99.99%
        # byte-identical to their predecessor. Every timestamp off such a stream
        # is fiction, and it silently makes latency look like ~0ms.
        #
        # -framerate is omitted on purpose: the device rejects it
        # ("Configuration of video device failed, falling back to default").
        return [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "avfoundation", "-capture_cursor", "0",
            "-i", SCREEN_DEVICE,
            "-vf", (f"crop={self.crop.w}:{self.crop.h}:{self.crop.x}:{self.crop.y},"
                    f"scale={self.w}:{self.h}"),
            "-pix_fmt", self.pix_fmt,
            "-fps_mode", "passthrough",
            "-f", "rawvideo", "-",
        ]


WINCAP = str(Path(__file__).resolve().parent / "tools" / "screencap" / "wincap")


class WindowCapture(FrameSource):
    """Frames of a single macOS window, via ScreenCaptureKit (tools/screencap/wincap).

    THE POINT: this keeps working while the operator uses the Mac.

    MirrorCapture crops "Capture screen 0", which is whatever Space is currently
    DISPLAYED. AirPlay Receiver puts the phone fullscreen on its own Space, so the
    instant you switch away to read a log or answer a message, that capture is
    silently recording your desktop instead of the phone — and every frame after
    that is garbage the harness cannot detect. It also means the machine is
    unusable for as long as the bot runs, which for an overnight grind that needs
    interactive debugging is not a real option.

    ScreenCaptureKit treats a window as a first-class object rather than a
    rectangle of the visible screen, so frames keep arriving while the window sits
    on an inactive Space or behind other windows.

    Output is rescaled to the phone's native resolution so calib.yaml means the
    same thing regardless of which source produced the frame.
    """

    def __init__(self, window_id: Optional[int] = None, scale: float = 1.0,
                 out_w: int = NATIVE_W, out_h: int = NATIVE_H,
                 owner: Optional[str] = None):
        self.window_id = window_id
        self.owner = owner
        self.scale = scale
        self.out_w, self.out_h = out_w, out_h
        self.src_w = self.src_h = 0
        #: Where the phone sits INSIDE the captured window. AirPlay Receiver runs
        #: fullscreen, so the window is screen-shaped (~1.55) with the phone
        #: letterboxed in black inside it — the window aspect is nothing like the
        #: phone's 0.46. Detected from the first frames rather than assumed,
        #: because the same code must also work when the window has been dragged
        #: out of fullscreen and is phone-shaped with no letterboxing at all.
        self.content: Optional[Crop] = None
        self._detect_failures = 0
        self.proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._slot: Optional[Frame] = None
        self._cv = threading.Condition()
        self._count = 0
        self.dropped = 0
        self.meter = FpsMeter()

    def _cmd(self) -> list[str]:
        cmd = [WINCAP]
        if self.window_id is not None:
            cmd += ["--window-id", str(self.window_id)]
        elif self.owner:
            cmd += ["--owner", self.owner]
        else:
            cmd += ["--find-phone"]
        if self.scale != 1.0:
            cmd += ["--scale", str(self.scale)]
        return cmd

    @staticmethod
    def _find_content(img: np.ndarray, tol: float = 0.03) -> Optional[Crop]:
        """Locate the phone inside a possibly-letterboxed window frame.

        Returns None rather than a best guess when nothing phone-shaped is
        present. A wrong crop here is invisible downstream — perception would run
        happily on a slice of black bars and report no error at all.
        """
        import cv2
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        lit = g > 12
        cols = np.where(lit.mean(axis=0) > 0.05)[0]
        rows = np.where(lit.mean(axis=1) > 0.05)[0]
        if not len(cols) or not len(rows):
            return None
        x0, x1 = int(cols[0]), int(cols[-1])
        y0, y1 = int(rows[0]), int(rows[-1])
        w, h = x1 - x0 + 1, y1 - y0 + 1
        if h <= 0 or abs(w / h - _PHONE_ASPECT) > tol:
            return None
        return Crop(x0, y0, w, h)

    def start(self) -> "WindowCapture":
        if not Path(WINCAP).exists():
            raise RuntimeError(
                f"{WINCAP} not built. "
                f"swiftc -O tools/screencap/wincap.swift -o {WINCAP}"
            )
        self.proc = subprocess.Popen(self._cmd(), stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
        # wincap announces its frame geometry on stderr before any pixels, because
        # a raw stream carries no header and the reader cannot reshape without it.
        assert self.proc.stderr
        for _ in range(40):
            line = self.proc.stderr.readline().decode(errors="replace").strip()
            if not line:
                if self.proc.poll() is not None:
                    raise RuntimeError("wincap exited before announcing geometry")
                continue
            m = re.search(r"(\d+)x(\d+)\s+BGRA", line)
            if m:
                self.src_w, self.src_h = int(m.group(1)), int(m.group(2))
                break
            log.info("wincap: %s", line)
        if not self.src_w:
            raise RuntimeError("wincap never announced frame geometry")
        log.info("window capture %dx%d -> %dx%d",
                 self.src_w, self.src_h, self.out_w, self.out_h)

        self._stop.clear()
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()
        return self

    def _read_loop(self) -> None:
        import cv2
        nbytes = self.src_w * self.src_h * 4      # BGRA
        assert self.proc and self.proc.stdout
        stream = self.proc.stdout
        while not self._stop.is_set():
            buf = stream.read(nbytes)
            if buf is None or len(buf) < nbytes:
                log.warning("wincap stream ended (%d bytes)", len(buf) if buf else 0)
                break
            img = np.frombuffer(buf, np.uint8).reshape(self.src_h, self.src_w, 4)
            img = img[:, :, :3]                    # drop alpha -> BGR

            if self.content is None:
                self.content = self._find_content(img)
                if self.content is not None:
                    log.info("phone content inside window at %r", self.content)
                else:
                    self._detect_failures += 1
                    # Splash screens and transitions can be near-black, so a few
                    # failures are normal at startup. Sustained failure means the
                    # window does not contain a phone and every frame we emit
                    # would be something else entirely — say so loudly.
                    if self._detect_failures in (30, 300):
                        log.warning(
                            "no phone-shaped content found in the captured window "
                            "after %d frames — is this actually the mirror?",
                            self._detect_failures)
                    continue

            c = self.content
            img = img[c.y:c.y + c.h, c.x:c.x + c.w]
            if img.shape[1] != self.out_w or img.shape[0] != self.out_h:
                img = cv2.resize(img, (self.out_w, self.out_h))
            t = time.monotonic() * 1000.0
            self.meter.tick(t)
            with self._cv:
                if self._slot is not None:
                    self.dropped += 1
                self._count += 1
                self._slot = Frame(img=img, t_ms=t, index=self._count)
                self._cv.notify()

    def latest(self, timeout: float = 1.0) -> Optional[Frame]:
        with self._cv:
            if self._slot is None:
                self._cv.wait(timeout)
            frame, self._slot = self._slot, None
            return frame

    def stop(self) -> None:
        self._stop.set()
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None
        if self._thread:
            self._thread.join(timeout=2)


def open_capture(wait_s: float = 0.0) -> FrameSource:
    """Best available frame source, preferring the direct path if it ever returns.

    find_iphone_index() is tried first so this keeps working unchanged on a
    machine with a UVC capture stick or a restored DAL plug-in, where the phone
    is a real AVCaptureDevice and none of the AirPlay compromises apply.
    """
    idx = find_iphone_index()
    if idx is not None:
        log.info("using direct AVFoundation device %d", idx)
        return AVFoundationCapture(idx)

    deadline = time.monotonic() + wait_s
    while True:
        # WindowCapture first: it survives the operator switching Spaces, so the
        # Mac stays usable while the bot runs. MirrorCapture is the fallback and
        # requires the AirPlay Space to remain frontmost for the whole session.
        if Path(WINCAP).exists():
            try:
                return WindowCapture().start()
            except RuntimeError as e:
                log.debug("window capture unavailable: %s", e)
        crop = find_mirrored_phone()
        if crop:
            log.warning("falling back to screen-crop capture — this breaks if you "
                        "switch away from the AirPlay Space")
            return MirrorCapture(crop)
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "no frame source: no direct capture device (expected on current "
                "macOS, see docs/capture-paths.md), and no mirrored phone window "
                "found. Start Screen Mirroring on the phone."
            )
        time.sleep(2.0)


class ArraySource(FrameSource):
    """Replay a list of frames. For tests and for re-running a death offline."""

    def __init__(self, frames: list[np.ndarray], fps: float = 60.0):
        self.frames = frames
        self.dt = 1000.0 / fps
        self.i = 0
        self.meter = FpsMeter()
        self.dropped = 0

    def latest(self, timeout: float = 1.0) -> Optional[Frame]:
        if self.i >= len(self.frames):
            return None
        t = self.i * self.dt
        self.meter.tick(t)
        f = Frame(img=self.frames[self.i], t_ms=t, index=self.i)
        self.i += 1
        return f


def measure_fps(source: FrameSource, seconds: float = 5.0) -> float:
    """Phase 2: measure it. Do NOT assume 120 — design to the measured number.

    Crossy Road is a 2014 Unity title and is almost certainly 60fps-capped; the
    AVFoundation device path typically delivers 60 regardless of panel refresh.
    """
    t0 = time.monotonic()
    n = 0
    while time.monotonic() - t0 < seconds:
        if source.latest(timeout=1.0):
            n += 1
    return n / (time.monotonic() - t0)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(list_devices())
    idx = find_iphone_index()
    if idx is None:
        raise SystemExit("no iPhone-looking capture device; pass the index manually")
    with AVFoundationCapture(idx) as cap:
        time.sleep(1.0)
        print(f"measured fps: {measure_fps(cap):.1f}  (dropped {cap.dropped})")
