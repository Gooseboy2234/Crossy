"""Client for the XCUITest gesture runner. See docs/tap-protocol.md.

Crossy Road's lateral moves are swipes, so this is a gesture channel, not a tap
channel. `forward` is a tap; `left`/`right` are short drags.

Two things this does beyond sending bytes:

  * Fire-and-forget sends with an async ack reader, so a slow XCUITest gesture
    never stalls the planning loop.
  * A **per-opcode** rolling latency estimate. Taps and swipes do not cost the
    same, and folding them into one `latency_offset_ms` is how you end up with
    lateral moves that are systematically mistimed.
"""

from __future__ import annotations

import logging
import socket
import statistics
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Optional

log = logging.getLogger("taps")

HOST, PORT = "127.0.0.1", 9100

TAP = 0x01
SWIPE_LEFT = 0x02
SWIPE_RIGHT = 0x03
SWIPE_DOWN = 0x04
SWIPE_UP = 0x05
PING = 0x10
ACTIVATE = 0x11
SET_DEFAULTS = 0x12
#: terminate + relaunch. The only thing that escapes a fullscreen interstitial —
#: ACTIVATE cannot, because the app is already frontmost and the ad lives inside
#: it. Required by CLAUDE.md:59's 45s ad timeout and by supervisor.py's stall
#: recovery. Never call it during a scoring run: invariant 6 requires the run to
#: end in-game or the score never submits.
RELAUNCH = 0x13

OPCODE_NAME = {
    TAP: "tap", SWIPE_LEFT: "swipe_left", SWIPE_RIGHT: "swipe_right",
    SWIPE_DOWN: "swipe_down", SWIPE_UP: "swipe_up",
    PING: "ping", ACTIVATE: "activate", SET_DEFAULTS: "set_defaults",
    RELAUNCH: "relaunch",
}

#: plan.py action -> opcode. `wait` deliberately has no entry: waiting is the
#: absence of input, and sending anything for it would be a bug.
ACTION_OPCODE = {"forward": TAP, "left": SWIPE_LEFT, "right": SWIPE_RIGHT, "back": SWIPE_DOWN}

CMD = struct.Struct(">BBHHHH")   # opcode, seq, x0, y0, dist, dur_ms
ACK = struct.Struct(">BBH")      # seq, status, runner_dur_ms
STATUS = {0: "ok", 1: "bad_opcode", 2: "not_foreground", 3: "gesture_threw"}


def _n(v: float) -> int:
    """Normalized 0..1 -> uint16, clamped."""
    return max(0, min(10000, int(round(v * 10000))))


@dataclass
class LatencyStats:
    """Rolling round-trip times, keyed by opcode."""

    window: int = 64
    rtt: Dict[int, Deque[float]] = field(default_factory=dict)
    runner: Dict[int, Deque[float]] = field(default_factory=dict)
    dropped: int = 0

    def record(self, opcode: int, rtt_ms: float, runner_ms: float) -> None:
        self.rtt.setdefault(opcode, deque(maxlen=self.window)).append(rtt_ms)
        self.runner.setdefault(opcode, deque(maxlen=self.window)).append(runner_ms)

    def median(self, opcode: int) -> Optional[float]:
        d = self.rtt.get(opcode)
        return statistics.median(d) if d else None

    def p90(self, opcode: int) -> Optional[float]:
        d = self.rtt.get(opcode)
        if not d:
            return None
        s = sorted(d)
        return s[min(len(s) - 1, int(0.9 * len(s)))]

    def summary(self) -> str:
        parts = []
        for op, d in sorted(self.rtt.items()):
            if d:
                parts.append(
                    f"{OPCODE_NAME.get(op, op)}: n={len(d)} "
                    f"med={statistics.median(d):.0f}ms p90={self.p90(op):.0f}ms "
                    f"runner={statistics.median(self.runner[op]):.0f}ms"
                )
        if self.dropped:
            parts.append(f"dropped_acks={self.dropped}")
        return " | ".join(parts) or "no samples"


