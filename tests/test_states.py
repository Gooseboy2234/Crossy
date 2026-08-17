"""Screen classification against real frames captured off the device.

Everything in tests/fixtures/ is a real screenshot from an iPhone 16 Pro running
the shipping build, downscaled 6x (and the mirror tile 6x from a QuickTime screen
recording, i.e. a second, independent capture path). Nothing here is synthetic
except the deliberately-degraded frames in the "safe default" section, which
exist to prove the classifier refuses to guess.

This file is the reason states.py can be trusted at 3am. Before it existed the
classifier had never seen a screenshot and scored 0/38 on these same frames:
live gameplay came back `gacha`, the game-over card came back `gacha`, and the
harness would have relaunched the app every 10-25 seconds all night.
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_calib
from states import (
    GAME_OVER_NO_TAP_Y,
    GAME_OVER_PLAY_XY,
    GAME_OVER_REWARDED_BANNER_YS,
    STATE_TIMEOUT_S,
    Screen,
    ScreenClassifier,
    is_safe_game_over_tap,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CALIB = load_calib()


def load(name: str) -> np.ndarray:
    path = FIXTURES / name
    img = cv2.imread(str(path))
    assert img is not None, f"missing fixture {path}"
    return img


@pytest.fixture(scope="module")
def clf() -> ScreenClassifier:
    return ScreenClassifier(CALIB)


#: fixture -> the screen a human sees when looking at it.
GROUND_TRUTH = {
    # Hipster Whale card, shown for a few seconds after every relaunch.
    "splash.png": Screen.SPLASH,
    # Main menu: playfield behind the CROSSY ROAD logo, tab bar along the bottom.
    "menu.png": Screen.MENU,
    # Same menu, logo mid-animation on the other side — the logo drifts, so
    # nothing may key on where it is.
    "menu_logo_left.png": Screen.MENU,
    # First seconds of a run (score 0) and just before the fatal car (score 3).
    "gameplay_early.png": Screen.GAMEPLAY,
    "gameplay_late.png": Screen.GAMEPLAY,
    # Game-over card. `_settled` is a separate run; `_mirror_tile` came through
    # the screen-recording path instead of a device screenshot, at a third scale.
    "game_over.png": Screen.GAME_OVER,
    "game_over_settled.png": Screen.GAME_OVER,
    "game_over_mirror_tile.png": Screen.GAME_OVER,
    # Two frames after the car hits: chicken gone, pause button gone, banners not
    # in yet. Genuinely ambiguous, and correctly refused.
    "death_transition.png": Screen.UNKNOWN,
}


# --------------------------------------------------------------------------
# Ground truth
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name,expected", sorted(GROUND_TRUTH.items()))
def test_real_screen_classifies_correctly(clf, name, expected):
    got = clf.classify(load(name))
    assert got.screen is expected, f"{name}: {got.screen.value} ({got.detail})"


@pytest.mark.parametrize("name,expected", sorted(GROUND_TRUTH.items()))
def test_classification_survives_the_capture_downscale(clf, name, expected):
    """The ROIs are normalised, so the answer must not depend on capture scale.

    Production runs ffmpeg at 886x1920 (calib.capture), the phase0 dumps are
    native 1206x2622 and these fixtures are 6x smaller again. A classifier that
    only works at one of those is a classifier that works in tests and nowhere
    else.
    """
    img = load(name)
    for w, h in ((886, 1920), (1206, 2622), (302, 656)):
        resized = cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)
        got = clf.classify(resized)
        assert got.screen is expected, f"{name} @ {w}x{h}: {got.screen.value} ({got.detail})"


def test_menu_and_gameplay_are_told_apart_by_chrome_not_by_the_playfield(clf):
    """The menu *is* the playfield, so no terrain statistic can separate them.

    Measured on these fixtures: menu lanes=0.51 band=0.70-0.92, gameplay
    lanes=0.41-0.54 band=0.59-0.78. Fully overlapping. The old classifier tried
    to split them on exactly those numbers, which is why it never returned
    GAMEPLAY at all.
    """
    menu = cv2.cvtColor(load("menu.png"), cv2.COLOR_BGR2HSV)
    play = cv2.cvtColor(load("gameplay_late.png"), cv2.COLOR_BGR2HSV)

    # The terrain statistics do not separate them...
    assert abs(clf.lane_structure_score(menu) - clf.lane_structure_score(play)) < 0.25

    # ...but the chrome does, in both directions.
    assert clf.tab_bar_darkness(menu) > 0.45 and clf.tab_bar_darkness(play) < 0.15
    assert clf.pause_button_fill(play) > 0.20 and clf.pause_button_fill(menu) < 0.05


def test_animated_traffic_is_not_a_gameplay_signal(clf):
    """Game-over runs a live traffic scene behind the banners.

    Two consecutive game-over captures differ substantially in the playfield, so
    "the screen is moving" must never be read as "we are in a run". Both must
    still classify as GAME_OVER.
    """
    a, b = load("game_over.png"), load("game_over_settled.png")
    playfield = (slice(int(0.60 * a.shape[0]), int(0.85 * a.shape[0])), slice(None))
    moved = float(np.mean(cv2.absdiff(a[playfield], b[playfield])))
    assert moved > 5.0, "fixtures do not actually differ; pick two further apart"
    assert clf.classify(a).screen is Screen.GAME_OVER
    assert clf.classify(b).screen is Screen.GAME_OVER


# --------------------------------------------------------------------------
# UNKNOWN is the safe default (CLAUDE.md:177)
# --------------------------------------------------------------------------


def test_gameplay_needs_the_pause_button_not_just_the_absence_of_menus(clf):
    """Strip the one piece of live-run chrome and the frame stops being gameplay.

    This is the whole safety argument. Every screen we have never captured —
    gacha, piggy bank, Pecking Order, seasonal popups — looks like "not the menu
    and not the game-over card". If that were enough to mean GAMEPLAY, the
    perceiver would hallucinate lanes on a shop screen and the planner would
    swipe at it until morning.
    """
    img = load("gameplay_late.png").copy()
    h, w = img.shape[:2]
    img[int(0.10 * h) : int(0.19 * h), int(0.83 * w) : w] = (90, 170, 90)   # grass over the button
    got = clf.classify(img)
    assert got.screen is Screen.UNKNOWN, got.detail


def test_menu_needs_the_play_tab_lit_not_just_the_tab_bar(clf):
    """A dark bottom bar alone is not the main menu.

    The bar is a *tab* bar — the shop and gacha screens wear the same chrome with
    a different tab selected. We have never captured those, so a frame with the
    bar but no lit play tab must fall through to the dump-and-relaunch path
    rather than get tapped through blindly.
    """
    img = load("menu.png").copy()
    h, w = img.shape[:2]
    img[int(0.95 * h) : h, int(0.41 * w) : int(0.59 * w)] = (24, 24, 24)    # unlight the tab
    got = clf.classify(img)
    assert got.screen is not Screen.MENU, got.detail
    assert got.screen is Screen.UNKNOWN, got.detail


def test_game_over_needs_both_banners(clf):
    """One banner is not enough, on purpose.

    The blue band sits inside calib.yaml's water range (H 95-130), so a river row
    could fake it on its own. Nothing in the game puts a saturated orange band
    (S 217, V 255 — past the track band's S<=200, V<=200) directly beneath a blue
    one. Requiring the pair also keeps the slide-in animation UNKNOWN, so no tap
    is emitted while the PLAY button is still moving.
    """
    img = load("game_over_settled.png").copy()
    h = img.shape[0]
    img[int(0.50 * h) : int(0.58 * h)] = (90, 170, 90)      # grass over the orange banner
    got = clf.classify(img)
    assert got.screen is not Screen.GAME_OVER, got.detail
    assert got.screen is Screen.UNKNOWN, got.detail


@pytest.mark.parametrize(
    "name,frame",
    [
        ("flat grey", np.full((1920, 886, 3), 128, np.uint8)),
        ("black", np.zeros((1920, 886, 3), np.uint8)),
        ("white", np.full((1920, 886, 3), 255, np.uint8)),
        ("noise", np.random.default_rng(0).integers(0, 255, (1920, 886, 3), dtype=np.uint8)),
        ("flat cyan-but-wrong-shade", np.full((1920, 886, 3), (200, 120, 40), np.uint8)),
    ],
)
def test_screens_we_have_never_seen_are_unknown(clf, name, frame):
    """Anything without positive evidence must be UNKNOWN so the caller dumps it.

    Note the last case: a solid-colour screen that is *not* the Hipster Whale
    cyan. SPLASH keys on that exact shade rather than on "one flat colour fills
    the frame", because the generic version would swallow solid-colour ad
    interstitials, and those must be dumped, not waited out.
    """
    got = clf.classify(frame)
    assert got.screen is Screen.UNKNOWN, f"{name}: {got.screen.value} ({got.detail})"


def test_a_slot_flooded_with_button_colour_is_not_a_button(clf):
    """A full-screen creative in the pause button's exact blue must not be gameplay.

    The pause check is bounded on both sides for this reason. A real button fills
    ~40% of its slot with frame art around it; 100% means the slot is a
    background. Found by a solid-colour test case, not by reasoning — an
    unbounded "is there blue here" test called this frame GAMEPLAY.
    """
    hsv = np.zeros((1920, 886, 3), np.uint8)
    hsv[:] = (99, 100, 240)                                  # dead centre of PAUSE_BUTTON_BLUE
    frame = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
    got = clf.classify(frame)
    assert got.screen is not Screen.GAMEPLAY, got.detail


def test_no_real_frame_is_labelled_ad_or_gacha(clf):
    """Both labels are ungrounded — we have captured neither screen.

    The previous revision emitted GACHA for live gameplay frames (its rule was
    0.15 <= lanes < 0.45, and real gameplay measures 0.41-0.54). A wrong label is
    worse than no label: it buys the stall a 25-45s budget instead of 10s.
    """
    for name in GROUND_TRUTH:
        got = clf.classify(load(name))
        assert got.screen not in (Screen.AD, Screen.GACHA, Screen.CRASHED), \
            f"{name}: {got.screen.value} ({got.detail})"


def test_a_live_run_is_never_read_as_game_over(clf):
    """A false GAME_OVER logs a phantom death and ends the run's bookkeeping."""
    for name in ("gameplay_early.png", "gameplay_late.png"):
        blue, orange = clf.rewarded_banner_fills(cv2.cvtColor(load(name), cv2.COLOR_BGR2HSV))
        assert blue < 0.05 and orange < 0.05, f"{name}: banners {blue:.3f}/{orange:.3f}"


# --------------------------------------------------------------------------
# Tap safety on the game-over screen
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["game_over.png", "game_over_settled.png",
                                  "game_over_mirror_tile.png"])
