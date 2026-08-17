"""Loads calib.yaml and params.yaml into frozen dataclasses.

Frozen because a planner parameter mutating mid-run is the kind of bug that
shows up as "p̂ got worse overnight and nothing changed".
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml

ROOT = Path(__file__).resolve().parent
CALIB_PATH = ROOT / "calib.yaml"
PARAMS_PATH = ROOT / "params.yaml"


@dataclass(frozen=True)
class Params:
    # searched
    safety_margin_ms: float = 120.0
    latency_offset_ms: float = 200.0
    #: Lateral moves are SWIPES (docs/2026-reality-check.md §1) and
    #: press(forDuration:thenDragTo:) costs more than tap(). Folding both into
    #: one latency constant systematically mistimes one of them.
    swipe_extra_latency_ms: float = 60.0
    log_entry_min_overlap: float = 0.55
    log_exit_lead_ms: float = 400.0
    water_max_consecutive: int = 4
    eagle_timeout_ms: float = 2200.0
    eagle_panic_margin_ms: float = 60.0
    bfs_depth: int = 3
    replan_interval_frames: int = 2
    column_center_bias: float = 0.3
    lateral_move_cost: float = 1.2
    #: Penalty for landing on a lethal lane without clearance ahead of you.
    dwell_cost: float = 2.0
    # fixed
    hop_duration_ms: float = 130.0
    wait_quantum_ms: float = 60.0
    chicken_width: float = 0.80
    max_nodes: int = 1500
    plan_horizon_ms: float = 1500.0
    eagle_death_ms: float = 3600.0
    panic_forward_bonus: float = 4.0

    @property
    def hash(self) -> str:
        """Short stable digest — goes in deaths.csv so a row is traceable to its params."""
        blob = json.dumps(asdict(self), sort_keys=True).encode()
        return hashlib.sha1(blob).hexdigest()[:10]

    def evolve(self, **kw: Any) -> "Params":
        return replace(self, **kw)


#: name -> (lo, hi). Populated from params.yaml at load; this is the fallback.
DEFAULT_SEARCH_SPACE: Dict[str, Tuple[float, float]] = {
    "safety_margin_ms": (40, 400),
    "latency_offset_ms": (80, 320),
    "swipe_extra_latency_ms": (0, 250),
    "log_entry_min_overlap": (0.35, 0.85),
    "log_exit_lead_ms": (150, 900),
    "water_max_consecutive": (2, 8),
    "eagle_timeout_ms": (1200, 3200),
    "eagle_panic_margin_ms": (10, 150),
    "bfs_depth": (2, 5),
    "replan_interval_frames": (1, 6),
    "column_center_bias": (0.0, 1.5),
    "lateral_move_cost": (0.5, 3.0),
    "dwell_cost": (0.0, 8.0),
}

INTEGER_PARAMS = ("water_max_consecutive", "bfs_depth", "replan_interval_frames")


@dataclass(frozen=True)
class SearchSpace:
    bounds: Dict[str, Tuple[float, float]] = field(default_factory=lambda: dict(DEFAULT_SEARCH_SPACE))
    integers: Tuple[str, ...] = INTEGER_PARAMS

    @property
    def names(self) -> List[str]:
        return sorted(self.bounds)

    def clip(self, name: str, value: float) -> float:
        lo, hi = self.bounds[name]
        v = min(hi, max(lo, value))
        return int(round(v)) if name in self.integers else float(v)

    def clip_all(self, values: Dict[str, float]) -> Dict[str, float]:
        return {k: self.clip(k, v) for k, v in values.items() if k in self.bounds}

    def to_vector(self, p: Params) -> List[float]:
        return [float(getattr(p, n)) for n in self.names]

    def from_vector(self, vec: List[float], base: Params) -> Params:
        return base.evolve(**self.clip_all(dict(zip(self.names, vec))))


def load_params(path: Path | str = PARAMS_PATH) -> Tuple[Params, SearchSpace]:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    fixed = raw.pop("fixed", {}) or {}
    space_raw = raw.pop("search_space", {}) or {}
    ints = tuple(raw.pop("integer_params", INTEGER_PARAMS) or INTEGER_PARAMS)

    known = {f for f in Params.__dataclass_fields__}
    kwargs = {k: v for k, v in {**raw, **fixed}.items() if k in known}
    unknown = ({*raw, *fixed} - known)
    if unknown:
        raise ValueError(f"{path}: unknown parameter(s) {sorted(unknown)}")

    for name in ints:
        if name in kwargs:
            kwargs[name] = int(round(float(kwargs[name])))
    params = Params(**kwargs)

    space = SearchSpace(
        bounds={k: (float(v[0]), float(v[1])) for k, v in space_raw.items()} or dict(DEFAULT_SEARCH_SPACE),
        integers=ints,
    )
    return params, space


@dataclass(frozen=True)
class Calib:
    measured: bool
    capture: Dict[str, Any]
    geometry: Dict[str, Any]
    lanes: Dict[str, Dict[str, List[int]]]
    obstacles: Dict[str, Any]

    # --- pixel <-> world, the only place this conversion may happen ---------
    def row_to_screen_y(self, row: int, chicken_row: int = 0) -> float:
        g = self.geometry
        return g["row0_screen_y"] - (row - chicken_row) * g["px_per_row"]

    def screen_y_to_row(self, y: float, chicken_row: int = 0) -> int:
        g = self.geometry
        return int(round((g["row0_screen_y"] - y) / g["px_per_row"])) + chicken_row

    def screen_x_to_col(self, x: float) -> float:
        g = self.geometry
        return (x - g["chicken_screen_xy"][0]) / g["px_per_col"]

    def col_to_screen_x(self, col: float) -> float:
        g = self.geometry
        return g["chicken_screen_xy"][0] + col * g["px_per_col"]

    def px_to_cols(self, px: float) -> float:
        return px / self.geometry["px_per_col"]


def load_calib(path: Path | str = CALIB_PATH) -> Calib:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    return Calib(
        measured=bool(raw.get("measured", False)),
        capture=raw.get("capture", {}),
        geometry=raw.get("geometry", {}),
        lanes=raw.get("lanes", {}),
        obstacles=raw.get("obstacles", {}),
    )


THRESHOLD = 328  # HARD. See CLAUDE.md and runbook A.1. Never raise this.
MAX_RUNS = 500
LOCKFILE = Path.home() / ".crossy_bot_done"
HEARTBEAT = ROOT / "run" / "heartbeat"
LOGS = ROOT / "logs"
DEBUG_DIR = ROOT / "debug"