class TapClient:
    """Long-lived connection to the on-device runner.

    Not thread-safe for concurrent `send`; the planning loop is single-threaded
    and should stay that way.
    """

    def __init__(
        self,
        host: str = HOST,
        port: int = PORT,
        swipe_dist: float = 0.18,      # fraction of screen width
        swipe_dur_ms: int = 50,
        origin: tuple[float, float] = (0.5, 0.62),
        connect_timeout: float = 5.0,
    ):
        self.host, self.port = host, port
        self.swipe_dist = swipe_dist
        self.swipe_dur_ms = swipe_dur_ms
        self.origin = origin
        self.connect_timeout = connect_timeout
        self.sock: Optional[socket.socket] = None
        self.stats = LatencyStats()
        self._seq = 0
        self._sent_at: Dict[int, tuple[float, int]] = {}
        self._lock = threading.Lock()
        self._reader: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # -- lifecycle ---------------------------------------------------------

    def connect(self, retries: int = 5) -> None:
        delay = 0.5
        last: Optional[Exception] = None
        for _ in range(retries):
            try:
                s = socket.create_connection((self.host, self.port), self.connect_timeout)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.sock = s
                self._stop.clear()
                self._reader = threading.Thread(target=self._read_acks, daemon=True)
                self._reader.start()
                log.info("connected to runner at %s:%d", self.host, self.port)
                return
            except OSError as e:      # noqa: PERF203 - retry loop
                last = e
                time.sleep(delay)
                delay = min(delay * 2, 8.0)
        raise ConnectionError(
            f"no runner on {self.host}:{self.port} after {retries} tries "
            f"(is `iproxy 9100 9100` up, and is the XCUITest runner running?)"
        ) from last

    def close(self) -> None:
        self._stop.set()
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.sock.close()
            self.sock = None

    def __enter__(self) -> "TapClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- sending -----------------------------------------------------------

    def _send(self, opcode: int, x: float, y: float, dist: float, dur_ms: int) -> int:
        if self.sock is None:
            raise ConnectionError("not connected")
        with self._lock:
            self._seq = (self._seq + 1) % 256
            seq = self._seq
            self._sent_at[seq] = (time.perf_counter(), opcode)
            # Bound the outstanding map: a runner that stops acking must not
            # grow this without limit.
            if len(self._sent_at) > 512:
                for k in sorted(self._sent_at, key=lambda k: self._sent_at[k][0])[:256]:
                    self._sent_at.pop(k, None)
                    self.stats.dropped += 1
        self.sock.sendall(CMD.pack(opcode, seq, _n(x), _n(y), _n(dist), dur_ms))
        return seq

    def act(self, action: str) -> Optional[int]:
        """Send a plan.py action. Returns the seq, or None for `wait`."""
        op = ACTION_OPCODE.get(action)
        if op is None:
            return None                    # `wait` sends nothing, by design
        x, y = self.origin
        return self._send(op, x, y, self.swipe_dist, self.swipe_dur_ms)

    def tap(self, x: Optional[float] = None, y: Optional[float] = None) -> int:
        ox, oy = self.origin
        return self._send(TAP, x if x is not None else ox, y if y is not None else oy, 0, 0)

    def ping(self) -> int:
        return self._send(PING, 0, 0, 0, 0)

    def activate(self) -> int:
        return self._send(ACTIVATE, 0, 0, 0, 0)

    def relaunch(self) -> int:
        """Terminate the app and bring it back — the ad/stall escape hatch.

        Use this, not activate(), whenever the screen is unrecognised or stuck:
        an interstitial runs INSIDE Crossy Road, so the app is already frontmost
        and activate() is a no-op against it.

        Must never fire during a scoring run (invariant 6).
        """
        return self._send(RELAUNCH, 0, 0, 0, 0)

    def set_defaults(self, dist: float, dur_ms: int) -> int:
        self.swipe_dist, self.swipe_dur_ms = dist, dur_ms
        return self._send(SET_DEFAULTS, 0, 0, dist, dur_ms)

    # -- acks --------------------------------------------------------------

    def _read_acks(self) -> None:
        buf = b""
        while not self._stop.is_set() and self.sock is not None:
            try:
                chunk = self.sock.recv(256)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while len(buf) >= ACK.size:
                seq, status, runner_ms = ACK.unpack(buf[: ACK.size])
                buf = buf[ACK.size :]
                with self._lock:
                    sent = self._sent_at.pop(seq, None)
                if sent is None:
                    continue
                t0, opcode = sent
                rtt = (time.perf_counter() - t0) * 1000.0
                self.stats.record(opcode, rtt, float(runner_ms))
                if status:
                    log.warning("runner status %s for %s",
                                STATUS.get(status, status), OPCODE_NAME.get(opcode, opcode))

    # -- measurement -------------------------------------------------------

    def measured_latency_ms(self, action: str, fallback: float) -> float:
        """Best current estimate of tap-to-movement latency for one action.

        Half the RTT is the outbound leg; the runner's own `dur_ms` happens on
        the far side and is already inside the RTT, so it is not added again.
        Returns `fallback` until enough samples exist to beat a guess.
        """
        op = ACTION_OPCODE.get(action)
        if op is None:
            return 0.0
        d = self.stats.rtt.get(op)
        if not d or len(d) < 8:
            return fallback
        return statistics.median(d) / 2.0 + statistics.median(self.stats.runner[op])


if __name__ == "__main__":
    # Manual bring-up check. Run after `iproxy 9100 9100` and the XCUITest runner.
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    with TapClient() as c:
        c.activate()
        time.sleep(4.0)
        for _ in range(5):
            c.tap()
            time.sleep(0.4)
        for action in ("left", "right", "left", "right"):
            c.act(action)
            time.sleep(0.6)
        time.sleep(1.0)
        print(c.stats.summary())
