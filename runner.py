"""Main loop, menu state machine, kill switch, heartbeat.

The kill switch is three independent layers and any one failing must not defeat
the cap:

  1. stop tapping at THRESHOLD — the eagle ends the run naturally
  2. a lockfile that permanently retires the bot
  3. teardown of the tap channel entirely

**Stop tapping rather than force-quitting.** The run has to end in-game for the
score to submit to Game Center; force-quitting mid-run loses it. This is the
whole point of the exercise, so it is the one thing that must not be clever.
"""

from __future__ import annotations

import argparse
import csv
import logging
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import cv2
import numpy as np

import analyze
from analyze import DEATHS_CSV, DEATH_FIELDS, RUNS_CSV, RUN_FIELDS
from capture import AVFoundationCapture, FrameSource, find_iphone_index
from config import (
    DEBUG_DIR,
    HEARTBEAT,
    LOCKFILE,
    LOGS,
    MAX_RUNS,
    THRESHOLD,
    Calib,
    Params,
    load_calib,
    load_params,
)
from debug import DebugState, RingBuffer, dump_death, dump_unknown
from perceive import Perceiver
from plan import Planner
from states import (
    AD_TIMEOUT_S,
    STATE_TIMEOUT_S,
    UNKNOWN_TIMEOUT_S,
    EventModeGuard,
    Screen,
    ScreenClassifier,
    find_close_buttons,
)
from taps import TapClient
from world import LaneType

log = logging.getLogger("runner")

#: Consecutive 2s frame-source timeouts before the runner gives up. The heartbeat
#: is only written on a real frame, so this bounds how long a dead capture can
#: masquerade as a live one.
MAX_FRAME_STARVATION = 8


@dataclass
class RunStats:
    run_id: str
    started: float
    rows: Dict[str, int] = field(default_factory=lambda: {"grass": 0, "road": 0, "water": 0, "track": 0})
    boxed_ticks: int = 0
    squeeze_ticks: int = 0
    ads_seen: int = 0
    unknown_screens: int = 0
    frames: int = 0


class KillSwitchTripped(Exception):
    """Raised once the threshold is reached. Not an error — the goal."""


