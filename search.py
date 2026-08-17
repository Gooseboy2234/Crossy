"""CEM in sim, successive halving on device.

Two facts drive the whole design (runbook §9):

  * Optimise **p̂, not mean score.** Run length is roughly geometric, so score
    variance is enormous and one episode gives you one sample. p̂ gives ~100
    row-crossing events per episode.

  * **Decompose per lane type.** Tuning water parameters against global p̂
    dilutes the signal with unrelated deaths. Water params are scored against
    p̂_water alone.

Device evaluation is the scarce resource: ~10 minutes per candidate. Do not give
every candidate a full budget — screen cheaply, escalate survivors.

BEFORE TRUSTING ANY OF THIS: validate the sim. Run your current on-device
parameters through it and compare predicted p̂ against measured on-device p̂.
Within ~30% relative is good enough to optimise against. If the sim says 0.4% and
the phone says 2%, the noise model is too gentle — fix that first or you are
optimising a fantasy.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import random
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from config import Params, SearchSpace, load_params
from sim import Biome, Noise, PHat, estimate_p_hat

log = logging.getLogger("search")

#: Which parameters are scored against which sub-metric. This is the efficiency
#: unlock: you reach a tight water estimate in a fraction of the episodes because
#: the signal is not diluted by road deaths.
GROUPS: Dict[str, Tuple[str, ...]] = {
    "water": ("log_entry_min_overlap", "log_exit_lead_ms", "water_max_consecutive"),
    "timing": ("safety_margin_ms", "latency_offset_ms", "swipe_extra_latency_ms", "dwell_cost"),
    "eagle": ("eagle_timeout_ms", "eagle_panic_margin_ms"),
    "search": ("bfs_depth", "replan_interval_frames", "column_center_bias", "lateral_move_cost"),
}

#: Sub-metric each group is judged on. `None` means overall p̂.
GROUP_METRIC = {"water": "water", "timing": "road", "eagle": None, "search": None}


def objective(ph: PHat, lane: Optional[str]) -> float:
    """Lower is better. Returns the relevant p̂."""
    if lane is None:
        return ph.p
    p, deaths = ph.lane_p(lane)
    if deaths < 3:
        # Too few events in this lane to say anything; fall back to overall so a
        # candidate that simply never reaches water cannot win by default.
        return ph.p
    return p


# --------------------------------------------------------------------------
# Cross-Entropy Method
# --------------------------------------------------------------------------


@dataclass
class CEMResult:
    best: Params
    best_score: float
    history: List[float]


def cem(
    base: Params,
    space: SearchSpace,
    names: Sequence[str],
    lane: Optional[str] = None,
    generations: int = 8,
    population: int = 24,
    elite_frac: float = 0.15,
    deaths_per_eval: int = 120,
    seed: int = 0,
    biome: Optional[Biome] = None,
    noise: Optional[Noise] = None,
) -> CEMResult:
    """Cross-entropy method over `names` only, leaving other params at `base`."""
    rng = random.Random(seed)
    names = list(names)
    mu = [float(getattr(base, n)) for n in names]
    sigma = [max(1e-6, 0.25 * (space.bounds[n][1] - space.bounds[n][0])) for n in names]

    n_elite = max(2, int(population * elite_frac))
    best, best_score = base, float("inf")
    history: List[float] = []

    for gen in range(generations):
        cands: List[Params] = []
        for _ in range(population):
            vals = {n: space.clip(n, rng.gauss(m, s)) for n, m, s in zip(names, mu, sigma)}
            cands.append(base.evolve(**vals))

        scored: List[Tuple[float, Params]] = []
        for i, c in enumerate(cands):
            ph = estimate_p_hat(
                c, deaths_target=deaths_per_eval, seed=seed * 977 + gen * 31 + i,
                biome=biome, noise=noise,
            )
            scored.append((objective(ph, lane), c))

        scored.sort(key=lambda t: t[0])
        elite = [c for _, c in scored[:n_elite]]
        if scored[0][0] < best_score:
            best_score, best = scored[0]

        for j, n in enumerate(names):
            vals = [float(getattr(c, n)) for c in elite]
            mu[j] = statistics.fmean(vals)
            # Floor the spread: without it the population collapses after two or
            # three generations and the rest of the budget explores nothing.
            span = space.bounds[n][1] - space.bounds[n][0]
            sigma[j] = max(statistics.pstdev(vals) if len(vals) > 1 else 0.0, 0.02 * span)

        history.append(scored[0][0])
        log.info("gen %d/%d best=%.4f%% mu=%s", gen + 1, generations, scored[0][0] * 100,
                 {n: round(m, 3) for n, m in zip(names, mu)})

    return CEMResult(best=best, best_score=best_score, history=history)


def tune_all_groups(
    base: Params, space: SearchSpace, seed: int = 0, **kw
) -> Params:
    """Tune group by group, each against its own sub-metric.

    Water first: it is where nearly all of p lives, so everything downstream is
    measured against a bird that can actually cross a river.
    """
    current = base
    for group in ("water", "timing", "search", "eagle"):
        names = [n for n in GROUPS[group] if n in space.bounds]
        if not names:
            continue
        lane = GROUP_METRIC[group]
        log.info("=== tuning %s against p̂_%s ===", group, lane or "overall")
        res = cem(current, space, names, lane=lane, seed=seed, **kw)
        current = res.best
        log.info("  -> %s", {n: getattr(current, n) for n in names})
    return current


# --------------------------------------------------------------------------
# Successive halving
# --------------------------------------------------------------------------


def successive_halving(
    candidates: List[Params],
    evaluate: Callable[[Params, int], PHat],
    rounds: Sequence[int] = (15, 40, 150),
    lane: Optional[str] = None,
    keep_frac: float = 0.5,
) -> List[Tuple[float, Params, PHat]]:
    """Eliminate weak candidates on small budgets, escalate the survivors.

    `evaluate(params, deaths_target) -> PHat`. Pass a sim-backed callable to
    rehearse; pass a device-backed one for the real thing, where each evaluation
    costs ~10 minutes and the whole ladder runs mostly unattended overnight.

    Compare against your hand-tuned baseline throughout. If none of the sim-tuned
    candidates beat it on device, the noise model is wrong — go back and fix the
    sim rather than shipping the winner of a bad race.
    """
    alive = list(candidates)
    results: List[Tuple[float, Params, PHat]] = []

    for rnd, budget in enumerate(rounds, 1):
        scored: List[Tuple[float, Params, PHat]] = []
        for i, c in enumerate(alive):
            ph = evaluate(c, budget)
            scored.append((objective(ph, lane), c, ph))
            log.info("round %d cand %d/%d: %s", rnd, i + 1, len(alive), ph)
        scored.sort(key=lambda t: t[0])
        results = scored
        if rnd == len(rounds):
            break
        keep = max(1, int(len(scored) * keep_frac))
        alive = [c for _, c, _ in scored[:keep]]
        log.info("round %d: %d -> %d survivors", rnd, len(scored), len(alive))

    return results


def sim_evaluator(seed: int = 0, biome=None, noise=None) -> Callable[[Params, int], PHat]:
    counter = {"n": 0}

    def _eval(p: Params, deaths: int) -> PHat:
        counter["n"] += 1
        return estimate_p_hat(p, deaths_target=deaths, seed=seed + counter["n"],
                              biome=biome, noise=noise)

    return _eval


# --------------------------------------------------------------------------
# Sim validation gate
# --------------------------------------------------------------------------


def validate_sim(params: Params, device_p: float, device_deaths: int,
                 deaths_target: int = 300, seed: int = 0) -> bool:
    """Does the sim agree with the device within ~30% relative?

    This is the gate on the entire search. A sim that is too clean will tune you
    to razor-thin margins that shatter on real perception noise, and it will do
    so while reporting excellent numbers.
    """
    ph = estimate_p_hat(params, deaths_target=deaths_target, seed=seed)
    rel = abs(ph.p - device_p) / device_p if device_p else float("inf")
    print(f"  sim    p̂ = {ph.p*100:.2f}% ±{ph.rel_se*100:.0f}% ({ph.deaths} deaths)")
    print(f"  device p̂ = {device_p*100:.2f}% ±{100/max(device_deaths,1)**0.5:.0f}% "
          f"({device_deaths} deaths)")
    print(f"  relative difference: {rel*100:.0f}%")
    if device_deaths < 100:
        print("  WARNING: fewer than 100 device deaths — this comparison is very noisy.")
    if rel <= 0.30:
        print("  PASS — close enough to optimise against.")
        return True
    hint = "too gentle" if ph.p < device_p else "too harsh"
    print(f"  FAIL — the noise model is {hint}. Fix sim.Noise before trusting any search.")
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Parameter search")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("cem", help="tune in sim")
    c.add_parser_group = None
    c.add_argument("--group", choices=[*GROUPS, "all"], default="water")
    c.add_argument("--generations", type=int, default=8)
    c.add_argument("--population", type=int, default=24)
    c.add_argument("--deaths", type=int, default=120)
    c.add_argument("--seed", type=int, default=0)
    c.add_argument("--out", default="params.tuned.json")

    h = sub.add_parser("halve", help="rehearse successive halving in sim")
    h.add_argument("--candidates", default="params.tuned.json")
    h.add_argument("--seed", type=int, default=0)

    v = sub.add_parser("validate", help="compare sim p-hat against device p-hat")
    v.add_argument("--device-p", type=float, required=True, help="e.g. 0.018 for 1.8%%")
    v.add_argument("--device-deaths", type=int, required=True)

    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    base, space = load_params()

    if args.cmd == "validate":
        return 0 if validate_sim(base, args.device_p, args.device_deaths) else 1

    if args.cmd == "cem":
        if args.group == "all":
            best = tune_all_groups(
                base, space, seed=args.seed, generations=args.generations,
                population=args.population, deaths_per_eval=args.deaths,
            )
        else:
            res = cem(base, space, GROUPS[args.group], lane=GROUP_METRIC[args.group],
                      generations=args.generations, population=args.population,
                      deaths_per_eval=args.deaths, seed=args.seed)
            best = res.best
        Path(args.out).write_text(json.dumps(asdict(best), indent=2))
        print(f"\nwrote {args.out}")
        ph = estimate_p_hat(best, deaths_target=300, seed=args.seed + 1)
        print(f"tuned: {ph}")
        print("\nThis is a SIM result. Validate against device p̂ before believing it:")
        print("  python search.py validate --device-p <measured> --device-deaths <n>")
        return 0

    if args.cmd == "halve":
        cands = [base]
        p = Path(args.candidates)
        if p.exists():
            cands.append(Params(**json.loads(p.read_text())))
        results = successive_halving(cands, sim_evaluator(seed=args.seed))
        for score, _, ph in results:
            print(f"  {score*100:.2f}%  {ph}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
