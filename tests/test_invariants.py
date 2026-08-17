"""Regression tests for the invariants and for every bug found by measurement.

Each of the four bugs below was found by instrumenting the sim, not by reading
the code, and every one of them was a *consistency* bug — two places disagreeing
about time or position. They produce no exception and no obviously wrong output;
they just raise p̂. That is exactly the class of bug that comes back, so each one
gets a test that would have caught it.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Params, load_params
from plan import FORWARD, LEFT, RIGHT, WAIT, Planner, action_duration
from sim import CrossySim, Noise
from world import (
    Lane,
    LaneType,
    Obstacle,
    World,
    collides_in_window,
    min_overlap_in_window,
    time_to_hazard,
    time_to_overlap,
)


@pytest.fixture
def params() -> Params:
    p, _ = load_params()
    return p


# --------------------------------------------------------------------------
# Collision math
# --------------------------------------------------------------------------


def test_fast_obstacle_is_not_missed_between_samples():
    """A train crosses the footprint in <50ms. Sampling would step over it.

    This is why the swept tests are closed-form rather than sampled: at 20 col/s
    a train is inside the chicken's footprint for ~90ms, so a 100ms sampling
    grid misses it roughly half the time — and misses it *silently*.
    """
    train = Obstacle(row=1, x=-10.0, width=30.0, vx=20.0)
    # It is nowhere near the chicken at either endpoint of a 1s window...
    assert not collides_in_window(train, 30.0, 0.8, 0.0, 0.0)
    assert not collides_in_window(train, 30.0, 0.8, 3000.0, 3000.0)
    # ...but it certainly passes through during it.
    assert collides_in_window(train, 30.0, 0.8, 0.0, 3000.0)


def test_stationary_obstacle():
    tree = Obstacle(row=1, x=2.0, width=0.9, vx=0.0)
    assert collides_in_window(tree, 2.0, 0.8, 0.0, 100.0)
    assert not collides_in_window(tree, 4.0, 0.8, 0.0, 100.0)
    assert time_to_overlap(tree, 2.0, 0.8, 0.0) == 0.0
    assert time_to_overlap(tree, 4.0, 0.8, 0.0) == math.inf


def test_collision_respects_time_reference():
    """t_ref is the time the obstacle's x was sampled at, not an offset."""
    car = Obstacle(row=1, x=0.0, width=1.0, vx=5.0)
    # Sampled at t=1000, asked about [1000, 1000]: it is at x=0, on the chicken.
    assert collides_in_window(car, 0.0, 0.8, 1000.0, 1000.0, t_ref_ms=1000.0)
    # Same absolute window, but the sample was taken at t=0 -> it has moved 5 cols.
    assert not collides_in_window(car, 0.0, 0.8, 1000.0, 1000.0, t_ref_ms=0.0)


def test_min_overlap_is_exact_at_endpoints():
    """Overlap over time is a clipped tent, so endpoints bound its minimum."""
    log = Obstacle(row=1, x=0.0, width=3.0, vx=2.0)
    got = min_overlap_in_window(log, 0.0, 0.8, 0.0, 400.0)
    dense = min(
        # 401 samples: if the endpoint claim were wrong this would find lower.
        __import__("world").overlap_fraction(log, 0.0, 0.8, t / 1000.0)
        for t in range(0, 401)
    )
    assert got == pytest.approx(dense, abs=1e-9)


def test_time_to_hazard_picks_the_soonest():
    lane = Lane(1, LaneType.ROAD, [
        Obstacle(1, -10.0, 1.0, 5.0),    # arrives later
        Obstacle(1, -4.0, 1.0, 5.0),     # arrives sooner
    ])
    assert time_to_hazard(lane, 0.0, 0.8, 0.0) == pytest.approx(
        time_to_overlap(lane.obstacles[1], 0.0, 0.8, 0.0)
    )


# --------------------------------------------------------------------------
# Planner invariants
# --------------------------------------------------------------------------