class Runner:
    def __init__(
        self,
        source: FrameSource,
        taps: Optional[TapClient],
        calib: Calib,
        params: Params,
        max_runs: int = MAX_RUNS,
        dry_run: bool = False,
    ):
        self.source = source
        self.taps = taps
        self.calib = calib
        self.params = params
        self.max_runs = max_runs
        self.dry_run = dry_run

        self.perceiver = Perceiver(calib)
        self.planner = Planner(params)
        self.classifier = ScreenClassifier(calib)
        self.event_guard = EventModeGuard(calib)
        self.ring = RingBuffer()

        self.runs_done = 0
        self.stats: Optional[RunStats] = None
        self.stopping = False
        self.threshold_reached = False

        self._state_since = time.monotonic()
        self._last_screen = Screen.UNKNOWN
        self._tick = 0
        self._last_action_t = 0.0

    # -- lifecycle ---------------------------------------------------------

    def run(self) -> int:
        if LOCKFILE.exists():
            log.info("lockfile present: %s", LOCKFILE.read_text().strip())
            print("Already succeeded. Bot is retired.")
            return 0

        LOGS.mkdir(parents=True, exist_ok=True)
        HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_csv(DEATHS_CSV, DEATH_FIELDS)
        self._ensure_csv(RUNS_CSV, RUN_FIELDS)
        self._new_run()

        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)

        try:
            starved = 0
            while not self.stopping and self.runs_done < self.max_runs:
                frame = self.source.latest(timeout=2.0)
                if frame is None:
                    # Give up rather than spin. A dead capture is not something
                    # this process can fix, and spinning here would keep the
                    # heartbeat fresh — so the supervisor would see a healthy
                    # runner and never restart anything.
                    starved += 1
                    log.warning("no frame for 2s (%d consecutive)", starved)
                    if starved >= MAX_FRAME_STARVATION:
                        log.error("capture produced nothing for %.0fs — exiting so the "
                                  "supervisor can restart the pipeline",
                                  2.0 * MAX_FRAME_STARVATION)
                        return 2
                    continue
                starved = 0
                HEARTBEAT.write_text(str(time.time()))
                self._tick += 1
                self._dispatch(frame.img, frame.t_ms)
        except KillSwitchTripped:
            return self._succeed()
        finally:
            self._teardown()
        return 0

    def _on_signal(self, *_) -> None:
        log.info("signal received, stopping after this tick")
        self.stopping = True

    # -- state machine -----------------------------------------------------

    def _dispatch(self, img: np.ndarray, t_ms: float) -> None:
        cls = self.classifier.classify(img)
        if cls.screen is not self._last_screen:
            log.info("screen %s -> %s (%s)", self._last_screen.value, cls.screen.value, cls.detail)
            self._last_screen = cls.screen
            self._state_since = time.monotonic()
        held = time.monotonic() - self._state_since

        if cls.screen is Screen.GAMEPLAY:
            self._play(img, t_ms, cls.detail)
            return

        # Universal escape. Any non-gameplay state that outlives its budget gets
        # dumped and relaunched, whatever we labelled it. Ad creatives are
        # adversarial and the AD/GACHA/UNKNOWN boundary is genuinely fuzzy —
        # hanging the only timeout off one branch just relocates the 2am stall to
        # whichever state we got wrong.
        budget = STATE_TIMEOUT_S.get(cls.screen, UNKNOWN_TIMEOUT_S)
        if held > budget:
            if cls.screen in (Screen.UNKNOWN, Screen.AD):
                if self.stats:
                    self.stats.unknown_screens += 1
                path = dump_unknown(img, f"stuck in {cls.screen.value} for {held:.0f}s: {cls.detail}")
                log.warning("stuck in %s for %.0fs, dumped %s", cls.screen.value, held, path)
            self._relaunch(f"{cls.screen.value} held {held:.0f}s > {budget:.0f}s")
            return

        if cls.screen is Screen.DEATH:
            self._on_death(img)
        elif cls.screen is Screen.AD:
            self._handle_ad(img, held)
        else:
            # MENU, GACHA, CRASHED and anything unlabelled: tap through and let
            # the budget above catch it if that does not work.
            if cls.screen is not Screen.UNKNOWN:
                self._tap_centre()

    # -- gameplay ----------------------------------------------------------

    def _play(self, img: np.ndarray, t_ms: float, detail: str) -> None:
        if self.event_guard.baseline is None:
            self.event_guard.calibrate(img)
        elif self.event_guard.looks_flipped(img):
            # Hopside Down or another mode that replaces the main game. Grinding
            # through this produces garbage all night; fail loudly instead.
            dump_unknown(img, f"event mode suspected (flipped playfield): {detail}")
            log.error("playfield looks inverted — event mode active, not classic endless. "
                      "Stopping rather than grinding a mode we cannot play.")
            self.stopping = True
            return

        obs = self.perceiver.perceive(img, t_ms)
        score = obs.chicken_row

        # --- kill switch layer 1: stop tapping ---------------------------
        if score >= THRESHOLD:
            if not self.threshold_reached:
                log.info("THRESHOLD %d reached at score %d — ceasing input, "
                         "letting the run end naturally so the score submits", THRESHOLD, score)
                self.threshold_reached = True
            return          # no taps. The eagle will end it. Do not force-quit.

        if self.stats:
            self.stats.frames += 1

        if self._tick % max(1, self.params.replan_interval_frames):
            return          # do not execute a stale plan, but do not thrash either

        ms_since_forward = t_ms - self._last_action_t if self._last_action_t else 0.0
        res = self.planner.plan(
            obs.world, obs.chicken_row, obs.chicken_col,
            log_id=-1, ms_since_forward=ms_since_forward,
        )

        if self.stats:
            if res.boxed_in:
                self.stats.boxed_ticks += 1
            elif res.margin_used < self.params.safety_margin_ms:
                self.stats.squeeze_ticks += 1
            lane = obs.world.lane(obs.chicken_row).type.value
            if lane in self.stats.rows:
                self.stats.rows[lane] = max(self.stats.rows[lane], obs.chicken_row)

        self.ring.record(img, self._debug_state(obs, res, t_ms, score))

        if res.action != "wait":
            self._last_action_t = t_ms
            if not self.dry_run and self.taps:
                self.taps.act(res.action)
            self.perceiver.note_lateral(res.action)

    def _debug_state(self, obs, res, t_ms: float, score: int) -> DebugState:
        return DebugState(
            t_ms=t_ms,
            score=score,
            chicken_row=obs.chicken_row,
            chicken_col=obs.chicken_col,
            action=res.action,
            fps=getattr(self.source, "meter", None).fps if hasattr(self.source, "meter") else 0.0,
            latency_ms=(self.taps.measured_latency_ms(res.action, self.params.latency_offset_ms)
                        if self.taps else self.params.latency_offset_ms),
            margin_used=res.margin_used,
            boxed_in=res.boxed_in,
            panicking=res.panicking,
            screen="gameplay",
            lanes={r: l.type.value for r, l in obs.world.lanes.items()},
            obstacles=[
                {"row": o.row, "x": o.x, "width": o.width, "vx": o.vx}
                for l in obs.world.lanes.values() for o in l.obstacles
            ],
            plan=res.path,
        )

    # -- death -------------------------------------------------------------

    def _on_death(self, img: np.ndarray) -> None:
        if self.stats is None:
            self._tap_centre()
            return

        score = self.perceiver.chicken_row
        lane, cause = self._infer_cause()
        self._log_death(score, lane, cause)
        self._log_run(score)
        if len(self.ring):
            dump_death(self.ring, self.stats.run_id, cause, lane, self.calib)
        self.ring.clear()

        self.runs_done += 1
        log.info("run %d ended: score=%d cause=%s lane=%s", self.runs_done, score, cause, lane)

        if self.threshold_reached:
            raise KillSwitchTripped()

        self._tap_centre()
        self.perceiver.reset()
        self._new_run()

    def _infer_cause(self) -> tuple[str, str]:
        """Best guess from the last recorded tick. The ring dump is the real record."""
        if not len(self.ring):
            return "unknown", "unknown"
        _, st = self.ring.buf[-1]
        lane = st.lanes.get(st.chicken_row, "unknown")
        if st.panicking:
            return lane, "eagle"
        cause = {"water": "water_gap", "road": "car", "track": "train"}.get(lane, "unknown")
        return lane, cause

    # -- ads ---------------------------------------------------------------

    def _handle_ad(self, img: np.ndarray, held: float) -> None:
        """Hunt the close button, but never unboundedly.

        Close buttons appear on a delay, sit at variable positions, are sometimes
        deliberately tiny or misleading, and occasionally lead to a second
        screen. One network always slips through whatever else you do, so the
        timeout is the load-bearing part and the button hunt is the optimisation.
        """
        if self.stats and held < 0.2:
            self.stats.ads_seen += 1
        if held < 2.0:
            return          # close controls are usually delayed; tapping early does nothing

        for (cx, cy) in find_close_buttons(img)[:2]:
            if not self.dry_run and self.taps:
                self.taps.tap(cx / img.shape[1], cy / img.shape[0])
            log.debug("ad: tried close button at (%d, %d)", cx, cy)

    # -- unknown / recovery ------------------------------------------------

    def _relaunch(self, why: str) -> None:
        log.warning("relaunching app: %s", why)
        if not self.dry_run and self.taps:
            self.taps.activate()
        self._state_since = time.monotonic()
        self.perceiver.reset()

    def _tap_centre(self) -> None:
        if not self.dry_run and self.taps:
            self.taps.tap()

    # -- logging -----------------------------------------------------------

    @staticmethod
    def _ensure_csv(path: Path, fields: List[str]) -> None:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", newline="") as f:
                csv.writer(f).writerow(fields)

    def _new_run(self) -> None:
        self.stats = RunStats(run_id=uuid.uuid4().hex[:8], started=time.time())
        self._last_action_t = 0.0

    def _log_death(self, score: int, lane: str, cause: str) -> None:
        assert self.stats
        with DEATHS_CSV.open("a", newline="") as f:
            csv.writer(f).writerow([
                self.stats.run_id, score, score, lane, cause,
                self.params.hash, datetime.now().isoformat(timespec="seconds"),
            ])

    def _log_run(self, score: int) -> None:
        assert self.stats
        fps = getattr(self.source, "meter", None)
        with RUNS_CSV.open("a", newline="") as f:
            csv.writer(f).writerow([
                self.stats.run_id, score, self.params.hash,
                datetime.now().isoformat(timespec="seconds"),
                round(time.time() - self.stats.started, 1),
                round(fps.fps, 1) if fps else 0.0,
                self.stats.rows["grass"], self.stats.rows["road"],
                self.stats.rows["water"], self.stats.rows["track"],
                self.stats.boxed_ticks, self.stats.squeeze_ticks,
                self.stats.ads_seen, self.stats.unknown_screens,
            ])

    # -- kill switch -------------------------------------------------------

    def _succeed(self) -> int:
        score = self.perceiver.chicken_row
        # Layer 2: permanent disable.
        LOCKFILE.write_text(f"{score} @ {datetime.now().isoformat(timespec='seconds')}\n")
        log.info("wrote lockfile %s", LOCKFILE)
        print(f"\n*** {score} >= {THRESHOLD}. Bot retired. ***")
        print("Confirm the score posted to Game Center before deleting debug dumps.")
        return 0

    def _teardown(self) -> None:
        # Layer 3: kill the tap channel entirely.
        if self.taps:
            self.taps.close()
        self.source.stop()