def test_play_button_constant_lands_on_the_play_button(clf, name):
    """GAME_OVER_PLAY_XY must point at orange button art, in every capture path."""
    img = load(name)
    h, w = img.shape[:2]
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    x, y = GAME_OVER_PLAY_XY
    patch = hsv[int((y - 0.03) * h) : int((y + 0.03) * h),
                int((x - 0.03) * w) : int((x + 0.03) * w)]
    orange = cv2.inRange(patch, np.array((8, 140, 200), np.uint8),
                         np.array((28, 255, 255), np.uint8)).mean() / 255.0
    # The rest of the patch is the white PLAY triangle sitting on the orange.
    assert orange > 0.30, f"{name}: tap target is {orange:.2f} orange — button moved?"


@pytest.mark.parametrize("name", ["game_over.png", "game_over_settled.png",
                                  "game_over_mirror_tile.png"])
def test_the_forbidden_ys_really_are_the_rewarded_video_banners(clf, name):
    """y~0.46 is the blue FREE banner and y~0.53 the orange FREE GIFT banner.

    Both launch a rewarded video. On 2026-08-16 a blind tap at a fixed coordinate
    started one. This test is here so the constants cannot rot into pointing at
    something harmless while the comment still says "never tap".
    """
    img = load(name)
    h, w = img.shape[:2]
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    y_blue, y_orange = GAME_OVER_REWARDED_BANNER_YS

    def band(y, lo, hi):
        strip = hsv[int((y - 0.01) * h) : int((y + 0.01) * h), int(0.03 * w) : int(0.97 * w)]
        return cv2.inRange(strip, np.array(lo, np.uint8), np.array(hi, np.uint8)).mean() / 255.0

    assert band(y_blue, (100, 130, 190), (112, 210, 255)) > 0.40, f"{name}: blue banner moved"
    assert band(y_orange, (12, 185, 225), (23, 250, 255)) > 0.40, f"{name}: orange banner moved"

    # ...and both are inside the no-tap band.
    lo, hi = GAME_OVER_NO_TAP_Y
    assert lo < y_blue < hi and lo < y_orange < hi


