# Wire protocol — host ⇄ XCUITest runner

`iproxy 9100 9100`, then a warm TCP socket to `127.0.0.1:9100`.

The runbook's original protocol was a bare 4-byte `(x, y)` pair. That cannot
express a direction, and Crossy Road's lateral moves are **swipes** — so it could
only ever drive `forward`. This replaces it.

## Command frame — 10 bytes, big-endian, fixed size

```
offset  size  field     meaning
0       1     opcode    see table
1       1     seq       wraps at 256; echoed in the ack
2       2     x0        gesture origin, normalized × 10000
4       2     y0        gesture origin, normalized × 10000
6       2     dist      gesture travel, normalized × 10000 (swipes only)
8       2     dur_ms    press-and-drag duration, ms (swipes only)
```

Fixed size on purpose: the runner reads exactly 10 bytes and never has to parse
a length prefix, so a partial write can't desynchronize the stream permanently.

| opcode | name | notes |
|---|---|---|
| `0x01` | `TAP` | hop forward. Only `x0,y0` are read. |
| `0x02` | `SWIPE_LEFT` | sidestep left |
| `0x03` | `SWIPE_RIGHT` | sidestep right |
| `0x04` | `SWIPE_DOWN` | step back |
| `0x05` | `SWIPE_UP` | untested alternate forward — Phase 0 should check whether the game accepts it, since a swipe-up that works would let every action share one latency profile |
| `0x10` | `PING` | acked, no input synthesized. Heartbeat for the supervisor. |
| `0x11` | `ACTIVATE` | `XCUIApplication.activate()` — recover from a background/crash |
| `0x12` | `SET_DEFAULTS` | store `dist`/`dur_ms` as defaults for later gestures |

## Ack frame — 4 bytes, big-endian

```
offset  size  field
0       1     seq       echoed
1       1     status    0 = ok, 1 = bad opcode, 2 = app not foreground, 3 = gesture threw
2       2     dur_ms    time the runner spent inside the XCUITest call
```

Acks are **not** waited on. The client fires and forgets; a reader thread matches
acks by `seq` and keeps a rolling round-trip distribution per opcode.

This is the point of the ack: it turns `latency_offset_ms` from a hand-tuned
constant into a **measured** one, and it measures taps and swipes *separately*.
They will not match — `press(forDuration:thenDragTo:)` carries meaningfully more
overhead than `tap()`, and the runbook's single latency constant silently
averages two different numbers together.

The runner-side `dur_ms` also isolates *where* the latency is: a large `dur_ms`
means XCUITest's event synthesis is the bottleneck and no amount of socket
tuning will help.

## Failure semantics

- The runner never blocks on a slow client. A full send buffer drops the ack.
- The client never blocks on the runner. Missing acks show up as an RTT
  measurement gap, which `supervisor.py` reads as a stall.
- The connection is expected to be long-lived. On drop, the client reconnects
  with backoff and the runner keeps listening.
