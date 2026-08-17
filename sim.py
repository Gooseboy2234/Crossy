"""Headless environment with domain randomization.

The structural point of this file (runbook Phase 8): the sim keeps **truth** and
hands the planner a **noisy observation** of it. If the planner saw ground truth,
domain randomization would be decorative and the search would happily tune you to
razor-thin margins that shatter on real perception noise.

  sim.observe()  -> World built from jittered positions/velocities, with frame
                    staleness and occasional lane misclassification
  sim.step(a)    -> resolves the action against the true lanes

Lane traffic is modelled as an infinite periodic train of obstacles, materialised
to a finite window around the playfield at each query. Because the phase is
carried analytically, re-materialising at a later time reproduces exactly the
same physical configuration — no teleporting obstacle, so the planner's
constant-velocity assumption holds exactly.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from config import Params
from plan import FORWARD, LEFT, RIGHT, WAIT, Planner, action_duration
from world import (
    Lane,
    LaneType,
    Obstacle,
    World,
    carrier_at,
    overlap_fraction,
)

CAUSES = ("car", "train", "water_gap", "log_offscreen", "eagle", "unknown")


# --------------------------------------------------------------------------
# Biome / generation
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Biome:
    """Lane mix and obstacle statistics.

    Defaults approximate the base 'Chicken' biome. Runbook A.4 flags character
    selection as an unexplored lever: water is where p lives, so a water-light
    biome is a free reduction. Log a real lane-type distribution from device
    play and drop it in here before tuning.
    """

    name: str = "default"
    lane_weights: Dict[str, float] = field(
        default_factory=lambda: {"grass": 0.34, "road": 0.34, "water": 0.19, "track": 0.13}
    )
    run_len: Dict[str, Tuple[int, int]] = field(
        default_factory=lambda: {"grass": (1, 3), "road": (1, 4), "water": (1, 3), "track": (1, 2)}
    )
    car_speed: Tuple[float, float] = (2.0, 7.0)
    car_width: Tuple[float, float] = (1.0, 2.2)
    car_gap: Tuple[float, float] = (2.5, 7.0)
    log_speed: Tuple[float, float] = (1.2, 3.6)
    log_width: Tuple[float, float] = (2.0, 4.0)
    log_gap: Tuple[float, float] = (1.5, 4.0)
    train_speed: Tuple[float, float] = (16.0, 24.0)
    train_len: Tuple[float, float] = (24.0, 40.0)
    train_period_s: Tuple[float, float] = (3.5, 9.0)
    tree_density: Tuple[float, float] = (0.10, 0.35)


WATER_LIGHT = Biome(
    name="water_light",
    lane_weights={"grass": 0.40, "road": 0.40, "water": 0.08, "track": 0.12},
)


@dataclass
class PeriodicLane:
    """Ground truth for one row."""

    row: int
    type: LaneType
    vx: float = 0.0
    width: float = 0.0
    period: float = 0.0       # world columns between successive obstacle centres
    phase: float = 0.0        # centre position of copy k=0 at t=0
    trees: Tuple[float, ...] = ()

    def materialize(self, t_ms: float, col_min: float, col_max: float, pad: float = 12.0) -> List[Obstacle]:
        """Obstacle centres at absolute time `t_ms`, covering the playfield + pad."""
        if self.type is LaneType.GRASS:
            return [
                Obstacle(self.row, c, 0.9, 0.0, oid=self.row * 1000 + i)
                for i, c in enumerate(self.trees)
            ]
        if self.period <= 0:
            return []
        base = self.phase + self.vx * (t_ms / 1000.0)
        lo, hi = col_min - pad, col_max + pad
        k0 = math.floor((lo - base) / self.period)
        k1 = math.ceil((hi - base) / self.period)
        out: List[Obstacle] = []
        for i, k in enumerate(range(k0, k1 + 1)):
            out.append(
                Obstacle(self.row, base + k * self.period, self.width, self.vx,
                         oid=self.row * 1000 + (k % 997))
            )
        return out


class LaneGenerator:
    def __init__(self, biome: Biome, rng: random.Random, col_min: float, col_max: float):
        self.b = biome
        self.rng = rng
        self.col_min = col_min
        self.col_max = col_max
        self._pending: List[str] = []

    def _next_type(self) -> str:
        if not self._pending:
            names = list(self.b.lane_weights)
            weights = [self.b.lane_weights[n] for n in names]
            t = self.rng.choices(names, weights=weights, k=1)[0]
            lo, hi = self.b.run_len.get(t, (1, 1))
            self._pending = [t] * self.rng.randint(lo, hi)
        return self._pending.pop()

    def make(self, row: int) -> PeriodicLane:
        r = self.rng
        b = self.b
        # Rows 0-2 are always safe ground: the game does the same, and it keeps
        # episode starts from being decided before the planner gets a turn.
        t = "grass" if row <= 2 else self._next_type()

        if t == "grass":
            n_cols = int(self.col_max - self.col_min) + 1
            density = r.uniform(*b.tree_density) if row > 2 else 0.0
            trees = tuple(
                float(c)
                for c in range(int(self.col_min), int(self.col_max) + 1)
                if r.random() < density
            )
            return PeriodicLane(row, LaneType.GRASS, trees=trees)

        if t == "road":
            w = r.uniform(*b.car_width)
            return PeriodicLane(
                row, LaneType.ROAD,
                vx=r.choice([-1, 1]) * r.uniform(*b.car_speed),
                width=w,
                period=w + r.uniform(*b.car_gap),
                phase=r.uniform(-10.0, 10.0),
            )

        if t == "water":
            w = r.uniform(*b.log_width)
            return PeriodicLane(
                row, LaneType.WATER,
                vx=r.choice([-1, 1]) * r.uniform(*b.log_speed),
                width=w,
                period=w + r.uniform(*b.log_gap),
                phase=r.uniform(-10.0, 10.0),
            )

        speed = r.choice([-1, 1]) * r.uniform(*b.train_speed)
        length = r.uniform(*b.train_len)
        return PeriodicLane(
            row, LaneType.TRACK,
            vx=speed,
            width=length,
            period=abs(speed) * r.uniform(*b.train_period_s) + length,
            phase=r.uniform(-40.0, 40.0),
        )


# --------------------------------------------------------------------------
# Domain randomization
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Noise:
    """Runbook Phase 8. Tune against the *noisy* sim; those params transfer.

    `pos_jitter_cols` is quoted in pixels in the runbook (±3-5px); at the
    placeholder 78 px/col that is ~0.05 col.
    """

    pos_jitter_cols: float = 0.05
    vel_rel_error: float = 0.08
    latency_jitter_ms: float = 40.0
    stale_frame_prob: float = 0.02
    lane_misclass_prob: float = 0.005
    stale_frame_age_ms: float = 33.0

    @staticmethod
    def none() -> "Noise":
        return Noise(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)


# --------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------


@dataclass
class Death:
    row: int
    lane_type: str
    cause: str


@dataclass
class EpisodeResult:
    score: int
    death: Optional[Death]
    rows_by_lane: Dict[str, int]
    ticks: int
    ms: float

    @property
    def rows_total(self) -> int:
        return sum(self.rows_by_lane.values())


class CrossySim:
    def __init__(
        self,
        params: Params,
        seed: int = 0,
        biome: Optional[Biome] = None,
        noise: Optional[Noise] = None,
        col_min: float = -5.0,
        col_max: float = 5.0,
        horizon_rows: int = 8,
    ):
        self.p = params
        self.rng = random.Random(seed)
        self.noise = noise if noise is not None else Noise()
        self.col_min, self.col_max = col_min, col_max
        self.horizon = horizon_rows
        self.gen = LaneGenerator(biome or Biome(), self.rng, col_min, col_max)
        self.lanes: Dict[int, PeriodicLane] = {}
        self.reset()

    # -- lifecycle ---------------------------------------------------------

    def reset(self) -> None:
        self.t = 0.0
        self.row = 0
        self.col = 0.0
        self.log_id = -1
        self.obs_col = 0.0
        self.score = 0
        self.max_row = 0
        self.ms_since_forward = 0.0
        self.dead: Optional[Death] = None
        self.rows_by_lane: Dict[str, int] = {k.value: 0 for k in LaneType}
        self.lanes.clear()
        self._ensure(self.horizon)

    def _ensure(self, upto: int) -> None:
        for r in range(len(self.lanes), upto + 1):
            self.lanes[r] = self.gen.make(r)

    def truth_lane(self, row: int) -> PeriodicLane:
        self._ensure(row)
        return self.lanes[row]

    # -- observation -------------------------------------------------------

    def observe(self) -> World:
        """A World as perception would report it: stale, jittered, occasionally wrong."""
        n = self.noise
        # The frame we are acting on is already `latency_offset_ms` old by the
        # time the input lands. Modelling latency HERE — as staleness of the
        # observation — rather than as an extra delay on every action is the
        # faithful version: the capture→CV→socket pipeline runs concurrently
        # with the chicken hopping, it does not serialize behind it.
        #
        # Charging it per action instead made every hop take 330ms rather than
        # 130ms, which tripled road exposure and starved forward progress into
        # eagle deaths. That was a bug in the environment, not in the planner.
        t_obs = self.t - self.p.latency_offset_ms
        if n.stale_frame_prob and self.rng.random() < n.stale_frame_prob:
            t_obs -= n.stale_frame_age_ms

        # The chicken's own column comes from the same stale frame as everything
        # else. On land that is harmless — the column is on a grid and does not
        # move. On a log it is the whole ballgame: the chicken drifts during the
        # latency window too, and handing the planner a *fresh* column beside a
        # *stale* world made it over-predict its own drift by latency × log speed
        # (~0.46 columns, most of a chicken). That single inconsistency was
        # ~75% of all water deaths.
        self.obs_col = self.col
        if self.log_id >= 0:
            log = self._true_log(self.row, self.log_id, self.t)
            if log is not None:
                self.obs_col = self.col + log.vx * (t_obs - self.t) / 1000.0

        self._ensure(self.row + self.horizon)
        lanes: Dict[int, Lane] = {}
        for r in range(max(0, self.row - 1), self.row + self.horizon + 1):
            tl = self.truth_lane(r)
            ltype = tl.type
            if n.lane_misclass_prob and self.rng.random() < n.lane_misclass_prob:
                ltype = self.rng.choice([LaneType.GRASS, LaneType.ROAD, LaneType.WATER, LaneType.TRACK])
            obs = []
            for ob in tl.materialize(t_obs, self.col_min, self.col_max):
                x = ob.x + self.rng.gauss(0.0, n.pos_jitter_cols) if n.pos_jitter_cols else ob.x
                vx = ob.vx * (1.0 + self.rng.gauss(0.0, n.vel_rel_error)) if n.vel_rel_error else ob.vx
                obs.append(Obstacle(r, x, ob.width, vx, oid=ob.oid))
            lanes[r] = Lane(row=r, type=ltype, obstacles=obs)

        return World(lanes=lanes, col_min=self.col_min, col_max=self.col_max, t_ref_ms=t_obs)

    def latency_error(self) -> float:
        """Only the *unpredicted* part of latency.

        The nominal offset is already modelled as observation staleness, and the
        planner compensates for it. What actually kills you is the jitter it
        could not have known about.
        """
        j = self.noise.latency_jitter_ms
        return self.rng.uniform(-j, j) if j else 0.0

    # -- dynamics ----------------------------------------------------------

    def step(self, action: str) -> None:
        """Resolve `action` against ground truth. Sets `self.dead` on death."""
        if self.dead:
            return
        p = self.p
        # The input lands late and the world keeps moving in the meantime.
        # Charging the *jittered* latency here is what makes latency_offset_ms a
        # tunable rather than a declaration: the planner assumed the nominal
        # value, the sim delivers the real one, and the gap is what kills you.
        dt = action_duration(p, action) + self.latency_error()
        t_arr = self.t + dt

        # drift while riding
        col = self.col
        if self.log_id >= 0:
            log = self._true_log(self.row, self.log_id, self.t)
            if log is not None:
                col = self.col + log.vx * (dt / 1000.0)

        if action == FORWARD:
            target_row, target_col = self.row + 1, col
        elif action == LEFT:
            target_row, target_col = self.row, col - 1.0
        elif action == RIGHT:
            target_row, target_col = self.row, col + 1.0
        else:
            target_row, target_col = self.row, col

        self.t = t_arr

        if not (self.col_min <= target_col <= self.col_max):
            if self.log_id >= 0:
                return self._die(self.row, "log_offscreen")
            # Walking into the wall is a no-op in game, not a death.
            target_col = min(self.col_max, max(self.col_min, target_col))
            target_row = self.row

        lane = self.truth_lane(target_row)
        obstacles = lane.materialize(t_arr, self.col_min, self.col_max)

        if lane.type is LaneType.GRASS:
            for ob in obstacles:  # trees block, they do not kill
                if overlap_fraction(ob, target_col, p.chicken_width, 0.0) > 0.15:
                    target_row, target_col = self.row, self.col
                    break

        advanced = target_row > self.row
        prev_row = self.row
        self.row, self.col = target_row, float(target_col)
        if lane.type is not LaneType.WATER:
            if self.log_id >= 0:
                self.col = float(round(self.col))   # step off the log onto the grid
            self.log_id = -1

        if advanced:
            self.max_row = max(self.max_row, self.row)
            self.score = self.max_row
            self.ms_since_forward = 0.0
            self.rows_by_lane[lane.type.value] = self.rows_by_lane.get(lane.type.value, 0) + 1
        else:
            self.ms_since_forward += dt

        # --- lethality, evaluated on truth at the true arrival time ---------
        if lane.type is LaneType.ROAD:
            for ob in obstacles:
                if overlap_fraction(ob, self.col, p.chicken_width, 0.0) > 0.0:
                    return self._die(self.row, "car")
        elif lane.type is LaneType.TRACK:
            for ob in obstacles:
                if overlap_fraction(ob, self.col, p.chicken_width, 0.0) > 0.0:
                    return self._die(self.row, "train")
        elif lane.type is LaneType.WATER:
            carrier = carrier_at(
                Lane(target_row, LaneType.WATER, obstacles), self.col, p.chicken_width, 0.0, 0.5
            )
            if carrier is None:
                return self._die(self.row, "water_gap")
            self.log_id = carrier.oid
            if not (self.col_min <= self.col <= self.col_max):
                return self._die(self.row, "log_offscreen")

        if self.ms_since_forward >= p.eagle_death_ms:
            return self._die(self.row, "eagle")

    def _true_log(self, row: int, oid: int, t_ms: float) -> Optional[Obstacle]:
        for ob in self.truth_lane(row).materialize(t_ms, self.col_min, self.col_max):
            if ob.oid == oid:
                return ob
        return None

    def _die(self, row: int, cause: str) -> None:
        self.dead = Death(row=row, lane_type=self.truth_lane(row).type.value, cause=cause)

    # -- episode -----------------------------------------------------------

    def run_episode(self, planner: Optional[Planner] = None, max_score: int = 400,
                    max_ticks: int = 20000) -> EpisodeResult:
        planner = planner or Planner(self.p)
        self.reset()
        ticks = 0
        while self.dead is None and self.score < max_score and ticks < max_ticks:
            world = self.observe()
            res = planner.plan(world, self.row, self.obs_col, self.log_id, self.ms_since_forward)
            self.step(res.action)
            ticks += 1
        return EpisodeResult(
            score=self.score,
            death=self.dead,
            rows_by_lane=dict(self.rows_by_lane),
            ticks=ticks,
            ms=self.t,
        )


# --------------------------------------------------------------------------
# p̂ estimation
# --------------------------------------------------------------------------


@dataclass
class PHat:
    deaths: int
    rows: int
    by_lane: Dict[str, Tuple[int, int]]   # lane -> (deaths, rows)
    by_cause: Dict[str, int]
    episodes: int
    scores: List[int] = field(default_factory=list)

    @property
    def p(self) -> float:
        return self.deaths / self.rows if self.rows else 1.0

    @property
    def rel_se(self) -> float:
        """Relative standard error ~= 1/sqrt(deaths). Always report it (CLAUDE.md)."""
        return 1.0 / math.sqrt(self.deaths) if self.deaths else float("inf")

    def lane_p(self, lane: str) -> Tuple[float, int]:
        d, r = self.by_lane.get(lane, (0, 0))
        return (d / r if r else 0.0), d

    def p_reach(self, target: int = 330) -> float:
        return (1.0 - self.p) ** target

    def __str__(self) -> str:
        bits = [f"p̂={self.p*100:.2f}% ±{self.rel_se*100:.0f}% ({self.deaths} deaths / {self.rows} rows)"]
        for lane in ("water", "road", "track", "grass"):
            p, d = self.lane_p(lane)
            if d or self.by_lane.get(lane, (0, 0))[1]:
                bits.append(f"{lane}={p*100:.2f}% ({d}d)")
        bits.append(f"P(reach 330)={self.p_reach()*100:.2f}%")
        return "  ".join(bits)


def estimate_p_hat(
    params: Params,
    deaths_target: int = 100,
    seed: int = 0,
    biome: Optional[Biome] = None,
    noise: Optional[Noise] = None,
    max_episodes: int = 4000,
    max_score: int = 400,
) -> PHat:
    """Run episodes until `deaths_target` deaths accumulate.

    Deaths, not episodes: relative SE of p̂ is ~1/sqrt(deaths), so the death count
    is the thing that has to be budgeted (runbook §9.2).
    """
    by_lane: Dict[str, List[int]] = {k.value: [0, 0] for k in LaneType}
    by_cause: Dict[str, int] = {c: 0 for c in CAUSES}
    deaths = rows = episodes = 0
    scores: List[int] = []

    sim = CrossySim(params, seed=seed, biome=biome, noise=noise)
    planner = Planner(params)

    while deaths < deaths_target and episodes < max_episodes:
        sim.rng.seed(seed * 1_000_003 + episodes)
        sim.gen.rng = sim.rng
        r = sim.run_episode(planner, max_score=max_score)
        episodes += 1
        scores.append(r.score)
        for lane, n in r.rows_by_lane.items():
            by_lane.setdefault(lane, [0, 0])[1] += n
            rows += n
        if r.death:
            deaths += 1
            by_lane.setdefault(r.death.lane_type, [0, 0])[0] += 1
            by_cause[r.death.cause] = by_cause.get(r.death.cause, 0) + 1

    return PHat(
        deaths=deaths,
        rows=rows,
        by_lane={k: (v[0], v[1]) for k, v in by_lane.items()},
        by_cause=by_cause,
        episodes=episodes,
        scores=scores,
    )


if __name__ == "__main__":
    import argparse

    from config import load_params

    ap = argparse.ArgumentParser(description="Headless Crossy Road sim")
    ap.add_argument("--deaths", type=int, default=100)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--clean", action="store_true", help="no domain randomization (diagnostic only)")
    ap.add_argument("--water-light", action="store_true")
    args = ap.parse_args()

    params, _ = load_params()
    ph = estimate_p_hat(
        params,
        deaths_target=args.deaths,
        seed=args.seed,
        noise=Noise.none() if args.clean else None,
        biome=WATER_LIGHT if args.water_light else None,
    )
    print(ph)
    print("causes:", {k: v for k, v in sorted(ph.by_cause.items(), key=lambda kv: -kv[1]) if v})
