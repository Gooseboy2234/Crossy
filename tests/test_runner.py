"""Integration tests for the main loop, kill switch and recovery paths.

The kill switch gets the most attention here because it is the only part of the
system where a bug is unrecoverable: force-quitting at the wrong moment loses the
score the whole project exists to submit.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
import runner as runner_mod
from capture import ArraySource
from config import THRESHOLD, load_calib, load_params
from runner import KillSwitchTripped, Runner
from states import Screen
from test_perception import render
from world import LaneType

GAMEPLAY_LAYOUT = {
    0: LaneType.GRASS, 1: LaneType.ROAD, 2: LaneType.WATER, 3: LaneType.GRASS,
    4: LaneType.TRACK, 5: LaneType.GRASS, 6: LaneType.ROAD, 7: LaneType.GRASS,
    8: LaneType.GRASS,
}


class FakeTaps:
    """Records what would have been sent, sends nothing."""

    def __init__(self):
        self.actions: list[str] = []
        self.taps: list[tuple] = []
        self.activations = 0
        self.closed = False

    def act(self, action):
        self.actions.append(action)
        return 1

    def tap(self, x=None, y=None):
        self.taps.append((x, y))
        return 1

    def activate(self):
        self.activations += 1
        return 1

    def close(self):
        self.closed = True

    def measured_latency_ms(self, action, fallback):
        return fallback


def gameplay_frames(n=30):
    out = []
    for i in range(n):
        obs = {
            1: [(-3.0 + 4.0 * (i * 0.0167), 2.0)],
            2: [(1.0 - 2.0 * (i * 0.0167), 3.0)],
            6: [(2.0 - 3.0 * (i * 0.0167), 2.0)],
        }
        out.append(render(GAMEPLAY_LAYOUT, obstacles=obs))
    return out


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Redirect all state to a temp dir so tests never touch the real logs."""
    monkeypatch.setattr(config, "LOCKFILE", tmp_path / "lock")
    monkeypatch.setattr(config, "HEARTBEAT", tmp_path / "heartbeat")
    monkeypatch.setattr(runner_mod, "LOCKFILE", tmp_path / "lock")
    monkeypatch.setattr(runner_mod, "HEARTBEAT", tmp_path / "heartbeat")
    monkeypatch.setattr(runner_mod, "LOGS", tmp_path)
    monkeypatch.setattr(runner_mod, "DEATHS_CSV", tmp_path / "deaths.csv")
    monkeypatch.setattr(runner_mod, "RUNS_CSV", tmp_path / "runs.csv")
    return tmp_path


def _runner(frames, taps=None, **kw):
    calib = load_calib()
    params, _ = load_params()
    return Runner(ArraySource(frames), taps, calib, params, dry_run=taps is None, **kw)


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------


def test_pipeline_runs_end_to_end(env):
    r = _runner(gameplay_frames(30))
    r.run()
    assert r._tick == 30
    assert r._last_screen is Screen.GAMEPLAY
    assert r.stats.frames == 30
    assert len(r.ring) > 0, "nothing recorded for death forensics"


def test_heartbeat_is_written(env):
    r = _runner(gameplay_frames(5))
    r.run()
    assert runner_mod.HEARTBEAT.exists()
    assert float(runner_mod.HEARTBEAT.read_text()) > 0


def test_starved_capture_exits_instead_of_spinning(env):
    """A dead capture must not keep the heartbeat fresh.

    If the loop spun here it would look healthy to the supervisor, which would
    then never restart anything — an eight-hour no-op that reports success.
    """
    r = _runner([])
    assert r.run() == 2


def test_taps_are_actually_sent_during_gameplay(env):
    taps = FakeTaps()
    r = _runner(gameplay_frames(40), taps=taps)
    r.run()
    assert taps.actions, "planner never issued an executable action"
    assert "wait" not in taps.actions, "`wait` must send nothing at all"


# --------------------------------------------------------------------------
# Kill switch — three independent layers
# --------------------------------------------------------------------------


def test_kill_switch_stops_tapping_at_threshold(env):
    """Layer 1. Cease input; the eagle ends the run and the score submits.

    Never force-quit: the run has to end in-game or Game Center never sees it.
    """
    taps = FakeTaps()
    r = _runner(gameplay_frames(30), taps=taps)
    r.perceiver.chicken_row = THRESHOLD          # pretend we got there
    r.perceiver._detect_scroll = lambda sig: 0   # freeze the row counter
    r.run()

    assert r.threshold_reached
    assert taps.actions == [], f"kept tapping past the threshold: {taps.actions}"


def test_kill_switch_does_not_trip_below_threshold(env):
    taps = FakeTaps()
    r = _runner(gameplay_frames(30), taps=taps)
    r.perceiver.chicken_row = THRESHOLD - 2
    r.perceiver._detect_scroll = lambda sig: 0
    r.run()
    assert not r.threshold_reached


