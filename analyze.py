"""p̂ overall and per lane, score histogram, fps trend.

Morning triage, three questions in order (runbook §10.5):
  1. Did it hit 328?
  2. If not, what was the measured p̂ versus what you tuned to?
  3. Did it actually run all night, or stall at 1am and spend seven hours
     tapping a menu?

Question 3 is the one people forget, and "hundreds of runs logged with score 0"
is its signature.
"""

from __future__ import annotations

import argparse
import csv
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from config import LOGS, THRESHOLD

DEATHS_CSV = LOGS / "deaths.csv"
RUNS_CSV = LOGS / "runs.csv"

DEATH_FIELDS = [
    "run_id", "final_score", "death_row", "lane_type", "cause",
    "params_hash", "timestamp",
]
RUN_FIELDS = [
    "run_id", "final_score", "params_hash", "timestamp", "duration_s", "mean_fps",
    "rows_grass", "rows_road", "rows_water", "rows_track",
    "boxed_ticks", "squeeze_ticks", "ads_seen", "unknown_screens",
]


def _read(path: Path) -> List[dict]:
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def _f(row: dict, key: str, default: float = 0.0) -> float:
    try:
        return float(row.get(key) or default)
    except (TypeError, ValueError):
        return default


@dataclass
class LaneStat:
    lane: str
    deaths: int
    rows: int

    @property
    def p(self) -> float:
        return self.deaths / self.rows if self.rows else 0.0

    @property
    def rel_se(self) -> float:
        return 1.0 / math.sqrt(self.deaths) if self.deaths else float("inf")

    def __str__(self) -> str:
        if not self.rows:
            return f"  {self.lane:<7} no data"
        se = "  n/a" if self.deaths == 0 else f"±{self.rel_se*100:3.0f}%"
        return (f"  {self.lane:<7} p̂={self.p*100:6.2f}% {se}"
                f"   {self.deaths:4d} deaths / {self.rows:6d} rows")


@dataclass
class Report:
    deaths: List[dict]
    runs: List[dict]

    @property
    def total_deaths(self) -> int:
        return len(self.deaths)

    @property
    def total_rows(self) -> int:
        return sum(
            int(_f(r, f"rows_{lane}"))
            for r in self.runs
            for lane in ("grass", "road", "water", "track")
        )

    @property
    def p_hat(self) -> float:
        return self.total_deaths / self.total_rows if self.total_rows else 0.0

    @property
    def rel_se(self) -> float:
        return 1.0 / math.sqrt(self.total_deaths) if self.total_deaths else float("inf")

    def p_reach(self, target: int = 330) -> float:
        return (1.0 - self.p_hat) ** target

    def by_lane(self) -> List[LaneStat]:
        deaths = Counter(d["lane_type"] for d in self.deaths)
        out = []
        for lane in ("water", "road", "track", "grass"):
            rows = sum(int(_f(r, f"rows_{lane}")) for r in self.runs)
            out.append(LaneStat(lane, deaths.get(lane, 0), rows))
        return out

    def by_cause(self) -> Counter:
        return Counter(d["cause"] for d in self.deaths)

    def scores(self) -> List[int]:
        return [int(_f(r, "final_score")) for r in self.runs]

    def fps_trend(self) -> List[Tuple[str, float]]:
        return [(r.get("timestamp", "?"), _f(r, "mean_fps")) for r in self.runs]


def histogram(values: List[int], bins: int = 10, width: int = 42) -> str:
    if not values:
        return "  (no runs)"
    lo, hi = min(values), max(values)
    if hi == lo:
        return f"  all {len(values)} runs scored {lo}"
    step = (hi - lo) / bins
    counts = [0] * bins
    for v in values:
        counts[min(bins - 1, int((v - lo) / step))] += 1
    peak = max(counts) or 1
    lines = []
    for i, c in enumerate(counts):
        a, b = lo + i * step, lo + (i + 1) * step
        lines.append(f"  {a:6.0f}-{b:6.0f} {'#' * int(width * c / peak):<{width}} {c}")
    return "\n".join(lines)


def stall_check(report: Report) -> List[str]:
    """Detect the failure mode that costs a whole night for zero information."""
    warnings = []
    scores = report.scores()
    if not scores:
        return ["no runs logged at all — did the harness ever start?"]

    zeros = sum(1 for s in scores if s == 0)
    if zeros > 0.3 * len(scores):
        warnings.append(
            f"{zeros}/{len(scores)} runs scored 0 — almost certainly stuck on an "
            f"unhandled menu state. Check debug/unknown/."
        )

    unknown = sum(int(_f(r, "unknown_screens")) for r in report.runs)
    if unknown:
        warnings.append(f"{unknown} unknown-screen events — check debug/unknown/")

    fps = [f for _, f in report.fps_trend() if f > 0]
    if len(fps) >= 8:
        head = sum(fps[: len(fps) // 4]) / max(1, len(fps) // 4)
        tail = sum(fps[-len(fps) // 4 :]) / max(1, len(fps) // 4)
        if tail < head * 0.85:
            warnings.append(
                f"fps sagged {head:.0f} -> {tail:.0f} across the session — thermal "
                f"throttle. Duty-cycle the grind or add cooling; p degrades with it."
            )

    boxed = sum(int(_f(r, "boxed_ticks")) for r in report.runs)
    if boxed > 0.05 * sum(int(_f(r, "final_score")) or 1 for r in report.runs):
        warnings.append(
            f"{boxed} boxed-in ticks — the planner is being forced into "
            f"least-bad choices often. Leading indicator of deaths."
        )
    return warnings


def main() -> None:
    ap = argparse.ArgumentParser(description="Session analysis")
    ap.add_argument("deaths", nargs="?", default=str(DEATHS_CSV))
    ap.add_argument("--runs", default=str(RUNS_CSV))
    args = ap.parse_args()

    report = Report(_read(Path(args.deaths)), _read(Path(args.runs)))

    print("=" * 66)
    best = max(report.scores(), default=0)
    hit = best >= THRESHOLD
    print(f"1. Did it hit {THRESHOLD}?   {'YES — ' + str(best) if hit else f'no (best {best})'}")

    print(f"\n2. Measured p̂")
    if report.total_deaths:
        print(f"  overall p̂={report.p_hat*100:.2f}% ±{report.rel_se*100:.0f}% "
              f"({report.total_deaths} deaths / {report.total_rows} rows)")
        print(f"  P(reach 330) = {report.p_reach()*100:.2f}%  "
              f"-> ~{1/report.p_reach():.0f} runs expected" if report.p_hat < 1 else "")
        # Relative SE ~ 1/sqrt(deaths): 20 deaths is ±22% and screening only.
        if report.total_deaths < 100:
            print(f"  NOTE: {report.total_deaths} deaths is ±{report.rel_se*100:.0f}% — "
                  f"cannot distinguish 1.0% from 1.3%. Need ~100 for that, ~400 for ±5%.")
        for stat in report.by_lane():
            print(stat)
        print("\n  causes: " + ", ".join(f"{c}={n}" for c, n in report.by_cause().most_common()))
    else:
        print("  no deaths logged")

    print(f"\n3. Did it run all night?")
    warnings = stall_check(report)
    if warnings:
        for w in warnings:
            print(f"  ! {w}")
    else:
        print(f"  {len(report.runs)} runs, nothing anomalous")

    print(f"\nscore histogram ({len(report.scores())} runs)")
    print(histogram(report.scores()))
    print("=" * 66)


if __name__ == "__main__":
    main()
