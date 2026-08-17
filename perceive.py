"""HSV lane classification, blob tracking, 3-frame median velocity.

Flat-shaded voxels, a fixed orthographic camera, ~6 colours, fixed lighting.
HSV thresholds and connected components. No CNN — it would be a labelling
project with no upside (runbook A.5).

The orthographic camera is the gift that makes this tractable: no perspective
distortion, so screen-Y → world-row is a constant affine map, forever.

INVARIANT 5: this module is the *only* place pixels become world columns.
Everything downstream is in world units.

CAVEAT: the HSV ranges in calib.yaml are placeholders and every constant here
wants checking against a real contact sheet. `python debug.py --contact-sheet`
after any change, and actually look at the image before believing it.
"""

from __future__ import annotations

import logging
import statistics
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

from config import Calib
from world import Lane, LaneType, Obstacle, World

log = logging.getLogger("perceive")

#: Velocity needs three frames. A 2-frame difference turns one noisy centroid
#: into a wild estimate and then into an unexplained death (INVARIANT 2).
VELOCITY_FRAMES = 3


# --------------------------------------------------------------------------
# Lane classification
# --------------------------------------------------------------------------


class LaneClassifier:
    """Modal-hue classification of a horizontal strip per row."""

    def __init__(self, calib: Calib):
        self.calib = calib
        self.ranges = {
            LaneType.GRASS: calib.lanes.get("grass"),
            LaneType.ROAD: calib.lanes.get("road"),
            LaneType.WATER: calib.lanes.get("water"),
            LaneType.TRACK: calib.lanes.get("track"),
        }

    def classify_strip(self, hsv_strip: np.ndarray) -> Tuple[LaneType, float]:
        """Return the lane type with the largest in-range pixel share, and that share."""
        if hsv_strip.size == 0:
            return LaneType.UNKNOWN, 0.0
        total = hsv_strip.shape[0] * hsv_strip.shape[1]
        best, best_share = LaneType.UNKNOWN, 0.0
        for lane_type, r in self.ranges.items():
            if not r:
                continue
            lo = np.array([r["h"][0], r["s"][0], r["v"][0]], np.uint8)
            hi = np.array([r["h"][1], r["s"][1], r["v"][1]], np.uint8)
            share = float(cv2.inRange(hsv_strip, lo, hi).sum()) / (255.0 * total)
            if share > best_share:
                best, best_share = lane_type, share
        # A strip that matches nothing well is UNKNOWN, and the planner refuses
        # to enter UNKNOWN rather than gambling. Better a wasted hop than a
        # confident guess about a lane we cannot see.
        return (best, best_share) if best_share >= 0.35 else (LaneType.UNKNOWN, best_share)


# --------------------------------------------------------------------------
# Blob tracking
# --------------------------------------------------------------------------


@dataclass
class Track:
    """One tracked obstacle, with the history needed for a median velocity."""

    tid: int
    row: int
    x: float                                    # world columns
    width: float
    history: Deque[Tuple[float, float]] = field(default_factory=lambda: deque(maxlen=8))
    misses: int = 0

    def velocity(self) -> float:
        """Median of consecutive pairwise velocities. Never a 2-frame difference."""
        h = list(self.history)
        if len(h) < VELOCITY_FRAMES:
            return 0.0
        vels = []
        for (t0, x0), (t1, x1) in zip(h, h[1:]):
            dt = (t1 - t0) / 1000.0
            if dt > 1e-4:
                vels.append((x1 - x0) / dt)
        if len(vels) < 2:
            return 0.0
        return float(statistics.median(vels))