def test_tap_safety_helper_rejects_the_banners_and_accepts_the_play_button():
    assert is_safe_game_over_tap(*GAME_OVER_PLAY_XY)
    for y in GAME_OVER_REWARDED_BANNER_YS:
        assert not is_safe_game_over_tap(0.5, y), f"y={y} is a rewarded-video banner"
    # The measured banner extents, edge to edge.
    for y in (0.429, 0.494, 0.505, 0.571):
        assert not is_safe_game_over_tap(0.5, y)


def test_the_centre_of_the_screen_is_inside_the_hazard():
    """"Tap the middle" is the natural fallback and it is exactly wrong here.

    Anything that reaches for a screen-centre tap on GAME_OVER lands between the
    two rewarded-video banners, within a few percent of both.
    """
    assert not is_safe_game_over_tap(0.5, 0.5)


def test_play_button_is_clear_of_the_hazard_by_a_wide_margin():
    _, y = GAME_OVER_PLAY_XY
    _, hi = GAME_OVER_NO_TAP_Y
    assert y - hi > 0.25, "PLAY button is uncomfortably close to the banners"


# --------------------------------------------------------------------------
# Wiring the runner depends on
# --------------------------------------------------------------------------


def test_game_over_is_the_same_member_as_death():
    """runner.py branches on Screen.DEATH to log the run and dump the ring buffer.

    GAME_OVER is the readable alias for it — one screen, two names — so renaming
    in new code cannot silently detach the death-logging path.
    """
    assert Screen.GAME_OVER is Screen.DEATH
    assert Screen.GAME_OVER.value == "death"


def test_every_named_non_gameplay_state_has_a_timeout_budget():
    for screen in Screen:
        if screen is Screen.GAMEPLAY:
            continue
        assert screen in STATE_TIMEOUT_S, f"{screen.value} has no escape budget"


def test_splash_budget_outlasts_a_relaunch():
    """A relaunch is terminate -> activate -> splash -> menu, ~4.5s on device.

    A splash budget shorter than that relaunches the app during its own splash
    screen, forever — the exact "stuck tapping into the void" failure CLAUDE.md
    warns about, self-inflicted.
    """
    assert STATE_TIMEOUT_S[Screen.SPLASH] > 10.0