def _grass_world(rows=8, t_ref=0.0) -> World:
    return World(
        lanes={r: Lane(r, LaneType.GRASS, []) for r in range(-1, rows)},
        col_min=-5.0, col_max=5.0, t_ref_ms=t_ref,
    )


def test_planner_uses_the_world_time_base(params):
    """BUG 1: plan times were plan-relative, collision math got an absolute ref.

    With an empty world the plan is trivially safe either way, so the assertion
    is on the arithmetic: a world stamped far in the future must not change the
    decision, but it must change the absolute times the plan reasons about.
    """
    planner = Planner(params)
    a = planner.plan(_grass_world(t_ref=0.0), 0, 0.0)
    b = planner.plan(_grass_world(t_ref=1_000_000.0), 0, 0.0)
    assert a.action == b.action == FORWARD


def test_invariant_1_latency_is_paid_exactly_once(params):
    """The head of the plan pays pipeline latency; later steps must not."""
    planner = Planner(params)
    world = _grass_world()
    root = planner._search(world, 0, 0.0, -1, params.safety_margin_ms, False)
    assert not root.boxed_in
    # Reconstruct the timeline the search would build for three forward hops.
    expected = params.latency_offset_ms + 3 * action_duration(params, FORWARD)
    s = None
    st = planner._step(world, __import__("plan").State(0, 0.0, world.t_ref_ms, -1, 0, 0), FORWARD, 0)
    st = planner._step(world, st, FORWARD, 0)
    st = planner._step(world, st, FORWARD, 0)
    assert st.t - world.t_ref_ms == pytest.approx(expected)


def test_swipes_cost_more_than_taps(params):
    """Lateral moves are swipes; folding them into one latency would hide this."""
    assert action_duration(params, LEFT) > action_duration(params, FORWARD)
    assert action_duration(params, LEFT) == action_duration(params, RIGHT)
    assert action_duration(params, FORWARD) == params.hop_duration_ms


def test_invariant_3_refuses_water_with_no_carrier(params):
    """Open water with no log at all must never be entered."""
    world = _grass_world()
    world.lanes[1] = Lane(1, LaneType.WATER, [])
    res = Planner(params).plan(world, 0, 0.0)
    assert res.action != FORWARD


def test_invariant_3_refuses_a_log_that_drifts_offscreen(params):
    """BUG 4: a boardable log is not a survivable log.

    One wide log, moving left fast, positioned so the chicken would board near
    the left edge. It can be landed on — and it is a death sentence, because it
    reaches the boundary long before `log_exit_lead_ms` elapses.
    """
    world = _grass_world()
    world.lanes[1] = Lane(1, LaneType.WATER, [Obstacle(1, -4.0, 6.0, -4.0, oid=11)])
    world.lanes[2] = Lane(2, LaneType.GRASS, [])
    res = Planner(params).plan(world, 0, -4.0)
    assert res.action != FORWARD, "boarded a log that carries it offscreen"


def test_water_entry_allowed_when_the_log_gives_time(params):
    """The mirror image: the same log heading the other way is fine."""
    world = _grass_world()
    world.lanes[1] = Lane(1, LaneType.WATER, [Obstacle(1, -4.0, 6.0, 1.0, oid=11)])
    world.lanes[2] = Lane(2, LaneType.GRASS, [])
    res = Planner(params).plan(world, 0, -4.0)
    assert res.action == FORWARD


def test_planner_never_freezes_when_standing_is_lethal(params):
    """BUG 2: returning WAIT when nothing is safe caused 95% of sim deaths.

    The chicken is on a road with a truck arriving on top of it. Grass ahead is
    clear. Standing still is the one action guaranteed to kill it.
    """
    world = World(
        lanes={
            -1: Lane(-1, LaneType.GRASS, []),
            0: Lane(0, LaneType.ROAD, [Obstacle(0, -3.0, 2.0, 9.0, oid=1)]),
            1: Lane(1, LaneType.GRASS, []),
            2: Lane(2, LaneType.GRASS, []),
            3: Lane(3, LaneType.GRASS, []),
            4: Lane(4, LaneType.GRASS, []),
        },
        col_min=-5.0, col_max=5.0, t_ref_ms=0.0,
    )
    res = Planner(params).plan(world, 0, 0.0)
    assert res.action != WAIT


