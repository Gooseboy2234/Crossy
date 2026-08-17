"""World model and collision math.

The single source of truth for "does the chicken get hit". The planner and the
simulator both call into here, which is what makes sim results mean anything.

CONVENTION (CLAUDE.md invariant 5): everything in this module is in **world
columns**, never pixels. perceive.py converts at the boundary.

Time is milliseconds throughout, except `Obstacle.x_at` which takes seconds
because velocities are columns/second.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Iterable, List, Optional, Tuple


class LaneType(str, Enum):
    GRASS = "grass"
    ROAD = "road"
    WATER = "water"
    TRACK = "track"
    UNKNOWN = "unknown"

    @property
    def is_lethal_terrain(self) -> bool:
        """True where standing still on the terrain itself kills you."""
        return self is LaneType.WATER


#: Terrain the chicken can stand on indefinitely without a carrier.
SOLID = (LaneType.GRASS, LaneType.ROAD, LaneType.TRACK)


@dataclass
class Obstacle:
    """A car, train, log or tree, in world columns.

    `x` is the centre at the reference time of the World that owns it.
    """

    row: int
    x: float
    width: float
    vx: float = 0.0
    oid: int = -1

    def x_at(self, dt_s: float) -> float:
        return self.x + self.vx * dt_s

    def span_at(self, dt_s: float) -> Tuple[float, float]:
        c = self.x_at(dt_s)
        h = 0.5 * self.width
        return c - h, c + h


@dataclass
class Lane:
    row: int
    type: LaneType
    obstacles: List[Obstacle] = field(default_factory=list)
    #: Perception confidence, 0-1. Sub-1 values are informational; the planner
    #: treats an UNKNOWN lane as impassable rather than gambling on it.
    confidence: float = 1.0


@dataclass
class World:
    """A snapshot. `t_ref_ms` is the timestamp all obstacle positions are given at.

    Every query takes an absolute time and converts internally, so callers can
    never accidentally mix a plan-relative and a wall-clock time.
    """

    lanes: Dict[int, Lane] = field(default_factory=dict)
    col_min: float = -5.0
    col_max: float = 5.0
    t_ref_ms: float = 0.0

    def lane(self, row: int) -> Lane:
        # Shared sentinel: this is the hottest call in the planner and building a
        # throwaway Lane per miss showed up as 5% of total runtime.
        return self.lanes.get(row) or _UNKNOWN_LANE

    def obstacle(self, oid: int) -> Optional[Obstacle]:
        for lane in self.lanes.values():
            for ob in lane.obstacles:
                if ob.oid == oid:
                    return ob
        return None

    def in_bounds(self, col: float) -> bool:
        return self.col_min - 1e-9 <= col <= self.col_max + 1e-9


#: Returned for any row perception has not classified. Never mutated.
_UNKNOWN_LANE = Lane(row=-10_000, type=LaneType.UNKNOWN)


# --------------------------------------------------------------------------
# Exact swept tests.
#
# Both are analytic rather than sampled. A sampled check misses a fast car that
# passes entirely between two sample times, and a train at 20 cols/s crosses the
# chicken's footprint in under 50ms — well inside any sane sampling interval.
# --------------------------------------------------------------------------


def collides_in_window(
    ob: Obstacle,
    col: float,
    chicken_width: float,
    t0_ms: float,
    t1_ms: float,
    t_ref_ms: float = 0.0,
) -> bool:
    """Does `ob` overlap a chicken parked at `col` at ANY instant in [t0, t1]?

    The obstacle centre moves linearly, so the set of times where
    |centre(t) - col| < R is a single interval; we solve for it and intersect.
    """
    R = 0.5 * (ob.width + chicken_width)
    if R <= 0:
        return False
    # centre(t) = ob.x + vx * (t - t_ref) / 1000
    d0 = ob.x - col + ob.vx * (t0_ms - t_ref_ms) / 1000.0
    if abs(ob.vx) < 1e-9:
        return abs(d0) < R
    # d(t0 + s) = d0 + vx * s / 1000, s in [0, t1 - t0]
    span_ms = t1_ms - t0_ms
    lo_s = (-R - d0) * 1000.0 / ob.vx
    hi_s = (R - d0) * 1000.0 / ob.vx
    if lo_s > hi_s:
        lo_s, hi_s = hi_s, lo_s
    return hi_s > 0.0 and lo_s < span_ms


def time_to_overlap(
    ob: Obstacle,
    col: float,
    chicken_width: float,
    t_from_ms: float,
    t_ref_ms: float = 0.0,
) -> float:
    """Milliseconds from `t_from_ms` until `ob` starts overlapping `col`.

    0.0 if it already overlaps, `inf` if it never will. Same closed-form
    interval as `collides_in_window`, but reporting *when* rather than *whether*
    — which is what you need to rank options once every option is bad.
    """
    R = 0.5 * (ob.width + chicken_width)
    d0 = ob.x - col + ob.vx * (t_from_ms - t_ref_ms) / 1000.0
    if abs(ob.vx) < 1e-9:
        return 0.0 if abs(d0) < R else float("inf")
    lo_s = (-R - d0) * 1000.0 / ob.vx
    hi_s = (R - d0) * 1000.0 / ob.vx
    if lo_s > hi_s:
        lo_s, hi_s = hi_s, lo_s
    if hi_s <= 0.0:
        return float("inf")     # already gone past
    return max(0.0, lo_s)


def time_to_hazard(
    lane: Lane, col: float, chicken_width: float, t_from_ms: float, t_ref_ms: float = 0.0
) -> float:
    """How long a chicken parked at `col` in `lane` stays untouched."""
    if not lane.obstacles:
        return float("inf")
    return min(
        time_to_overlap(ob, col, chicken_width, t_from_ms, t_ref_ms) for ob in lane.obstacles
    )


def overlap_fraction(ob: Obstacle, col: float, chicken_width: float, dt_s: float) -> float:
    """Fraction of the chicken's footprint sitting on `ob` at `dt_s`."""
    lo, hi = ob.span_at(dt_s)
    a = col - 0.5 * chicken_width
    b = col + 0.5 * chicken_width
    inter = min(b, hi) - max(a, lo)
    return max(0.0, inter) / chicken_width


