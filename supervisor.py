"""Watchdog. Relaunches the XCUITest runner on stall.

Do not trust the XCUITest runner to live 8 hours. Test runners get killed by
timeouts, memory pressure, and iOS itself. This process owns the lifecycle so
that a dead runner costs 30 seconds instead of a night.

"Overnight run produced 3 results then nothing" means the runner died and this
was not running, or the heartbeat was never wired up.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

from config import HEARTBEAT, LOCKFILE, MAX_RUNS, ROOT

log = logging.getLogger("supervisor")

STALL_S = 45.0
RUNNER_BOOT_S = 20.0


def healthy(stall_s: float = STALL_S) -> bool:
    return HEARTBEAT.exists() and (time.time() - HEARTBEAT.stat().st_mtime) < stall_s


def device_connected(udid: str) -> bool:
    try:
        out = subprocess.run(["idevice_id", "-l"], capture_output=True, text=True, timeout=10)
        return udid in out.stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


def restart_runner(udid: str, xctestrun: Path) -> None:
    log.warning("restarting XCUITest runner")
    subprocess.run(["pkill", "-f", "xcodebuild"], check=False)
    time.sleep(3)
    subprocess.Popen(
        ["xcodebuild", "test-without-building",
         "-xctestrun", str(xctestrun),
         "-destination", f"platform=iOS,id={udid}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(RUNNER_BOOT_S)   # runner boot + socket bind


def ensure_iproxy(port: int = 9100) -> Optional[subprocess.Popen]:
    try:
        return subprocess.Popen(["iproxy", str(port), str(port)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        log.error("iproxy not found (brew install libimobiledevice)")
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Runner watchdog")
    ap.add_argument("--udid", default=os.environ.get("UDID"))
    ap.add_argument("--xctestrun", default=None)
    ap.add_argument("--stall-s", type=float, default=STALL_S)
    ap.add_argument("--poll-s", type=float, default=5.0)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not args.udid:
        print("set --udid or $UDID (idevice_id -l)")
        return 1

    xctestrun = Path(args.xctestrun) if args.xctestrun else None
    if xctestrun is None:
        found = sorted((ROOT / "ios" / "dd" / "Build" / "Products").glob("*.xctestrun"))
        if not found:
            print("no .xctestrun found — run ios/resign.sh first")
            return 1
        xctestrun = found[0]
    log.info("watching heartbeat %s, xctestrun %s", HEARTBEAT, xctestrun.name)

    restarts = 0
    while not LOCKFILE.exists():
        if not device_connected(args.udid):
            log.error("device gone — stopping")
            return 1
        if not healthy(args.stall_s):
            age = time.time() - HEARTBEAT.stat().st_mtime if HEARTBEAT.exists() else -1
            log.warning("stall detected (heartbeat age %.0fs)", age)
            restart_runner(args.udid, xctestrun)
            restarts += 1
        time.sleep(args.poll_s)

    log.info("lockfile appeared — bot succeeded. %d runner restarts this session.", restarts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