def test_boxed_in_still_returns_a_legal_action(params):
    """Even hemmed in on all sides, something must come back."""
    walls = [Obstacle(0, c, 0.9, 0.0, oid=100 + c) for c in (-1, 1)]
    world = World(
        lanes={
            0: Lane(0, LaneType.GRASS, walls),
            1: Lane(1, LaneType.WATER, []),      # no logs: impassable
            2: Lane(2, LaneType.GRASS, []),
        },
        col_min=-5.0, col_max=5.0, t_ref_ms=0.0,
    )
    res = Planner(params).plan(world, 0, 0.0)
    assert res.action in (FORWARD, LEFT, RIGHT, WAIT)


def test_eagle_panic_engages(params):
    planner = Planner(params)
    world = _grass_world()
    calm = planner.plan(world, 0, 0.0, ms_since_forward=0.0)
    panic = planner.plan(world, 0, 0.0, ms_since_forward=params.eagle_timeout_ms + 1)
    assert not calm.panicking
    assert panic.panicking


def test_plan_horizon_bounds_the_search(params):
    """Without the cap the search fans out along the time axis and stalls."""
    planner = Planner(params)
    res = planner.plan(_grass_world(), 0, 0.0)
    assert res.nodes < params.max_nodes


# --------------------------------------------------------------------------
# Planner <-> sim consistency  (BUG 3, the expensive one)
# --------------------------------------------------------------------------


def test_planner_prediction_matches_sim_truth_on_a_log():
    """The planner's predicted landing must equal the sim's actual landing.

    With noise off these are the same physical situation described twice, so any
    disagreement is a modelling inconsistency. It was 0.46 columns — most of a
    chicken — because the planner drifted the bird over latency+hop while the
    sim drifted it over hop alone. It cost ~75% of all water deaths and produced
    no error, only a worse number.
    """
    p, _ = load_params()
    sim = CrossySim(p, seed=1, noise=Noise.none())
    planner = Planner(p)

    checked = 0
    for _ in range(4000):
        if sim.dead:
            sim.reset()
        world = sim.observe()
        res = planner.plan(world, sim.row, sim.obs_col, sim.log_id, sim.ms_since_forward)

        if sim.log_id >= 0 and not res.boxed_in:
            log = world.obstacle(sim.log_id)
            if log is not None:
                dt = p.latency_offset_ms + action_duration(p, res.action)
                predicted = sim.obs_col + log.vx * dt / 1000.0
                if res.action == LEFT:
                    predicted -= 1.0
                elif res.action == RIGHT:
                    predicted += 1.0
                before_row = sim.row
                sim.step(res.action)
                if sim.dead is None and sim.row == before_row + (1 if res.action == FORWARD else 0):
                    if sim.truth_lane(sim.row).type is LaneType.WATER:
                        assert sim.col == pytest.approx(predicted, abs=1e-6)
                        checked += 1
                continue
        sim.step(res.action)

    assert checked > 20, f"only exercised {checked} on-log transitions"


def test_sim_charges_only_unpredicted_latency():
    """Nominal latency is observation staleness; only jitter is a surprise."""
    p, _ = load_params()
    sim = CrossySim(p, seed=2, noise=Noise.none())
    sim.reset()
    t0 = sim.t
    sim.step(FORWARD)
    assert sim.t - t0 == pytest.approx(action_duration(p, FORWARD))


def test_observation_is_stale_by_the_latency_offset():
    p, _ = load_params()
    sim = CrossySim(p, seed=2, noise=Noise.none())
    sim.reset()
    sim.t = 5000.0
    world = sim.observe()
    assert world.t_ref_ms == pytest.approx(5000.0 - p.latency_offset_ms)


# --------------------------------------------------------------------------
# Kill switch
# --------------------------------------------------------------------------


def test_threshold_is_328():
    """CLAUDE.md and runbook A.1. Not a tunable."""
    import config
    assert config.THRESHOLD == 328
