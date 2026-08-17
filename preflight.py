"""The "before any overnight run" checklist, as code.

Some items a program can verify; several it cannot, and pretending otherwise is
worse than asking. The ones that need a human ask for a human.

The two that actually bite:
  - starting a grind on day 6 of a 7-day profile: the runner dies at 3am
  - debug retention uncapped: 20 PNGs x 300 deaths fills a drive by morning
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

import check_calib
from config import DEBUG_DIR, HEARTBEAT, LOCKFILE, LOGS, ROOT, THRESHOLD

LAST_SIGN = ROOT / ".last_sign"
MAX_PROFILE_AGE_DAYS = 4.5
MIN_FREE_GB = 5.0


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    blocking: bool = True
    manual: bool = False


def _profile_age() -> Check:
    if not LAST_SIGN.exists():
        return Check("profile signed", False, "no .last_sign — run ios/resign.sh")
    try:
        age = (time.time() - float(LAST_SIGN.read_text().strip())) / 86400.0
    except ValueError:
        return Check("profile signed", False, ".last_sign is not a timestamp")
    if age > MAX_PROFILE_AGE_DAYS:
        return Check(
            "profile signed", False,
            f"{age:.1f} days old (>{MAX_PROFILE_AGE_DAYS}). Free profiles expire at 7 "
            f"days; the runner will die mid-night. Re-sign first.",
        )
    return Check("profile signed", True, f"{age:.1f} days old")


def _lockfile() -> Check:
    if LOCKFILE.exists():
        return Check("lockfile absent", False,
                     f"{LOCKFILE} exists: {LOCKFILE.read_text().strip()} — bot is retired")
    return Check("lockfile absent", True)


def _disk() -> Check:
    free_gb = shutil.disk_usage(ROOT).free / 1e9
    ok = free_gb >= MIN_FREE_GB
    return Check("disk space", ok, f"{free_gb:.1f} GB free (want >= {MIN_FREE_GB})")


def _debug_retention() -> Check:
    n = sum(1 for _ in DEBUG_DIR.rglob("*.png")) if DEBUG_DIR.exists() else 0
    ok = n < 2000
    return Check("debug retention", ok,
                 f"{n} PNGs in debug/ — run `python debug.py --prune --all-lanes`"
                 if not ok else f"{n} PNGs")


def _calib() -> Check:
    issues = check_calib.problems()
    hard = [i for i in issues if "PLACEHOLDER" not in i]
    if hard:
        return Check("calibration", False, "; ".join(hard[:3]))
    if issues:
        return Check("calibration", False,
                     "calib.yaml still has measured: false — Phase 3 not done")
    return Check("calibration", True)


def _tooling() -> Check:
    missing = [t for t in ("ffmpeg", "iproxy", "idevice_id") if shutil.which(t) is None]
    return Check("tooling", not missing,
                 f"missing: {', '.join(missing)}" if missing else "ffmpeg, iproxy, idevice_id")


def _device() -> Check:
    try:
        out = subprocess.run(["idevice_id", "-l"], capture_output=True, text=True, timeout=10)
        udids = [u for u in out.stdout.split() if u]
        return Check("device attached", bool(udids), ", ".join(udids) or "none")
    except (subprocess.SubprocessError, FileNotFoundError):
        return Check("device attached", False, "idevice_id unavailable")


def _p_hat() -> Check:
    """p̂ <= 1.5% measured ON DEVICE, not just in sim."""
    import analyze
    report = analyze.Report(analyze._read(analyze.DEATHS_CSV), analyze._read(analyze.RUNS_CSV))
    if report.total_deaths < 50:
        return Check("device p̂", False,
                     f"only {report.total_deaths} logged deaths — need >=50 for even a "
                     f"coarse estimate (±{100/max(report.total_deaths,1)**0.5:.0f}%)")
    p = report.p_hat
    ok = p <= 0.015
    return Check("device p̂", ok,
                 f"{p*100:.2f}% ±{report.rel_se*100:.0f}% over {report.total_deaths} deaths "
                 f"(target <= 1.50%)")


MANUAL = [
    "Auto-Lock = Never, Do Not Disturb on, charging",
    "Phone on a hard surface, fan pointed at it",
    "Playing the ORIGINAL world (not Space/Dinosaur — different eagle, different palette)",
    "No limited-time event mode active (Hopside Down / Crashy Cart replace the main game)",
    "Watched 10 supervised runs tonight with these exact params",
    "Ad strategy verified: 10 manual runs, ad count counted",
]


def run(quiet: bool = False) -> bool:
    checks: List[Check] = [
        _profile_age(), _lockfile(), _tooling(), _device(),
        _calib(), _disk(), _debug_retention(), _p_hat(),
    ]
    blocking_failures = [c for c in checks if not c.ok and c.blocking]

    if not quiet:
        print(f"preflight — threshold {THRESHOLD}\n")
        for c in checks:
            mark = "ok  " if c.ok else "FAIL"
            print(f"  [{mark}] {c.name:<18} {c.detail}")
        print("\n  Cannot be checked from here — confirm by hand:")
        for m in MANUAL:
            print(f"  [ ?  ] {m}")
        print()
        if blocking_failures:
            print(f"{len(blocking_failures)} blocking failure(s). Do not start an "
                  f"overnight grind.")
        else:
            print("Automated checks pass. The manual list is the rest of the job.")

    return not blocking_failures


def main() -> int:
    ap = argparse.ArgumentParser(description="Pre-overnight checklist")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args()
    return 0 if run(quiet=args.quiet) else 1


if __name__ == "__main__":
    sys.exit(main())
