"""Perception round-trip tests against synthetically rendered frames.

There is no device here, so these render a frame *from* a known world using the
calibration geometry, then check the Perceiver recovers that world. That closes
the loop on the pixel→world conversion, the HSV ranges as written in calib.yaml,
and the velocity estimator.

What it does NOT establish: that the HSV ranges match the real game. They are
placeholders. Only a contact sheet from real frames settles that.
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_calib
from perceive import VELOCITY_FRAMES, Perceiver
from states import Screen, ScreenClassifier, find_close_buttons
from world import LaneType

CALIB = load_calib()
W = CALIB.capture.get("scale_w", 886)
H = CALIB.capture.get("scale_h", 1920)

# Mid-range HSV picks for each lane, inside the calib.yaml bands.
LANE_HSV = {
    LaneType.GRASS: (60, 180, 150),
    LaneType.ROAD: (0, 10, 85),
    LaneType.WATER: (110, 180, 180),
    LaneType.TRACK: (15, 130, 120),
}
#: Deliberately outside every lane band, so it reads as foreground.
OBSTACLE_HSV = (170, 240, 240)


def _bgr(hsv):
    px = np.uint8([[list(hsv)]])
    return tuple(int(v) for v in cv2.cvtColor(px, cv2.COLOR_HSV2BGR)[0][0])


def render(lanes: dict[int, LaneType], obstacles: dict[int, list[tuple[float, float]]] | None = None,
           chicken_row: int = 0) -> np.ndarray:
    """Draw a frame: horizontal lane bands plus obstacle boxes, in world columns."""
    img = np.zeros((H, W, 3), np.uint8)
    img[:] = _bgr(LANE_HSV[LaneType.GRASS])
    g = CALIB.geometry
    half = int(g["px_per_row"] / 2)

    for row, lane in lanes.items():
        y = int(g["row0_screen_y"] - (row - chicken_row) * g["px_per_row"])
        cv2.rectangle(img, (0, y - half), (W, y + half), _bgr(LANE_HSV[lane]), -1)

    for row, obs in (obstacles or {}).items():
        y = int(g["row0_screen_y"] - (row - chicken_row) * g["px_per_row"])
        for x_col, w_col in obs:
            cx = int(CALIB.col_to_screen_x(x_col))
            hw = int(w_col * g["px_per_col"] / 2)
            cv2.rectangle(img, (cx - hw, y - 28), (cx + hw, y + 28), _bgr(OBSTACLE_HSV), -1)

    # HUD band so the playfield_top_y crop has something to exclude.
    cv2.rectangle(img, (0, 0), (W, int(g["playfield_top_y"]) - 1), (20, 20, 20), -1)
    return img


# --------------------------------------------------------------------------
# Lane classification
# --------------------------------------------------------------------------


@pytest.mark.parametrize("lane", [LaneType.GRASS, LaneType.ROAD, LaneType.WATER, LaneType.TRACK])
def test_lane_classification_round_trip(lane):
    """Every lane type in calib.yaml must be recoverable from its own colour."""
    p = Perceiver(CALIB)
    frame = render({r: lane for r in range(-1, 10)})
    obs = p.perceive(frame, t_ms=0.0)
    got = [l.type for r, l in obs.world.lanes.items() if r >= 0]
    assert got, "no lanes classified at all"
    assert all(t is lane for t in got), f"{lane} misread as {set(got)}"


def test_mixed_lanes_are_kept_distinct():
    p = Perceiver(CALIB)
    layout = {0: LaneType.GRASS, 1: LaneType.ROAD, 2: LaneType.WATER, 3: LaneType.TRACK}
    frame = render({**{r: LaneType.GRASS for r in range(-1, 10)}, **layout})
    obs = p.perceive(frame, t_ms=0.0)
    for row, expect in layout.items():
        assert obs.world.lane(row).type is expect, f"row {row}"


def test_unclassifiable_lane_reports_unknown():
    """A colour matching nothing must be UNKNOWN, not a confident wrong guess.

    The planner refuses to enter UNKNOWN. A wasted hop beats a confident guess
    about a lane we cannot see.
    """
    p = Perceiver(CALIB)
    frame = render({r: LaneType.GRASS for r in range(-1, 10)})
    g = CALIB.geometry
    y = int(g["row0_screen_y"] - 2 * g["px_per_row"])
    half = int(g["px_per_row"] / 2)
    cv2.rectangle(frame, (0, y - half), (W, y + half), (128, 0, 128), -1)   # magenta
    obs = p.perceive(frame, t_ms=0.0)
    assert obs.world.lane(2).type is LaneType.UNKNOWN


# --------------------------------------------------------------------------
# Obstacles: pixels -> world columns  (INVARIANT 5)
# --------------------------------------------------------------------------


def test_obstacle_position_round_trips_to_world_columns():
    p = Perceiver(CALIB)
    truth = [(-2.0, 2.0), (1.5, 2.0)]
    frame = render(
        {**{r: LaneType.GRASS for r in range(-1, 10)}, 1: LaneType.ROAD},
        obstacles={1: truth},
    )
    obs = p.perceive(frame, t_ms=0.0)
    found = sorted(o.x for o in obs.world.lane(1).obstacles)
    assert len(found) == 2, f"expected 2 obstacles, got {len(found)}"
    for got, (want, _) in zip(found, sorted(truth)):
        assert got == pytest.approx(want, abs=0.15)


# --------------------------------------------------------------------------
# Velocity  (INVARIANT 2)
# --------------------------------------------------------------------------


def _track_moving_obstacle(xs, dt_ms=16.7, corrupt_at=None, corrupt_to=None):
    p = Perceiver(CALIB)
    last = None
    for i, x in enumerate(xs):
        pos = corrupt_to if (corrupt_at is not None and i == corrupt_at) else x
        frame = render(
            {**{r: LaneType.GRASS for r in range(-1, 10)}, 1: LaneType.ROAD},
            obstacles={1: [(pos, 2.0)]},
        )
        last = p.perceive(frame, t_ms=i * dt_ms)
    return last


def test_velocity_is_recovered():
    dt = 16.7
    vx = 4.0                                   # columns/second
    xs = [-3.0 + vx * (i * dt / 1000.0) for i in range(6)]
    obs = _track_moving_obstacle(xs, dt)
    got = [o.vx for o in obs.world.lane(1).obstacles]
    assert got, "obstacle lost during tracking"
    assert got[0] == pytest.approx(vx, rel=0.15)


def test_one_bad_frame_does_not_wreck_the_velocity():
    """INVARIANT 2: 3-frame median, never a 2-frame difference.

    A single corrupted centroid is a leading cause of unexplained deaths. With a
    2-frame difference this obstacle would read as moving at hundreds of columns
    per second and the planner would treat the whole lane as lethal.
    """
    dt = 16.7
    vx = 4.0
    xs = [-3.0 + vx * (i * dt / 1000.0) for i in range(7)]
    obs = _track_moving_obstacle(xs, dt, corrupt_at=4, corrupt_to=3.5)
    got = [o.vx for o in obs.world.lane(1).obstacles]
    assert got
    assert abs(got[0]) < 4 * abs(vx), f"one bad frame produced vx={got[0]:.1f}"


def test_velocity_needs_three_frames_before_reporting():
    """Better to report zero than to report a two-frame guess."""
    obs = _track_moving_obstacle([-3.0, -2.9], 16.7)
    for o in obs.world.lane(1).obstacles:
        assert o.vx == 0.0
    assert VELOCITY_FRAMES >= 3


def test_static_obstacles_on_grass_have_no_velocity():
    obs = _track_moving_obstacle([0.0] * 5)
    p = Perceiver(CALIB)
    frame = render({r: LaneType.GRASS for r in range(-1, 10)}, obstacles={1: [(0.0, 1.0)]})
    for _ in range(4):
        out = p.perceive(frame, t_ms=0.0)
    for o in out.world.lane(1).obstacles:
        assert o.vx == 0.0


# --------------------------------------------------------------------------
# Screen classification
# --------------------------------------------------------------------------


def test_gameplay_frame_classifies_as_gameplay():
    frame = render({0: LaneType.ROAD, 1: LaneType.WATER, 2: LaneType.GRASS,
                    3: LaneType.ROAD, 4: LaneType.GRASS, 5: LaneType.TRACK,
                    6: LaneType.GRASS, 7: LaneType.ROAD, 8: LaneType.GRASS})
    cls = ScreenClassifier(CALIB).classify(frame)
    assert cls.screen is Screen.GAMEPLAY, cls.detail


def test_busy_unbanded_frame_is_not_gameplay():
    """An interstitial must never be mistaken for a playable field."""
    rng = np.random.default_rng(0)
    frame = rng.integers(0, 255, (H, W, 3), dtype=np.uint8)
    cls = ScreenClassifier(CALIB).classify(frame)
    assert cls.screen is not Screen.GAMEPLAY, cls.detail


def test_close_button_hunt_finds_a_corner_x():
    frame = np.full((H, W, 3), 200, np.uint8)
    cx, cy = int(W * 0.9), int(H * 0.06)
    cv2.rectangle(frame, (cx - 22, cy - 22), (cx + 22, cy + 22), (10, 10, 10), 3)
    cv2.line(frame, (cx - 12, cy - 12), (cx + 12, cy + 12), (10, 10, 10), 3)
    cv2.line(frame, (cx - 12, cy + 12), (cx + 12, cy - 12), (10, 10, 10), 3)

    got = find_close_buttons(frame)
    assert got, "no close-button candidate found"
    bx, by = got[0]
    assert abs(bx - cx) < 40 and abs(by - cy) < 40


def test_close_button_hunt_is_bounded():
    """Adversarial creatives must not produce an unbounded candidate list."""
    rng = np.random.default_rng(1)
    frame = rng.integers(0, 255, (H, W, 3), dtype=np.uint8)
    assert len(find_close_buttons(frame)) <= 6