def min_overlap_in_window(
    ob: Obstacle,
    col: float,
    chicken_width: float,
    t0_ms: float,
    t1_ms: float,
    t_ref_ms: float = 0.0,
) -> float:
    """Worst-case footprint overlap over [t0, t1].

    Overlap length as a function of time is a clipped tent — concave — so its
    minimum over an interval is attained at an endpoint. Two evaluations is
    exact, no sampling required.
    """
    f0 = overlap_fraction(ob, col, chicken_width, (t0_ms - t_ref_ms) / 1000.0)
    f1 = overlap_fraction(ob, col, chicken_width, (t1_ms - t_ref_ms) / 1000.0)
    return min(f0, f1)


def blocking_obstacle(
    lane: Lane,
    col: float,
    chicken_width: float,
    t0_ms: float,
    t1_ms: float,
    t_ref_ms: float = 0.0,
) -> Optional[Obstacle]:
    """First obstacle in `lane` that would hit a chicken at `col` during the window."""
    for ob in lane.obstacles:
        if collides_in_window(ob, col, chicken_width, t0_ms, t1_ms, t_ref_ms):
            return ob
    return None


def best_carrier(
    lane: Lane,
    col: float,
    chicken_width: float,
    t0_ms: float,
    t1_ms: float,
    min_overlap: float,
    t_ref_ms: float = 0.0,
) -> Optional[Obstacle]:
    """The log giving the most worst-case footprint support, if any clears `min_overlap`.

    Used for water. Enforcing the threshold across the *whole* safety window
    rather than at the nominal landing instant is what makes
    `log_entry_min_overlap` robust to latency jitter.
    """
    best: Optional[Obstacle] = None
    best_f = min_overlap
    for ob in lane.obstacles:
        f = min_overlap_in_window(ob, col, chicken_width, t0_ms, t1_ms, t_ref_ms)
        if f >= best_f:
            best, best_f = ob, f
    return best


def carrier_at(
    lane: Lane, col: float, chicken_width: float, dt_s: float, min_overlap: float
) -> Optional[Obstacle]:
    """Ground-truth 'am I standing on a log' check, single instant."""
    for ob in lane.obstacles:
        if overlap_fraction(ob, col, chicken_width, dt_s) >= min_overlap:
            return ob
    return None
