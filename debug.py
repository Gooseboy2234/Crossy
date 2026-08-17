"""Annotated frame dumps, contact sheets, retention cap.

"The bot died in water at row 84" is nearly useless. An annotated frame showing
the log was classified as water because its highlight fell outside the saturation
threshold is immediately actionable. This file is the difference between a death
being diagnosable and merely countable.

When a death is unexplained: dump the ring buffer first, theorize second.
"""

from __future__ import annotations

import argparse
import collections
import json
import logging
import shutil
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

import cv2
import numpy as np

from config import DEBUG_DIR, load_calib
from world import LaneType

log = logging.getLogger("debug")

LANE_COLOR = {
    "grass": (0, 200, 0),
    "road": (60, 60, 60),
    "water": (220, 120, 0),
    "track": (0, 90, 180),
    "unknown": (0, 0, 255),
}

#: 20 annotated PNGs per death across ~300 deaths fills a drive overnight.
#: Cap retention as you go — this is the item that bites people (runbook §10.3).
KEEP_PER_LANE = 6
RING_LEN = 20


@dataclass
class DebugState:
    """Everything needed to explain one tick, later, without the process alive."""

    t_ms: float
    score: int
    chicken_row: int
    chicken_col: float
    action: str
    fps: float
    latency_ms: float
    margin_used: float
    boxed_in: bool
    panicking: bool
    screen: str
    lanes: Dict[int, str]
    obstacles: List[Dict[str, float]]
    plan: List[Tuple[int, float]]


class RingBuffer:
    def __init__(self, maxlen: int = RING_LEN):
        self.buf: Deque[Tuple[np.ndarray, DebugState]] = collections.deque(maxlen=maxlen)

    def record(self, frame: np.ndarray, state: DebugState) -> None:
        self.buf.append((frame.copy(), state))

    def clear(self) -> None:
        self.buf.clear()

    def __len__(self) -> int:
        return len(self.buf)


