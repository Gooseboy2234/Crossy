"""Screen classifier for the menu state machine.

Most unattended failures are not deaths. They are the harness sitting on a screen
it does not recognise, tapping into the void (runbook §10.1). The 2026 build has
a much larger menu surface than the runbook's six states: Pecking Order,
same-device multiplayer, the gacha machine, a piggy bank, daily gifts, seasonal
popups, rate prompts.

So the design is inverted from the runbook's table: **the unknown-state escape is
the primary mechanism and the named states are optimisations.** Anything not
positively identified for >10s gets dumped and the app relaunched. That way a
screen nobody has ever seen costs 10 seconds, not a night.

Every heuristic here is a starting point calibrated against nothing. First night
will surface screens you did not know existed; `debug/unknown/` is how you add
them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

from config import Calib
from world import LaneType

log = logging.getLogger("states")


class Screen(str, Enum):
    GAMEPLAY = "gameplay"
    DEATH = "death"
    MENU = "menu"
    AD = "ad"
    GACHA = "gacha"
    CRASHED = "crashed"
    UNKNOWN = "unknown"


#: Hard ceiling on any ad-like state before force-relaunching the app.
#: Ad handling must never be an unbounded loop — close buttons appear on a delay,
#: sit at variable positions, are sometimes deliberately tiny or misleading, and
#: occasionally lead to a second screen. This is genuinely adversarial CV and it
#: is the most likely thing to strand the harness at 2am.
AD_TIMEOUT_S = 45.0
UNKNOWN_TIMEOUT_S = 10.0

#: How long the harness may sit in each non-gameplay state before force-relaunching.
#:
#: EVERY non-gameplay state has a budget, not just ads. Distinguishing an
#: interstitial from the gacha screen is genuinely hard and, operationally, it
#: does not matter: both are "a full-screen thing that is not the game". Making
#: the timeout depend on getting that label right just moves the stall from the
#: state you handled to the state you mislabelled. So the budget is universal and
#: the labels are only there to pick a smarter escape action.
STATE_TIMEOUT_S = {
    Screen.AD: AD_TIMEOUT_S,
    Screen.GACHA: 25.0,
    Screen.MENU: 25.0,
    Screen.DEATH: 25.0,
    Screen.CRASHED: 8.0,
    Screen.UNKNOWN: UNKNOWN_TIMEOUT_S,
}


@dataclass
class Classification:
    screen: Screen
    confidence: float
    detail: str = ""


class ScreenClassifier:
    def __init__(self, calib: Calib):
        self.calib = calib
        self.g = calib.geometry
        self.top_y = int(self.g.get("playfield_top_y", 300))
        self._lane_ranges = calib.lanes

    # -- primitives --------------------------------------------------------

    def lane_structure_score(self, hsv: np.ndarray) -> float:
        """Fraction of sampled rows that look like a known lane type.

        Gameplay has strong horizontal banding in a handful of flat colours.
        Interstitials, menus and the gacha screen do not.
        """
        px_per_row = self.g.get("px_per_row", 78.0)
        ys = range(self.top_y, hsv.shape[0] - 1, max(8, int(px_per_row / 3)))
        hits = 0
        total = 0
        for y in ys:
            strip = hsv[y : y + 4]
            if strip.size == 0:
                continue
            total += 1
            n = strip.shape[0] * strip.shape[1]
            for r in self._lane_ranges.values():
                lo = np.array([r["h"][0], r["s"][0], r["v"][0]], np.uint8)
                hi = np.array([r["h"][1], r["s"][1], r["v"][1]], np.uint8)
                if float(cv2.inRange(strip, lo, hi).sum()) / (255.0 * n) > 0.6:
                    hits += 1
                    break
        return hits / total if total else 0.0

    def horizontal_banding(self, hsv: np.ndarray) -> float:
        """How row-uniform the image is. Voxel lanes band hard; ads do not."""
        h = hsv[self.top_y :, :, 0].astype(np.float32)
        if h.size == 0:
            return 0.0
        within_row = float(h.std(axis=1).mean())
        between_rows = float(h.mean(axis=1).std())
        return between_rows / (within_row + 1e-6)

    def palette_size(self, bgr: np.ndarray, buckets: int = 16) -> int:
        """Distinct coarse colours. ~6 for the Original world; ads are busy."""
        small = cv2.resize(bgr, (64, 128), interpolation=cv2.INTER_AREA)
        q = (small // (256 // buckets)).reshape(-1, 3)
        return len(np.unique(q, axis=0))

    def darkened_overlay(self, bgr: np.ndarray) -> float:
        """Death and pause screens dim the playfield behind a panel."""
        v = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)[self.top_y :, :, 2]
        return float((v < 90).mean())

    # -- classification ----------------------------------------------------

    def classify(self, bgr: np.ndarray) -> Classification:
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        lanes = self.lane_structure_score(hsv)
        banding = self.horizontal_banding(hsv)
        palette = self.palette_size(bgr)
        dark = self.darkened_overlay(bgr)

        detail = f"lanes={lanes:.2f} band={banding:.1f} pal={palette} dark={dark:.2f}"

        if lanes >= 0.45 and banding >= 1.2:
            if dark >= 0.35:
                return Classification(Screen.DEATH, min(1.0, dark + lanes) / 2, detail)
            return Classification(Screen.GAMEPLAY, min(1.0, lanes), detail)

        if lanes < 0.15 and palette > 60:
            # Busy, unbanded, full-screen: an interstitial, most likely.
            return Classification(Screen.AD, 0.6, detail)

        if 0.15 <= lanes < 0.45 and palette > 40:
            return Classification(Screen.GACHA, 0.4, detail)

        if palette < 12 and lanes < 0.1:
            # Nearly blank — springboard, a loading screen, or a black frame.
            return Classification(Screen.CRASHED, 0.4, detail)

        return Classification(Screen.UNKNOWN, 0.0, detail)


# --------------------------------------------------------------------------
# Ad close buttons
# --------------------------------------------------------------------------


def find_close_buttons(bgr: np.ndarray, max_candidates: int = 6) -> List[Tuple[int, int]]:
    """Candidate close-button positions, best first.

    Heuristic, and deliberately so: ad creatives are adversarial and no fixed
    template survives contact. We look for small, high-contrast, roughly square
    blobs near the top corners — where the real control almost always is — and
    return several candidates to try in order.

    This is a best-effort optimisation on top of the timeout, never a
    replacement for it. If none of these work, AD_TIMEOUT_S relaunches the app
    and the run is lost but the night is not.
    """
    h, w = bgr.shape[:2]
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    candidates: List[Tuple[float, int, int]] = []

    # Corner regions, in preference order: ads put the X top-right most often.
    regions = [
        ("tr", int(w * 0.72), 0, w, int(h * 0.16)),
        ("tl", 0, 0, int(w * 0.28), int(h * 0.16)),
        ("br", int(w * 0.72), int(h * 0.86), w, h),
        ("bl", 0, int(h * 0.86), int(w * 0.28), h),
    ]
    weight = {"tr": 1.0, "tl": 0.8, "br": 0.5, "bl": 0.4}

    for name, x0, y0, x1, y1 in regions:
        roi = gray[y0:y1, x0:x1]
        if roi.size == 0:
            continue
        edges = cv2.Canny(roi, 60, 180)
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in contours:
            x, y, bw, bh = cv2.boundingRect(c)
            if not (10 <= bw <= 90 and 10 <= bh <= 90):
                continue
            aspect = bw / float(bh)
            if not (0.65 <= aspect <= 1.55):        # close buttons are square-ish
                continue
            score = weight[name] * (1.0 - abs(1.0 - aspect))
            candidates.append((score, x0 + x + bw // 2, y0 + y + bh // 2))

    candidates.sort(key=lambda c: -c[0])
    seen: List[Tuple[int, int]] = []
    for _, cx, cy in candidates:
        if all(abs(cx - sx) > 20 or abs(cy - sy) > 20 for sx, sy in seen):
            seen.append((cx, cy))
        if len(seen) >= max_candidates:
            break
    return seen


# --------------------------------------------------------------------------
# Event-mode guard
# --------------------------------------------------------------------------


class EventModeGuard:
    """Abort if the game is not running the classic endless mode.

    2026 ships limited-time modes that *replace* the main game — "Hopside Down"
    (Apr 2026) flipped the entire screen except the score, coin counter and pause
    button; "Crashy Cart" (Nov 2025) replaced hopping with an auto-scrolling
    cart. Either one running overnight is a total-loss night, and to the harness
    it looks like a catastrophic perception bug rather than a different game.

    The check: under the classic camera the playfield's vertical colour profile
    is denser toward the bottom (the chicken sits low, terrain recedes upward).
    A flipped screen inverts that. Cheap, and it fails loudly instead of
    grinding.
    """

    def __init__(self, calib: Calib, samples: int = 40):
        self.calib = calib
        self.top_y = int(calib.geometry.get("playfield_top_y", 300))
        self.samples = samples
        self.baseline: Optional[float] = None

    @staticmethod
    def _vertical_balance(hsv: np.ndarray, top_y: int) -> float:
        """Saturation mass below the midline minus above it, normalised."""
        s = hsv[top_y:, :, 1].astype(np.float32)
        if s.size == 0:
            return 0.0
        mid = s.shape[0] // 2
        upper, lower = s[:mid].mean(), s[mid:].mean()
        return float((lower - upper) / (lower + upper + 1e-6))

    def calibrate(self, bgr: np.ndarray) -> None:
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        self.baseline = self._vertical_balance(hsv, self.top_y)

    def looks_flipped(self, bgr: np.ndarray) -> bool:
        if self.baseline is None:
            return False
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        now = self._vertical_balance(hsv, self.top_y)
        # Sign inversion with meaningful magnitude, not just drift toward zero.
        return (now * self.baseline < 0) and abs(now - self.baseline) > 0.25