class BlobTracker:
    """Nearest-neighbour association within a gate radius, per lane row.

    Flickering boxes on a water contact sheet mean the gate radius is too tight.
    """

    def __init__(self, calib: Calib):
        self.calib = calib
        o = calib.obstacles
        self.min_area = o.get("min_area_px", 400)
        self.max_area = o.get("max_area_px", 60000)
        self.gate_cols = calib.px_to_cols(o.get("gate_radius_px", 90))
        self.tracks: Dict[int, List[Track]] = {}
        self._next_tid = 1

    def update(self, row: int, detections: List[Tuple[float, float]], t_ms: float) -> List[Track]:
        """`detections` is [(x_cols, width_cols)] for one row."""
        existing = self.tracks.get(row, [])
        unmatched = list(existing)
        out: List[Track] = []

        for x, w in detections:
            best, best_d = None, self.gate_cols
            for tr in unmatched:
                d = abs(tr.x - x)
                if d < best_d:
                    best, best_d = tr, d
            if best is None:
                best = Track(tid=self._next_tid, row=row, x=x, width=w)
                self._next_tid += 1
            else:
                unmatched.remove(best)
            best.x, best.width, best.misses = x, w, 0
            best.history.append((t_ms, x))
            out.append(best)

        # Keep briefly-unmatched tracks alive: a blob merging with another for a
        # frame or two must not reset its velocity history to zero.
        for tr in unmatched:
            tr.misses += 1
            if tr.misses <= 2:
                out.append(tr)

        self.tracks[row] = out
        return out

    def forget_below(self, row: int) -> None:
        for r in [r for r in self.tracks if r < row - 2]:
            del self.tracks[r]


# --------------------------------------------------------------------------
# Perceiver
# --------------------------------------------------------------------------


@dataclass
class Observation:
    world: World
    chicken_row: int
    chicken_col: float
    scroll_rows: int              # rows the field advanced since the last frame
    lane_confidence: Dict[int, float]


