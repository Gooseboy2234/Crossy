"""Screen classifier for the menu state machine.

Most unattended failures are not deaths. They are the harness sitting on a screen
it does not recognise, tapping into the void (CLAUDE.md:177, runbook §10.1). The
2026 build has a much larger menu surface than the runbook's six states: Pecking
Order, same-device multiplayer, the gacha machine, a piggy bank, daily gifts,
seasonal popups, rate prompts.

So the design is inverted from the runbook's table: **the unknown-state escape is
the primary mechanism and the named states are optimisations**
(docs/2026-reality-check.md §6). Anything not *positively* identified gets dumped
and the app relaunched after UNKNOWN_TIMEOUT_S. A screen nobody has ever seen
costs 10 seconds, not a night.

Grounding, 2026-08-16
---------------------
Everything below is measured from real frames off the device (iPhone 16 Pro,
native 1206x2622) rather than assumed. The previous revision of this file had
never seen a screenshot, and on the 38 real frames we now have it scored 0/38:
live gameplay came back `gacha` or `unknown` and the game-over screen came back
`gacha`. It would have relaunched the app every 10-25s all night.

Four states are grounded in real captures and classified here:

  SPLASH     the Hipster Whale logo card shown right after a relaunch
  MENU       the main menu (playfield + CROSSY ROAD logo + bottom tab bar)
  GAMEPLAY   a live run
  GAME_OVER  the post-death score card  (== Screen.DEATH, see the enum)

Everything else — ads, gacha, piggy bank, Pecking Order, seasonal popups — is
deliberately left as UNKNOWN. We have no real captures of them, and a heuristic
calibrated against nothing is exactly what produced the 0/38 above.

Signal design rules used throughout:

  * Fixed ROIs in **normalised** coordinates, so one set of numbers works at
    native 1206x2622, at the 886x1920 production downscale, and on the small
    test fixtures. Verified stable from full res down to 151x328.
  * Colour-signature fractions inside those ROIs. No template matching on the
    whole frame — it is slow, and it breaks on the 28 different worlds.
  * Every named state needs *positive* evidence. Absence of other states' markers
    is never enough, because the screens we have never seen are exactly the ones
    with no markers we know about.
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
    #: The post-death score card. `GAME_OVER` below is an alias for this member
    #: (same value), so `Screen.GAME_OVER is Screen.DEATH` — the readable name
    #: for new code, the original name for runner.py's death-logging branch and
    #: for the `screen` field already written into debug dumps.
    DEATH = "death"
    GAME_OVER = "death"
    MENU = "menu"
    SPLASH = "splash"
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
    Screen.DEATH: 25.0,           # also Screen.GAME_OVER
    # Measured: terminate -> activate -> splash -> menu is ~4.5s on this device.
    # A budget below that relaunches the app *during its own splash*, forever.
    Screen.SPLASH: 20.0,
    Screen.CRASHED: 8.0,
    Screen.UNKNOWN: UNKNOWN_TIMEOUT_S,
}


# --------------------------------------------------------------------------
# GAME-OVER TAP SAFETY  —  read this before emitting any tap on this screen
# --------------------------------------------------------------------------
#
# Measured from debug/phase0/.../f0034.png and the settled capture in
# tests/fixtures/game_over_settled.png. Both agree to 3 decimal places.
#
# Layout of the game-over card, top to bottom:
#
#   y 0.05-0.16   score, then "TOP 222"                     (text, inert)
#   y 0.429-0.494 blue "FREE" banner        <-- REWARDED VIDEO. NEVER TAP.
#   y 0.505-0.571 orange "FREE GIFT" banner <-- REWARDED VIDEO. NEVER TAP.
#   y 0.881-0.951 leaderboard | PLAY | share
#
# The two banners in the middle of the screen launch a rewarded video ad. This is
# not hypothetical: on 2026-08-16 a blind tap at a fixed coordinate started one.
# The centre of the screen is *inside* that hazard, so the harness must never
# fall back to "tap the middle" here — use GAME_OVER_PLAY_XY.
#
# The background traffic on this screen animates, so "the screen is moving" does
# NOT imply "we are in gameplay". Do not use motion as a gameplay signal.

#: Centre of the orange PLAY triangle: the ONLY safe place to tap on GAME_OVER.
#: Measured bbox x 0.293-0.706, y 0.881-0.951; this is its centroid.
GAME_OVER_PLAY_XY: Tuple[float, float] = (0.500, 0.916)

#: Centres of the two rewarded-video banners. Listed so they are greppable and so
#: nothing ever "helpfully" taps a banner to dismiss it. Tapping either one costs
#: a 30s unskippable ad at best.
GAME_OVER_REWARDED_BANNER_YS: Tuple[float, float] = (0.462, 0.538)

#: Inclusive no-tap band covering both banners plus a margin for the slide-in
#: animation, which moves them vertically for a few frames after death.
GAME_OVER_NO_TAP_Y: Tuple[float, float] = (0.41, 0.59)


def is_safe_game_over_tap(x: float, y: float) -> bool:
    """True if a normalised tap on the GAME_OVER screen misses both ad banners.

    Assert on this before sending anything on this screen. It is cheap, and the
    failure it prevents (a rewarded video, then a second screen, then a stalled
    night) is expensive.
    """
    lo, hi = GAME_OVER_NO_TAP_Y
    return not (lo <= y <= hi)


# --------------------------------------------------------------------------
# Colour signatures and ROIs, all measured off real device frames
# --------------------------------------------------------------------------
# HSV, OpenCV convention: H 0-179, S 0-255, V 0-255.
# ROIs are (x0, y0, x1, y1) as fractions of frame width/height.

#: Splash background. Sampled: H 97, S 148, V 238 — one flat cyan covering ~87%
#: of the card. The band is deliberately tight; a *generic* "one flat colour
#: fills the screen" rule would swallow solid-colour ad interstitials too, and
#: those must stay UNKNOWN so they get dumped.
SPLASH_CYAN = ((92, 125, 210), (102, 175, 255))
SPLASH_MIN_FILL = 0.45          # real: 0.863 splash / <=0.024 everything else

#: Bottom tab bar, present on the menu and absent in a run. Near-black chrome
#: (V<70) across the full width of the last few percent of the screen. On
#: gameplay and game-over that band is bright grass, road or button art.
MENU_TAB_BAR_ROI = (0.02, 0.955, 0.98, 0.995)
MENU_TAB_BAR_MAX_V = 70
MENU_TAB_BAR_MIN_DARK = 0.45    # real: 0.77 menu / <=0.044 gameplay / <=0.013 over

#: The bar is a *tab* bar — the shop and gacha screens almost certainly wear the
#: same chrome with a different tab lit. So MENU additionally requires the middle
#: (chicken / play) tab to be the selected one, which renders as a bright blue
#: tile. Without this a shop screen would be labelled MENU and the runner would
#: tap-through blindly instead of dumping it. Any other tab -> UNKNOWN -> dump
#: and relaunch, which lands us back on the play tab anyway.
MENU_PLAY_TAB_ROI = (0.425, 0.958, 0.575, 0.992)
MENU_PLAY_TAB_BLUE = ((88, 60, 170), (110, 255, 255))
MENU_PLAY_TAB_MIN_FILL = 0.40   # real: 1.00 menu / 0.00 gameplay and game-over

#: The two rewarded-video banners, used here only as a *detector*. Sampled:
#: blue H 106 S 170 V 231, orange H 17 S 217 V 255. Coverage is well under 1.0
#: because the "FREE"/"FREE GIFT" text and the video icons sit inside the bands.
GAME_OVER_BLUE_ROI = (0.03, 0.443, 0.97, 0.487)
GAME_OVER_BLUE = ((100, 130, 190), (112, 210, 255))
GAME_OVER_BLUE_MIN_FILL = 0.40  # real: 0.589-0.641 / 0.000 on gameplay and menu

GAME_OVER_ORANGE_ROI = (0.03, 0.520, 0.97, 0.565)
GAME_OVER_ORANGE = ((12, 185, 225), (23, 250, 255))
GAME_OVER_ORANGE_MIN_FILL = 0.35  # real: 0.510-0.630 / 0.000 elsewhere

#: The pause control: a light-blue rounded square under the coin counter. It is
#: the one piece of chrome that exists only while a run is live — it appears on
#: the first frame of the run and vanishes the instant the chicken dies. That
#: makes it the positive evidence GAMEPLAY needs.
#:
#: The match is bounded on BOTH sides, which matters more than it looks. A
#: *button* covers roughly 40% of its slot (measured 0.394-0.479 across every
#: capture scale) with frame art around it. A fill near 1.0 means the whole slot
#: is that colour — a flat blue background, not a control. Without the ceiling,
#: any solid-blue full-screen ad classifies as GAMEPLAY and the planner swipes at
#: it all night. The splash card floods this ROI the same way (same cyan family),
#: so the ceiling backs up the SPLASH-first ordering rather than relying on it.
PAUSE_BUTTON_ROI = (0.850, 0.112, 0.970, 0.176)
PAUSE_BUTTON_BLUE = ((90, 55, 200), (110, 205, 255))
PAUSE_BUTTON_MIN_FILL = 0.20    # real: 0.394-0.479 in a run / 0.000 in all else
PAUSE_BUTTON_MAX_FILL = 0.75    # a flooded slot is a background, not a button


def _roi(hsv: np.ndarray, box: Tuple[float, float, float, float]) -> np.ndarray:
    """Slice a normalised ROI. Normalised so one calibration fits every capture
    scale — the production pipeline downsamples to 886x1920, the phase0 dumps are
    native 1206x2622, and mirrored captures are a different aspect again."""
    h, w = hsv.shape[:2]
    x0, y0, x1, y1 = box
    return hsv[int(y0 * h) : int(y1 * h), int(x0 * w) : int(x1 * w)]


def _fill(roi: np.ndarray, band: Tuple[Tuple[int, int, int], Tuple[int, int, int]]) -> float:
    """Fraction of an ROI inside an HSV band. 0.0 for an empty ROI."""
    if roi.size == 0:
        return 0.0
    lo, hi = band
    mask = cv2.inRange(roi, np.array(lo, np.uint8), np.array(hi, np.uint8))
    return float(mask.mean()) / 255.0


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
    #
    # These three are diagnostics now, not decisions. On real frames they do not
    # separate anything we care about: the menu measures lanes=0.51 band=0.70-0.92
    # and live gameplay measures lanes=0.41-0.54 band=0.59-0.78, because the menu
    # *is* the playfield with a logo on top. They stay because they are cheap and
    # they go in the log line, which is what you read at 7am.

    def lane_structure_score(self, hsv: np.ndarray) -> float:
        """Fraction of sampled rows that look like a known lane type."""
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
        """How row-uniform the image is.

        Real device frames measure 0.59-0.92: the camera is very slightly rotated
        so lanes are not axis-aligned, and trees, cars and the chicken break every
        row up. Values above ~1.2 mean bands so perfect nothing 3D-rendered
        produced them — see the note in `classify`.
        """
        h = hsv[self.top_y :, :, 0].astype(np.float32)
        if h.size == 0:
            return 0.0
        within_row = float(h.std(axis=1).mean())
        between_rows = float(h.mean(axis=1).std())
        return between_rows / (within_row + 1e-6)

    def palette_size(self, bgr: np.ndarray, buckets: int = 16) -> int:
        """Distinct coarse colours.

        The old code assumed ~6 for the Original world and treated >60 as "busy,
        therefore an ad". Real frames measure 420-580, because of anti-aliasing,
        shadows and the isometric shading on every voxel. The measurement was
        never wrong; the assumption about its range was, and it is why real
        gameplay used to come back as `gacha`. Diagnostic only now.
        """
        small = cv2.resize(bgr, (64, 128), interpolation=cv2.INTER_AREA)
        q = (small // (256 // buckets)).reshape(-1, 3)
        return len(np.unique(q, axis=0))

    def darkened_overlay(self, bgr: np.ndarray) -> float:
        """Fraction of the playfield that is dark. Diagnostic.

        NB the real game-over screen does *not* dim the playfield — it slides two
        banners over an undimmed, still-animating traffic scene (measured 0.08,
        i.e. less dark than the menu's 0.22, which is dark only because of the
        tab bar). The old DEATH rule keyed on dimming that does not exist.
        """
        v = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)[self.top_y :, :, 2]
        return float((v < 90).mean())

    # -- grounded signals --------------------------------------------------

    def splash_fill(self, hsv: np.ndarray) -> float:
        """Share of the whole frame that is the Hipster Whale card's flat cyan.

        Strided 4x in both axes. This runs on every frame of a 60fps loop and the
        card is one flat colour over ~87% of itself, so a 16th of the pixels
        answers the same question for a 16th of the cost (measured: 0.863 full
        vs 0.863 strided).
        """
        return _fill(hsv[::4, ::4], SPLASH_CYAN)

    def tab_bar_darkness(self, hsv: np.ndarray) -> float:
        """Share of the bottom chrome strip that is near-black menu tab bar."""
        roi = _roi(hsv, MENU_TAB_BAR_ROI)
        if roi.size == 0:
            return 0.0
        return float((roi[:, :, 2] < MENU_TAB_BAR_MAX_V).mean())

    def play_tab_fill(self, hsv: np.ndarray) -> float:
        """Share of the middle tab slot lit up as the selected (chicken) tab."""
        return _fill(_roi(hsv, MENU_PLAY_TAB_ROI), MENU_PLAY_TAB_BLUE)

    def rewarded_banner_fills(self, hsv: np.ndarray) -> Tuple[float, float]:
        """(blue, orange) coverage of the two game-over rewarded-video banners."""
        return (
            _fill(_roi(hsv, GAME_OVER_BLUE_ROI), GAME_OVER_BLUE),
            _fill(_roi(hsv, GAME_OVER_ORANGE_ROI), GAME_OVER_ORANGE),
        )

    def pause_button_fill(self, hsv: np.ndarray) -> float:
        """Share of the pause-control slot that is the light-blue button."""
        return _fill(_roi(hsv, PAUSE_BUTTON_ROI), PAUSE_BUTTON_BLUE)

    # -- classification ----------------------------------------------------

    def classify(self, bgr: np.ndarray) -> Classification:
        """Label a frame, defaulting to UNKNOWN.

        Ordered most-specific first. Every branch needs positive evidence; the
        function falls through to UNKNOWN so the caller dumps to debug/unknown/
        and relaunches (CLAUDE.md:177). That fall-through is the feature, not the
        gap — it is what makes a screen nobody has seen cost 10 seconds.
        """
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)

        splash = self.splash_fill(hsv)
        bar = self.tab_bar_darkness(hsv)
        tab = self.play_tab_fill(hsv)
        blue, orange = self.rewarded_banner_fills(hsv)
        pause = self.pause_button_fill(hsv)

        # The five signals above cost ~2ms at 886x1920; lane_structure_score and
        # horizontal_banding cost another ~3ms between them and decide nothing
        # (see the note on the primitives). At 60fps that is a third of the frame
        # budget spent on a log line, so they are computed only on the paths that
        # actually read them: the synthetic fallback, and the UNKNOWN dump where
        # they are the numbers you triage from in the morning.
        detail = (
            f"splash={splash:.2f} bar={bar:.2f} tab={tab:.2f} "
            f"banner={blue:.2f}/{orange:.2f} pause={pause:.2f}"
        )

        # SPLASH first: its flat cyan also fills the pause-button ROI, and it is
        # the screen we see immediately after every relaunch, so mislabelling it
        # UNKNOWN would relaunch during the splash — forever.
        if splash >= SPLASH_MIN_FILL:
            return Classification(Screen.SPLASH, min(1.0, splash), detail)

        # MENU: dark tab bar along the bottom AND the play tab selected. Both,
        # because the bar is shared with the shop/gacha tabs we have never
        # captured; only the lit middle tab says "tapping through starts a run".
        if bar >= MENU_TAB_BAR_MIN_DARK and tab >= MENU_PLAY_TAB_MIN_FILL:
            return Classification(Screen.MENU, min(1.0, (bar + tab) / 2), detail)

        # GAME_OVER: both rewarded-video banners present, stacked in that order.
        #
        # Requiring BOTH is deliberate. The blue band alone overlaps calib.yaml's
        # water range (H 95-130), so a river row could fake it; nothing in the
        # game puts a saturated orange band (S 217, V 255 — outside the track
        # band's S<=200, V<=200) directly under a blue one. It also means the
        # first frame or two of the slide-in animation stay UNKNOWN, which is
        # what we want: no taps while the buttons are still moving.
        if blue >= GAME_OVER_BLUE_MIN_FILL and orange >= GAME_OVER_ORANGE_MIN_FILL:
            return Classification(Screen.GAME_OVER, min(1.0, (blue + orange) / 2), detail)

        # GAMEPLAY: the pause control is showing — a button-sized blue blob in
        # its slot, not a slot flooded with blue. It is the only chrome unique to
        # a live run: it appears on frame one and disappears the moment the
        # chicken is hit, so it cannot be spoofed by the menu (which is the same
        # playfield) or by the game-over card (same animated traffic).
        #
        # This is the signal most likely to need re-grounding if the HUD moves;
        # PAUSE_BUTTON_ROI above is the one number to re-measure.
        if PAUSE_BUTTON_MIN_FILL <= pause <= PAUSE_BUTTON_MAX_FILL:
            return Classification(Screen.GAMEPLAY, min(1.0, pause / 0.40), detail)

        # Structural fallback: bands so perfectly horizontal and so flat that
        # nothing rendered in 3D could produce them. No real frame has ever
        # reached band>=1.2 — the device tops out at 0.92 — so on device this is
        # dead weight; it exists for synthetic renders (tests/test_perception.py)
        # and as a last-resort world-agnostic signal. Kept at low confidence, and
        # deliberately unreachable by anything the camera sees.
        lanes = self.lane_structure_score(hsv)
        banding = self.horizontal_banding(hsv)
        detail = f"{detail} lanes={lanes:.2f} band={banding:.2f}"
        if lanes >= 0.45 and banding >= 1.2:
            return Classification(Screen.GAMEPLAY, 0.5, detail + " [synthetic-banding]")

        # Everything else — ads, gacha, piggy bank, Pecking Order, seasonal
        # popups, rate prompts, and whatever ships next month. We have no real
        # captures of any of them, so there is nothing honest to key on. The old
        # revision guessed here (`palette>60` -> AD, `0.15<=lanes<0.45` -> GACHA)
        # and the guesses were actively harmful: real gameplay measures
        # lanes 0.41-0.54, so the GACHA branch fired *on live runs*.
        #
        # UNKNOWN is stricter than any of those labels anyway — 10s to dump and
        # relaunch, versus 25-45s of tapping at a screen we have mislabelled.
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

    UNGROUNDED: we have captured no real interstitial yet, so this has never been
    checked against one. `classify` no longer emits Screen.AD for that reason —
    ads currently fall through to UNKNOWN and the 10s relaunch. This stays for
    when there are real ad frames to calibrate it against.
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

    UNGROUNDED: no event-mode frames captured. The baseline is taken from the
    first gameplay frame of the session, so this only ever measures drift away
    from whatever was running at the start.
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