def annotate(frame: np.ndarray, st: DebugState, calib) -> np.ndarray:
    """Draw lanes, obstacle boxes, velocity arrows and the plan onto a frame."""
    img = frame.copy()
    g = calib.geometry
    base_row = st.chicken_row

    def y_of(row: int) -> int:
        return int(g["row0_screen_y"] - (row - base_row) * g["px_per_row"])

    for row, lane in st.lanes.items():
        y = y_of(int(row))
        if not (0 <= y < img.shape[0]):
            continue
        c = LANE_COLOR.get(lane, (0, 0, 255))
        cv2.line(img, (0, y), (img.shape[1], y), c, 1)
        cv2.putText(img, f"{row}:{lane}", (4, y - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.4, c, 1)

    for ob in st.obstacles:
        x = int(calib.col_to_screen_x(ob["x"]))
        y = y_of(int(ob["row"]))
        w = int(ob["width"] * g["px_per_col"])
        cv2.rectangle(img, (x - w // 2, y - 24), (x + w // 2, y + 24), (0, 255, 255), 2)
        # Velocity vector, scaled to 500ms of travel. A wrong-length or
        # wrong-direction arrow means 2-frame differencing snuck back in.
        vx = int(ob["vx"] * g["px_per_col"] * 0.5)
        cv2.arrowedLine(img, (x, y), (x + vx, y), (0, 255, 255), 2, tipLength=0.3)

    pts = [(int(calib.col_to_screen_x(c)), y_of(int(r))) for r, c in st.plan]
    for a, b in zip(pts, pts[1:]):
        cv2.line(img, a, b, (255, 0, 255), 3)

    chick = (int(calib.col_to_screen_x(st.chicken_col)), y_of(st.chicken_row))
    cv2.circle(img, chick, 14, (255, 255, 255), 2)

    hud = (f"score={st.score} act={st.action} fps={st.fps:.1f} "
           f"lat={st.latency_ms:.0f}ms margin={st.margin_used:.0f}")
    cv2.putText(img, hud, (10, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
    flags = []
    if st.boxed_in:
        flags.append("BOXED")
    if st.panicking:
        flags.append("EAGLE-PANIC")
    if flags:
        cv2.putText(img, " ".join(flags), (10, 76),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
    return img


def dump_death(
    ring: RingBuffer,
    run_id: str,
    cause: str,
    lane_type: str,
    calib,
    outdir: Path = DEBUG_DIR,
) -> Path:
    d = outdir / f"{run_id}_{lane_type}_{cause}"
    d.mkdir(parents=True, exist_ok=True)
    for i, (frame, st) in enumerate(ring.buf):
        cv2.imwrite(str(d / f"{i:02d}.png"), annotate(frame, st, calib))
        (d / f"{i:02d}.json").write_text(json.dumps(asdict(st), default=str))
    prune(lane_type, outdir)
    return d


def prune(lane_type: str, outdir: Path = DEBUG_DIR, keep: int = KEEP_PER_LANE) -> int:
    """Keep only the newest `keep` dumps for this lane type. Delete as you go."""
    dirs = sorted(
        (p for p in outdir.glob(f"*_{lane_type}_*") if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
    )
    removed = 0
    for p in dirs[:-keep] if len(dirs) > keep else []:
        shutil.rmtree(p, ignore_errors=True)
        removed += 1
    return removed


def dump_unknown(frame: np.ndarray, detail: str, outdir: Path = DEBUG_DIR) -> Path:
    """Log every unknown state with a frame dump.

    Your first night will surface two or three screens you did not know existed,
    and these dumps are how you add them to the state machine.
    """
    d = outdir / "unknown"
    d.mkdir(parents=True, exist_ok=True)
    existing = sorted(d.glob("*.png"))
    for p in existing[:-40] if len(existing) > 40 else []:
        p.unlink(missing_ok=True)
    stamp = f"{len(existing):04d}"
    cv2.imwrite(str(d / f"{stamp}.png"), frame)
    (d / f"{stamp}.txt").write_text(detail)
    return d / f"{stamp}.png"


def contact_sheet(
    lane_type: str, outdir: Path = DEBUG_DIR, cols: int = 3, tile: Tuple[int, int] = (420, 900)
) -> Optional[Path]:
    """Tiled final frames of the six most recent deaths in one lane type.

    The single highest-value artifact: patterns jump out instantly that no CSV
    will reveal.

    Reading a water sheet:
      chicken short of the log      -> entry threshold or landing prediction off
      on the log, drifted offscreen -> exit planning, or log_exit_lead_ms too small
      boxes flickering between frames -> blob tracking gate radius too tight
      velocity arrows wrong         -> 2-frame differencing snuck back in
    """
    dirs = sorted(
        (p for p in outdir.glob(f"*_{lane_type}_*") if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
    )[-6:]
    if not dirs:
        return None

    tiles = []
    for d in dirs:
        pngs = sorted(d.glob("*.png"))
        if not pngs:
            continue
        img = cv2.imread(str(pngs[-1]))
        if img is None:
            continue
        img = cv2.resize(img, tile)
        cv2.putText(img, d.name, (8, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        tiles.append(img)
    if not tiles:
        return None

    while len(tiles) % cols:
        tiles.append(np.zeros((tile[1], tile[0], 3), np.uint8))
    rows = [np.hstack(tiles[i : i + cols]) for i in range(0, len(tiles), cols)]
    out = outdir / f"sheet_{lane_type}.png"
    cv2.imwrite(str(out), np.vstack(rows))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Debug artifact generator")
    ap.add_argument("--contact-sheet", action="store_true")
    ap.add_argument("--all-lanes", action="store_true")
    ap.add_argument("--lane", default="water")
    ap.add_argument("--prune", action="store_true")
    args = ap.parse_args()

    lanes = [t.value for t in LaneType] if args.all_lanes else [args.lane]
    if args.prune:
        for lane in lanes:
            print(f"{lane}: pruned {prune(lane)} dumps")
        return
    if args.contact_sheet:
        for lane in lanes:
            out = contact_sheet(lane)
            print(f"{lane}: {out}" if out else f"{lane}: no dumps yet")
        print("\nNow actually open the image and look at it before declaring success.")


if __name__ == "__main__":
    main()
