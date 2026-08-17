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


def find_iphone_index() -> Optional[int]:
    """Best-effort scrape of the device list. Verify by eye before trusting it."""
    for line in list_devices().splitlines():
        m = re.search(r"\[(\d+)\]\s+(.*(?:iPhone|Apple).*)", line, re.I)
        if m:
            log.info("found capture device: %s", m.group(2).strip())
            return int(m.group(1))
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