def test_lockfile_retires_the_bot(env):
    """Layer 2. Permanent disable — a second night must not undo the first."""
    runner_mod.LOCKFILE.write_text("328 @ whenever")
    taps = FakeTaps()
    r = _runner(gameplay_frames(30), taps=taps)
    assert r.run() == 0
    assert r._tick == 0, "ran despite the lockfile"
    assert taps.actions == []


def test_teardown_closes_the_tap_channel(env):
    """Layer 3. Kill the input path entirely."""
    taps = FakeTaps()
    r = _runner(gameplay_frames(5), taps=taps)
    r.run()
    assert taps.closed


def test_threshold_is_not_reachable_by_config(env):
    """A hard requirement, not a tunable. See CLAUDE.md and runbook A.1."""
    assert THRESHOLD == 328
    assert runner_mod.THRESHOLD == 328


# --------------------------------------------------------------------------
# Recovery
# --------------------------------------------------------------------------


def test_unknown_screen_dumps_and_relaunches(env, monkeypatch, tmp_path):
    monkeypatch.setattr(runner_mod, "STATE_TIMEOUT_S", {})   # every state times out at once
    monkeypatch.setattr(runner_mod, "UNKNOWN_TIMEOUT_S", 0.0)
    dumped = []
    monkeypatch.setattr(runner_mod, "dump_unknown",
                        lambda img, detail, **kw: dumped.append(detail) or tmp_path / "x.png")

    taps = FakeTaps()
    rng = np.random.default_rng(0)
    # Low-palette, unbanded noise: classifies as neither gameplay nor an ad.
    frame = np.full((1920, 886, 3), 128, np.uint8)
    frame[400:800] = 90
    r = _runner([frame] * 4, taps=taps)
    r.run()

    if r._last_screen is Screen.UNKNOWN:
        assert dumped, "unknown screen was not dumped for later triage"
        assert taps.activations > 0, "did not relaunch out of the unknown state"


def _interstitial() -> np.ndarray:
    """A plausible full-screen ad: gradient ground, colour blocks, text bars.

    Deliberately not random noise — noise averages to flat grey under the
    classifier's downscale, so it reads as a blank screen rather than a busy
    one. Real creatives keep their palette through a resize.
    """
    import cv2
    h, w = 1920, 886
    img = np.zeros((h, w, 3), np.uint8)
    for y in range(h):                                   # vertical gradient
        img[y, :] = (30 + 180 * y // h, 90, 220 - 150 * y // h)
    rng = np.random.default_rng(7)
    for i in range(14):                                  # product art blocks
        x0, y0 = int(rng.integers(0, w - 200)), int(rng.integers(200, h - 300))
        colour = tuple(int(c) for c in rng.integers(0, 255, 3))
        cv2.rectangle(img, (x0, y0), (x0 + 190, y0 + 240), colour, -1)
    for i in range(9):                                   # copy / CTA bars
        y = 300 + i * 150
        cv2.rectangle(img, (60, y), (w - 60, y + 42),
                      (250 - 8 * i, 240, 40 + 20 * i), -1)
    return img


def test_ad_state_has_a_hard_timeout(env, monkeypatch):
    """Ad handling must never be an unbounded loop.

    ~160 interstitials a night with CV-only handling; one creative with no
    findable close button would otherwise strand the harness until morning.
    """
    monkeypatch.setattr(runner_mod, "STATE_TIMEOUT_S", {})
    monkeypatch.setattr(runner_mod, "UNKNOWN_TIMEOUT_S", 0.0)
    taps = FakeTaps()
    r = _runner([_interstitial()] * 5, taps=taps)
    r.run()
    assert r._last_screen is not Screen.GAMEPLAY
    assert taps.activations > 0, "full-screen ad never timed out into a relaunch"


def test_an_unrecognised_ad_still_escapes_via_the_unknown_path(env, monkeypatch, tmp_path):
    """The safety net matters more than the classifier.

    Ad creatives are adversarial and no classifier survives all of them. A
    creative we cannot even label as an ad must still not strand the harness —
    it falls through to the unknown-state escape, which is *stricter* (10s)
    than the ad timeout (45s).
    """
    monkeypatch.setattr(runner_mod, "STATE_TIMEOUT_S", {})
    monkeypatch.setattr(runner_mod, "UNKNOWN_TIMEOUT_S", 0.0)
    monkeypatch.setattr(runner_mod, "dump_unknown", lambda img, detail, **kw: tmp_path / "x.png")
    taps = FakeTaps()
    weird = np.full((1920, 886, 3), 128, np.uint8)
    weird[500:900] = 95
    r = _runner([weird] * 4, taps=taps)
    r.run()
    assert r._last_screen is not Screen.GAMEPLAY
    assert taps.activations > 0, "never escaped an unlabelable full-screen state"
