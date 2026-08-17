"""Run a Phase 0 XCUITest while capturing the device screen, then tile the
frames so the result can be read without a human watching the phone.

WHY THIS EXISTS

SmokeTest.swift says outright that it cannot assert on Unity's internal state —
"you are the oracle". That is true of the test process, but it does not have to
be true of the operator. `devicectl device capture screenshot` pulls a native-res
PNG over USB in ~460ms, which is ~2.2fps: far too slow for the planner, and far
more than enough to see whether the chicken stepped left.

This is NOT the bot's capture path. capture.py wants 60fps rawvideo off an
AVFoundation device, and that is a different, unsolved problem — see the note in
docs/getting-started.md. This module exists so Phase 0 tests can be run and read
back unattended, one screenshot at a time.

CAVEAT ON TIMING

Frame timestamps are host-side, taken when the PNG lands, and each screenshot
costs ~460ms of unknown internal latency. They are good enough to order events
and to tell a lateral step from a forward hop. They are NOT good enough to
measure input latency — that needs the 240fps film ios/README.md asks for, and
nothing here substitutes for it.

Usage:
    python phase0.py test02_LateralSwipes
    python phase0.py test04_SwipeUpAsForward --fps 2.2
"""

from __future__ import annotations

import argparse
import logging
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import cv2
import numpy as np

log = logging.getLogger("phase0")

REPO = Path(__file__).resolve().parent
OUTDIR = REPO / "debug" / "phase0"
XCTESTRUN_GLOB = "ios/dd/Build/Products/CrossyRunner_iphoneos*.xctestrun"
SUITE = "CrossyRunnerUITests/SmokeTest"


@dataclass
class Shot:
    path: Path
    t_ms: float          # ms since capture start, host clock


def _udid() -> str:
    out = subprocess.run(["idevice_id", "-l"], capture_output=True, text=True)
    ids = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
    if not ids:
        raise SystemExit("no device from `idevice_id -l` — is it plugged in and unlocked?")
    return ids[0]


def screenshot(udid: str, dest: Path) -> bool:
    """One native-res PNG over USB. ~460ms. Returns False on any failure.

    Failures are expected and survivable: the device rejects a screenshot while
    it is mid-springboard-transition, and losing one frame of a 30-frame burst
    does not matter.
    """
    r = subprocess.run(
        ["xcrun", "devicectl", "device", "capture", "screenshot",
         "--device", udid, "--destination", str(dest), "-q"],
        capture_output=True, text=True,
    )
    return r.returncode == 0 and dest.exists()


class BurstCapture:
    """Screenshot in a loop on a background thread until told to stop.

    Deliberately dumb: no single-slot queue, no dropping. Every frame is kept,
    because at 2.2fps the whole point is that frames are scarce. capture.py's
    invariant-4 machinery is for the 60fps path and does not apply here.
    """

    def __init__(self, udid: str, outdir: Path, interval: float = 0.0):
        self.udid = udid
        self.outdir = outdir
        self.interval = interval
        self.shots: List[Shot] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._t0 = 0.0

    def _loop(self) -> None:
        i = 0
        while not self._stop.is_set():
            dest = self.outdir / f"f{i:04d}.png"
            t = (time.monotonic() - self._t0) * 1000.0
            if screenshot(self.udid, dest):
                self.shots.append(Shot(dest, t))
                i += 1
            if self.interval:
                self._stop.wait(self.interval)

    def __enter__(self) -> "BurstCapture":
        self.outdir.mkdir(parents=True, exist_ok=True)
        self._t0 = time.monotonic()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)


def run_test(name: str) -> subprocess.CompletedProcess:
    matches = sorted(REPO.glob(XCTESTRUN_GLOB))
    if not matches:
        raise SystemExit(f"no xctestrun at {XCTESTRUN_GLOB} — run ios/resign.sh first")
    return subprocess.run(
        ["xcodebuild", "test-without-building",
         "-xctestrun", str(matches[-1]),
         "-destination", f"platform=iOS,id={_udid()}",
         "-only-testing:" + f"{SUITE}/{name}"],
        capture_output=True, text=True,
    )


def sheet(shots: List[Shot], out: Path, cols: int = 6, tile=(240, 520),
          limit: int = 36) -> Optional[Path]:
    """Tile frames in time order, each labelled with its elapsed ms.

    The label is the whole point — without it you cannot line a frame up against
    the [phase0] synthesis timings in the test log, and ordering is the only
    thing distinguishing "stepped left" from "stepped right then left".
    """
    if not shots:
        return None
    picked = shots
    if len(shots) > limit:
        idx = np.linspace(0, len(shots) - 1, limit).astype(int)
        picked = [shots[i] for i in idx]
        log.warning("%d frames captured, sheet shows %d evenly spaced",
                    len(shots), limit)

    tiles = []
    for s in picked:
        img = cv2.imread(str(s.path))
        if img is None:
            continue
        img = cv2.resize(img, tile)
        cv2.rectangle(img, (0, 0), (tile[0], 26), (0, 0, 0), -1)
        cv2.putText(img, f"{s.t_ms:.0f}ms", (6, 19),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        tiles.append(img)
    if not tiles:
        return None

    while len(tiles) % cols:
        tiles.append(np.zeros((tile[1], tile[0], 3), np.uint8))
    rows = [np.hstack(tiles[i:i + cols]) for i in range(0, len(tiles), cols)]
    cv2.imwrite(str(out), np.vstack(rows))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Run a Phase 0 test with screen capture")
    ap.add_argument("test", help="e.g. test02_LateralSwipes")
    ap.add_argument("--interval", type=float, default=0.0,
                    help="seconds between shots; 0 = as fast as USB allows (~2.2fps)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for tool in ("xcrun", "idevice_id"):
        if shutil.which(tool) is None:
            raise SystemExit(f"{tool} not on PATH")

    stamp = time.strftime("%H%M%S")
    outdir = OUTDIR / f"{args.test}_{stamp}"
    udid = _udid()
    log.info("device %s -> %s", udid, outdir)

    with BurstCapture(udid, outdir, args.interval) as cap:
        proc = run_test(args.test)
    log.info("xcodebuild exit=%d, %d frames", proc.returncode, len(cap.shots))

    (outdir / "test.log").write_text(proc.stdout + proc.stderr)
    for line in proc.stdout.splitlines():
        if "[phase0]" in line or re.search(r"Test Case .* (passed|failed)", line):
            log.info("  %s", line.strip())

    s = sheet(cap.shots, outdir / "sheet.png")
    log.info("sheet: %s", s)


if __name__ == "__main__":
    main()
