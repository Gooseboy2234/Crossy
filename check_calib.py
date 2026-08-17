"""Calibration sanity check. Wired to the PostToolUse hook.

Runs after any edit to perceive.py or calib.yaml. Cheap, and it catches the
class of mistake that otherwise shows up as an hour of confused perception
debugging.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List

from config import load_calib


def problems() -> List[str]:
    out: List[str] = []
    try:
        c = load_calib()
    except Exception as e:                       # noqa: BLE001 - report, don't raise
        return [f"calib.yaml will not load: {e}"]

    g, cap = c.geometry, c.capture

    if not c.measured:
        out.append("measured: false — every value here is a PLACEHOLDER (Phase 3)")

    for key in ("px_per_row", "px_per_col", "row0_screen_y", "playfield_top_y"):
        if key not in g:
            out.append(f"geometry.{key} missing")
    if not out:
        if g["px_per_row"] <= 0 or g["px_per_col"] <= 0:
            out.append("px_per_row / px_per_col must be positive")
        if g["playfield_top_y"] >= g["row0_screen_y"]:
            out.append("playfield_top_y must be above row0_screen_y (smaller y)")

        # The Dynamic Island occupies top-centre and expands for Live Activities,
        # charging, and background audio. An expanded island overlapping a tracked
        # ROI looks exactly like a perception bug and will eat an hour.
        if g["playfield_top_y"] < 200:
            out.append(
                f"playfield_top_y={g['playfield_top_y']} may sit under the Dynamic "
                f"Island — verify against a real frame with the island expanded"
            )
        roi = g.get("score_roi")
        if roi and len(roi) == 4:
            x1, y1, x2, y2 = roi
            if x2 <= x1 or y2 <= y1:
                out.append("score_roi is not a valid x1,y1,x2,y2 box")
            h = cap.get("scale_h")
            if h and y1 < 0.09 * h and x2 > 0.30 * (cap.get("scale_w") or 1):
                out.append("score_roi reaches into the Dynamic Island's zone")

        if g.get("col_min", -5) >= g.get("col_max", 5):
            out.append("col_min must be < col_max")

    for name in ("grass", "road", "water", "track"):
        r = c.lanes.get(name)
        if not r:
            out.append(f"lanes.{name} missing")
            continue
        for ch, hi in (("h", 179), ("s", 255), ("v", 255)):
            lo_v, hi_v = r.get(ch, [None, None])
            if lo_v is None or hi_v is None:
                out.append(f"lanes.{name}.{ch} missing")
            elif not (0 <= lo_v <= hi_v <= hi):
                out.append(f"lanes.{name}.{ch}={r[ch]} out of range 0..{hi} or inverted")

    fps = cap.get("fps")
    if fps and fps > 60:
        out.append(
            f"capture.fps={fps} — do not assume >60. Crossy Road is a 2014 Unity "
            f"title and the AVFoundation path typically delivers 60 regardless of "
            f"panel refresh. Measure it (capture.py) and design to the measurement."
        )
    if cap.get("device_index") is None:
        out.append("capture.device_index unset (ffmpeg -f avfoundation -list_devices true -i \"\")")

    return out


def main() -> int:
    issues = problems()
    if not issues:
        print("calib: ok")
        return 0
    print("calib check:")
    for i in issues:
        print(f"  - {i}")
    # Non-zero only for real errors; an unmeasured placeholder file is the
    # expected state early on and must not block editing.
    hard = [i for i in issues if "PLACEHOLDER" not in i]
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main())
