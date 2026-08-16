"""Dijkstra over (row, col, t), latency-compensated.

Greedy per-row gets you p ~= 3%, which is infeasible (runbook §0). The lookahead
here is the difference between 3% and 1%.

Three invariants live in this file and nowhere else:

  * INVARIANT 1 — every collision check evaluates at
    `t + latency_offset_ms + hop_duration_ms`, never at `t`. Implemented
    structurally: the root sits at `t = 0`, the *first* step adds
    `latency_offset_ms` (the capture→CV→socket pipeline delay, paid once), and
    every step then adds its own duration. No caller can forget it because
    there is no code path that produces an arrival time without it.

    Lateral steps additionally pay `swipe_extra_latency_ms`. They are swipes —
    `press(forDuration:thenDragTo:)`, not `tap()` — and they are slower every
    time they execute, not just the first. See docs/2026-reality-check.md §1.

  * INVARIANT 3 — never enter water without a verified exit. `_has_exit` gates
    every transition into a water row, including lateral shuffles between logs.

  * INVARIANT 5 — world columns only. No pixels reach this module.

Cost is measured in *hop units* rather than milliseconds so that
`lateral_move_cost` and `column_center_bias` are dimensionless and their search
bounds mean the same thing regardless of `hop_duration_ms`.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from config import Params
from world import (
    SOLID,
    LaneType,
    Obstacle,
    World,
    best_carrier,
    blocking_obstacle,
    time_to_hazard,
)

FORWARD, LEFT, RIGHT, WAIT = "forward", "left", "right", "wait"
ACTIONS: Tuple[str, ...] = (FORWARD, LEFT, RIGHT, WAIT)

#: Dedup grids. Coarse enough to keep the frontier small, fine enough that two
#: states landing in the same bucket really are interchangeable.
COL_QUANT = 0.25
T_QUANT_MS = 25.0


def action_duration(p: Params, action: str) -> float:
    """Wall-clock cost of executing `action`, excluding the pipeline latency.

    Shared by the planner and the sim so the two can never disagree about how
    long a swipe takes — a disagreement there would show up as inexplicable
    deaths on lateral moves only.
    """
    if action == WAIT:
        return p.wait_quantum_ms
    if action in (LEFT, RIGHT):
        return p.hop_duration_ms + p.swipe_extra_latency_ms
    return p.hop_duration_ms


@dataclass(frozen=True)
class State:
    row: int
    col: float
    t: float          # ms on the plan timeline; root = 0, first step adds latency
    log_id: int       # -1 when not riding
    water_run: int    # consecutive water rows entered so far
    steps: int = 0    # actions taken; `steps == 0` is what makes a state the root

    def key(self) -> Tuple[int, int, int, int, int]:
        return (
            self.row,
            int(round(self.col / COL_QUANT)),
            int(round(self.t / T_QUANT_MS)),
            self.log_id,
            self.water_run,
        )


@dataclass
class PlanResult:
    action: str
    path: List[Tuple[int, float]]     # (row, col) waypoints, for the debug overlay
    depth_reached: int                # rows gained by the chosen plan
    nodes: int
    panicking: bool
    #: Set when no action was legal at any margin on the ladder. runner.py logs
    #: these; a burst of them means the planner is deadlocking, not that the
    #: game got hard.
    boxed_in: bool = False
    #: Which safety margin actually produced this plan. Below the configured
    #: `safety_margin_ms` means the situation was tight enough to force a
    #: squeeze — worth logging, it is the leading indicator of a death.
    margin_used: float = 0.0


class Planner:
    def __init__(self, params: Params):
        self.p = params

    # -- public ------------------------------------------------------------

    def plan(
        self,
        world: World,
        row: int,
        col: float,
        log_id: int = -1,
        ms_since_forward: float = 0.0,
    ) -> PlanResult:
        """Plan one action, degrading gracefully rather than freezing.

        The first version of this returned WAIT whenever no fully-safe action
        existed. In sim that accounted for **95% of all deaths**: standing still
        in a road lane is not a neutral default, it is a decision to be hit by
        whatever is already coming. Freezing is the single worst thing a
        Crossy Road agent can do.

        So: try the configured margin, and if that boxes us in, retry with
        progressively tighter ones before accepting a squeeze. Only if even a
        zero-margin search finds nothing do we fall back to ranking bad options
        by time-to-impact.
        """
        p = self.p
        panicking = ms_since_forward >= p.eagle_timeout_ms
        base = p.eagle_panic_margin_ms if panicking else p.safety_margin_ms

        ladder = [base, base * 0.5, min(base, p.eagle_panic_margin_ms), 0.0]
        seen: List[float] = []
        result: Optional[PlanResult] = None
        for margin in ladder:
            if any(abs(margin - m) < 1e-6 for m in seen):
                continue
            seen.append(margin)
            result = self._search(world, row, col, log_id, margin, panicking)
            if not result.boxed_in:
                result.margin_used = margin
                return result

        return self._least_bad(world, row, col, log_id, panicking, result)

    # -- search ------------------------------------------------------------

    def _search(
        self,
        world: World,
        row: int,
        col: float,
        log_id: int,
        margin: float,
        panicking: bool,
    ) -> PlanResult:
        p = self.p
        # Absolute time base, matching world.t_ref_ms. Plan-relative times would
        # be subtracted against an absolute reference inside the collision math
        # and silently produce nonsense.
        root = State(row=row, col=col, t=world.t_ref_ms, log_id=log_id, water_run=0, steps=0)
        goal_row = row + p.bfs_depth

        # (cost, tiebreak, state)
        frontier: List[Tuple[float, int, State]] = [(0.0, 0, root)]
        best_cost: Dict[Tuple, float] = {root.key(): 0.0}
        parent: Dict[Tuple, Tuple[Optional[Tuple], Optional[str], State]] = {
            root.key(): (None, None, root)
        }
        counter = 1
        nodes = 0
        best_terminal: Optional[Tuple[float, State]] = None
        # Fallback ranking for when the goal is unreachable inside the node cap:
        # deepest row first, then cheapest.
        best_partial: Tuple[int, float, Optional[State]] = (-1, 0.0, None)

        while frontier and nodes < p.max_nodes:
            cost, _, s = heapq.heappop(frontier)
            if cost > best_cost.get(s.key(), float("inf")) + 1e-9:
                continue
            nodes += 1

            rank = (s.row, -cost, s)
            if rank[:2] > best_partial[:2]:
                best_partial = rank

            if s.row >= goal_row:
                if best_terminal is None or cost < best_terminal[0]:
                    best_terminal = (cost, s)
                continue

            for action in ACTIONS:
                nxt = self._step(world, s, action, margin)
                if nxt is None:
                    continue
                step_cost = self._cost(s, nxt, action, panicking) + self._dwell_risk(world, nxt)
                new_cost = cost + step_cost
                k = nxt.key()
                if new_cost < best_cost.get(k, float("inf")) - 1e-9:
                    best_cost[k] = new_cost
                    parent[k] = (s.key(), action, nxt)
                    heapq.heappush(frontier, (new_cost, counter, nxt))
                    counter += 1

        goal = best_terminal[1] if best_terminal else best_partial[2]
        if goal is None or goal.key() == root.key():
            # No legal action at this margin. Report it rather than papering over
            # it with a WAIT — plan() decides what to do about it.
            return PlanResult(WAIT, [(row, col)], 0, nodes, panicking, boxed_in=True)

        first, path = self._unwind(parent, goal.key(), root.key())
        return PlanResult(
            action=first or WAIT,
            path=path,
            depth_reached=goal.row - row,
            nodes=nodes,
            panicking=panicking,
        )

    # -- last resort -------------------------------------------------------

    def _least_bad(
        self,
        world: World,
        row: int,
        col: float,
        log_id: int,
        panicking: bool,
        last: Optional[PlanResult],
    ) -> PlanResult:
        """Every action collides. Pick the one that buys the most time.

        Ranked by time-to-impact at the landing cell, with a small bonus for
        forward progress so ties break towards the exit rather than towards
        loitering. Water cells with no carrier score zero — falling in is
        instant, whereas a car still has to arrive.
        """
        p = self.p
        best_action, best_score = WAIT, -1.0

        for action in ACTIONS:
            dt = action_duration(p, action) + p.latency_offset_ms
            t_arr = world.t_ref_ms + dt

            c = col
            if log_id >= 0:
                log = world.obstacle(log_id)
                if log is not None:
                    c = col + log.vx * (dt / 1000.0)
            if action == LEFT:
                c -= 1.0
            elif action == RIGHT:
                c += 1.0
            target_row = row + 1 if action == FORWARD else row

            if not world.in_bounds(c):
                continue

            lane = world.lane(target_row)
            if lane.type is LaneType.WATER:
                carrier = best_carrier(
                    lane, c, p.chicken_width, t_arr, t_arr, 0.5, world.t_ref_ms
                )
                score = 0.0 if carrier is None else 1e6
            elif lane.type is LaneType.UNKNOWN:
                score = 0.0
            else:
                score = time_to_hazard(lane, c, p.chicken_width, t_arr, world.t_ref_ms)
                score = min(score, 1e6)

            if action == FORWARD:
                score += 1.0        # tie-break towards progress
            if score > best_score:
                best_action, best_score = action, score

        return PlanResult(
            action=best_action,
            path=[(row, col)],
            depth_reached=0,
            nodes=last.nodes if last else 0,
            panicking=panicking,
            boxed_in=True,
            margin_used=0.0,
        )

    # -- transition --------------------------------------------------------

    def action_duration(self, action: str) -> float:
        return action_duration(self.p, action)

    def _step(self, world: World, s: State, action: str, margin: float) -> Optional[State]:
        p = self.p
        dt = self.action_duration(action)
        if s.steps == 0:
            dt += p.latency_offset_ms      # INVARIANT 1, paid once at the head
        t_arr = s.t + dt

        # Drift first: while riding, the world moves you during the action.
        col = s.col
        if s.log_id >= 0:
            log = world.obstacle(s.log_id)
            if log is None:
                return None
            col = s.col + log.vx * (dt / 1000.0)
            if not world.in_bounds(col):
                return None          # carried offscreen mid-action

        if action == FORWARD:
            target_row, target_col = s.row + 1, col
        elif action == LEFT:
            target_row, target_col = s.row, col - 1.0
        elif action == RIGHT:
            target_row, target_col = s.row, col + 1.0
        else:
            target_row, target_col = s.row, col

        if not world.in_bounds(target_col):
            return None

        lane = world.lane(target_row)
        if lane.type is LaneType.UNKNOWN:
            # Refuse to gamble on a lane perception could not classify. The eagle
            # timer will force the issue if this persists.
            return None

        t0, t1 = t_arr - margin, t_arr + margin

        if lane.type is LaneType.WATER:
            # Leaving water for water, or entering it: same rules either way.
            landing = target_col
            carrier = best_carrier(
                lane, landing, p.chicken_width, t0, t1,
                p.log_entry_min_overlap, world.t_ref_ms,
            )
            if carrier is None:
                return None
            water_run = s.water_run + 1 if target_row != s.row else s.water_run
            if water_run > p.water_max_consecutive:
                return None
            if not self._has_exit(world, target_row, landing, t_arr, carrier, margin):
                return None          # INVARIANT 3
            return State(target_row, landing, t_arr, carrier.oid, water_run, s.steps + 1)

        # Solid ground. Snap off the log's fractional drift onto the grid.
        landing = round(target_col) if s.log_id >= 0 else target_col
        if not world.in_bounds(landing):
            return None
        if blocking_obstacle(lane, landing, p.chicken_width, t0, t1, world.t_ref_ms):
            return None
        return State(target_row, float(landing), t_arr, -1, 0, s.steps + 1)

    def _dwell_risk(self, world: World, s: State) -> float:
        """How exposed is it to be standing where `s` puts us?

        A cell that is clear *right now* but has a truck arriving in 150ms is not
        a safe cell — landing there is how the planner talks itself into a
        position where every subsequent action is already lost. Measured in hops
        of clearance, so it scales with `hop_duration_ms` rather than needing its
        own time constant.

        Grass has no traffic and scores zero, which is why the chicken learns to
        rest on grass and sprint across roads — the behaviour you want, arrived
        at by cost rather than by a hand-written rule.
        """
        p = self.p
        lane = world.lane(s.row)
        if lane.type in (LaneType.GRASS, LaneType.WATER, LaneType.UNKNOWN):
            return 0.0
        ttl = time_to_hazard(lane, s.col, p.chicken_width, s.t, world.t_ref_ms)
        want = 2.0 * p.hop_duration_ms
        if ttl >= want:
            return 0.0
        return p.dwell_cost * (1.0 - ttl / want)

    # -- invariant 3 -------------------------------------------------------

    def _has_exit(
        self,
        world: World,
        water_row: int,
        col: float,
        t_arr: float,
        log: Obstacle,
        margin: float,
    ) -> bool:
        """Is there a reachable way off this log within `log_exit_lead_ms`?

        Deliberately shallow: one row forward, checked at each hop opportunity
        across the lead window, aborting the moment the log would carry us out
        of bounds. Deep recursion here would double-count the main search's job
        and cost more than it buys — the main search still has to find the real
        exit, this only refuses the entry when *no* exit exists.
        """
        p = self.p
        next_lane = world.lane(water_row + 1)
        if next_lane.type is LaneType.UNKNOWN:
            return False

        lead = 0.0
        while lead <= p.log_exit_lead_ms + 1e-9:
            drift_col = col + log.vx * (lead / 1000.0)
            if not world.in_bounds(drift_col):
                return False         # offscreen before any exit opens up
            t_exit = t_arr + lead + p.hop_duration_ms
            t0, t1 = t_exit - margin, t_exit + margin
            landing = drift_col if next_lane.type is LaneType.WATER else round(drift_col)
            if world.in_bounds(landing):
                if next_lane.type is LaneType.WATER:
                    if best_carrier(
                        next_lane, landing, p.chicken_width, t0, t1,
                        p.log_entry_min_overlap, world.t_ref_ms,
                    ):
                        return True
                elif not blocking_obstacle(
                    next_lane, landing, p.chicken_width, t0, t1, world.t_ref_ms
                ):
                    return True
            lead += p.hop_duration_ms
        return False

    # -- cost --------------------------------------------------------------

    def _cost(self, s: State, nxt: State, action: str, panicking: bool) -> float:
        p = self.p
        if action == FORWARD:
            c = 1.0
            if panicking:
                c /= p.panic_forward_bonus
        elif action == WAIT:
            c = p.wait_quantum_ms / p.hop_duration_ms
            if panicking:
                c *= p.panic_forward_bonus
        else:
            c = p.lateral_move_cost
            if panicking:
                c *= p.panic_forward_bonus

        centre = 0.0
        c += p.column_center_bias * (abs(nxt.col - centre) - abs(s.col - centre))
        return max(c, 1e-3)          # keep Dijkstra's non-negativity guarantee

    # -- path --------------------------------------------------------------

    @staticmethod
    def _unwind(parent, goal_key, root_key) -> Tuple[Optional[str], List[Tuple[int, float]]]:
        chain: List[Tuple[Optional[str], State]] = []
        k = goal_key
        seen = set()
        while k is not None and k not in seen:
            seen.add(k)
            pk, action, st = parent[k]
            chain.append((action, st))
            if k == root_key:
                break
            k = pk
        chain.reverse()
        path = [(st.row, st.col) for _, st in chain]
        first = next((a for a, _ in chain if a is not None), None)
        return first, path