def main() -> int:
    ap = argparse.ArgumentParser(description="Crossy Road autoplayer")
    ap.add_argument("--device-index", type=int, default=None)
    ap.add_argument("--max-runs", type=int, default=MAX_RUNS)
    ap.add_argument("--dry-run", action="store_true", help="perceive and plan, send no input")
    ap.add_argument("--no-preflight", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    if not args.no_preflight:
        import preflight
        if not preflight.run(quiet=True):
            print("Preflight failed. Run `python preflight.py` for detail, "
                  "or pass --no-preflight to override.")
            return 1

    calib = load_calib()
    params, _ = load_params()

    idx = args.device_index if args.device_index is not None else find_iphone_index()
    if idx is None:
        print("No capture device found. `ffmpeg -f avfoundation -list_devices true -i \"\"`")
        return 1

    cap = calib.capture
    source = AVFoundationCapture(
        idx, width=cap.get("scale_w", 886), height=cap.get("scale_h", 1920),
        fps=cap.get("fps", 60),
    ).start()

    taps = None
    if not args.dry_run:
        taps = TapClient()
        taps.connect()

    return Runner(source, taps, calib, params, max_runs=args.max_runs,
                  dry_run=args.dry_run).run()


if __name__ == "__main__":
    sys.exit(main())