class Perceiver:
    """Frame -> World. Holds the tracking state across frames."""

    def __init__(self, calib: Calib, rows_ahead: int = 9, rows_behind: int = 1):
        self.calib = calib
        self.classifier = LaneClassifier(calib)
        self.tracker = BlobTracker(calib)
        self.rows_ahead = rows_ahead
        self.rows_behind = rows_behind
        self.chicken_row = 0
        self.chicken_col = 0.0
        self._prev_signature: Optional[np.ndarray] = None
        g = calib.geometry
        self.half_strip = int(calib.obstacles.get("strip_half_height_px", 30))
        self.col_min = float(g.get("col_min", -5))
        self.col_max = float(g.get("col_max", 5))

    # -- geometry ----------------------------------------------------------

    def _row_screen_y(self, row_offset: int) -> int:
        g = self.calib.geometry
        return int(round(g["row0_screen_y"] - row_offset * g["px_per_row"]))

    def _strip(self, img: np.ndarray, y: int) -> np.ndarray:
        top = max(0, y - self.half_strip)
        bot = min(img.shape[0], y + self.half_strip)
        return img[top:bot]

    # -- scroll detection --------------------------------------------------

    def _signature(self, hsv: np.ndarray) -> np.ndarray:
        """Mean hue per screen row — a cheap 1-D fingerprint of the field."""
        g = self.calib.geometry
        top = int(g.get("playfield_top_y", 300))
        return hsv[top:, :, 0].mean(axis=1).astype(np.float32)

    def _detect_scroll(self, sig: np.ndarray) -> int:
        """Whole rows the field moved since the previous frame.

        Scoring the row advance from vision rather than from "we sent a tap"
        keeps the score honest when a tap is dropped — and the score is what the
        kill switch acts on, so it must not be an assumption.
        """
        if self._prev_signature is None or self._prev_signature.shape != sig.shape:
            self._prev_signature = sig
            return 0
        px = int(round(self.calib.geometry["px_per_row"]))
        best, best_err = 0, float("inf")
        for shift in (-1, 0, 1):
            off = shift * px
            if off >= 0:
                a, b = self._prev_signature[off:], sig[: len(sig) - off]
            else:
                a, b = self._prev_signature[:off], sig[-off:]
            if len(a) < px:
                continue
            err = float(np.abs(a - b).mean())
            if err < best_err:
                best, best_err = shift, err
        self._prev_signature = sig
        return best

    # -- obstacle extraction ----------------------------------------------

    def _detect_obstacles(
        self, hsv_strip: np.ndarray, lane_type: LaneType, row: int
    ) -> List[Tuple[float, float]]:
        """Threshold to non-background, connected components, filter by area."""
        r = self.calib.lanes.get(lane_type.value)
        if not r or hsv_strip.size == 0:
            return []
        lo = np.array([r["h"][0], r["s"][0], r["v"][0]], np.uint8)
        hi = np.array([r["h"][1], r["s"][1], r["v"][1]], np.uint8)
        background = cv2.inRange(hsv_strip, lo, hi)
        fg = cv2.bitwise_not(background)
        fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))

        n, _, stats, centroids = cv2.connectedComponentsWithStats(fg, connectivity=8)
        out: List[Tuple[float, float]] = []
        for i in range(1, n):
            area = stats[i, cv2.CC_STAT_AREA]
            if not (self.min_area <= area <= self.max_area):
                continue
            cx = float(centroids[i][0])
            w_px = float(stats[i, cv2.CC_STAT_WIDTH])
            out.append((self.calib.screen_x_to_col(cx), self.calib.px_to_cols(w_px)))
        return out

    @property
    def min_area(self) -> int:
        return self.calib.obstacles.get("min_area_px", 400)

    @property
    def max_area(self) -> int:
        return self.calib.obstacles.get("max_area_px", 60000)

    # -- main --------------------------------------------------------------

    def perceive(self, img: np.ndarray, t_ms: float) -> Observation:
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        scroll = self._detect_scroll(self._signature(hsv))
        self.chicken_row += max(0, scroll)

        lanes: Dict[int, Lane] = {}
        confidence: Dict[int, float] = {}
        top_y = int(self.calib.geometry.get("playfield_top_y", 300))

        for offset in range(-self.rows_behind, self.rows_ahead + 1):
            y = self._row_screen_y(offset)
            if y < top_y or y >= img.shape[0]:
                continue
            row = self.chicken_row + offset
            strip = self._strip(hsv, y)
            lane_type, share = self.classifier.classify_strip(strip)
            confidence[row] = share

            obstacles: List[Obstacle] = []
            if lane_type in (LaneType.ROAD, LaneType.WATER, LaneType.TRACK, LaneType.GRASS):
                dets = self._detect_obstacles(strip, lane_type, row)
                for tr in self.tracker.update(row, dets, t_ms):
                    vx = 0.0 if lane_type is LaneType.GRASS else tr.velocity()
                    obstacles.append(
                        Obstacle(row=row, x=tr.x, width=tr.width, vx=vx, oid=tr.tid)
                    )

            lanes[row] = Lane(row=row, type=lane_type, obstacles=obstacles, confidence=share)

        self.tracker.forget_below(self.chicken_row)

        world = World(
            lanes=lanes, col_min=self.col_min, col_max=self.col_max, t_ref_ms=t_ms
        )
        return Observation(
            world=world,
            chicken_row=self.chicken_row,
            chicken_col=self.chicken_col,
            scroll_rows=scroll,
            lane_confidence=confidence,
        )

    def reset(self) -> None:
        """New run. Score is rows advanced, so the row counter starts over."""
        self.chicken_row = 0
        self.chicken_col = 0.0
        self._prev_signature = None
        self.tracker = BlobTracker(self.calib)

    def note_lateral(self, action: str) -> None:
        """The chicken's column is dead-reckoned from executed lateral moves.

        The camera keeps the chicken near-fixed on screen, so its column is not
        directly observable the way obstacles are. On a log this is refined by
        the carrier's velocity in the planner; on land the grid keeps it exact.
        """
        if action == "left":
            self.chicken_col = max(self.col_min, self.chicken_col - 1.0)
        elif action == "right":
            self.chicken_col = min(self.col_max, self.chicken_col + 1.0)
