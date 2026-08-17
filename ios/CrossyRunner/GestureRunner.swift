//  GestureRunner.swift — persistent XCUITest runner, TCP :9100.
//
//  Protocol: docs/tap-protocol.md. 10-byte commands in, 4-byte acks out.
//
//  Why not WebDriverAgent: its HTTP/JSON round trip adds 40-80ms of pure
//  overhead, and latency is the thing that drives p. This is a 10-byte write on
//  a warm socket.
//
//  Run with:
//    xcodebuild test-without-building \
//      -xctestrun dd/Build/Products/CrossyRunner_iphoneos.xctestrun \
//      -destination "platform=iOS,id=$UDID"
//  and on the host:  iproxy 9100 9100

import Foundation
import Network
import XCTest

private let kPort: UInt16 = 9100

private enum Op: UInt8 {
    case tap          = 0x01
    case swipeLeft    = 0x02
    case swipeRight   = 0x03
    case swipeDown    = 0x04
    case swipeUp      = 0x05
    case ping         = 0x10
    case activate     = 0x11
    case setDefaults  = 0x12
    case relaunch     = 0x13
}

private enum Status: UInt8 {
    case ok            = 0
    case badOpcode     = 1
    case notForeground = 2
    case gestureThrew  = 3
}

final class GestureRunner: XCTestCase {

    private var app: XCUIApplication!
    private var listener: NWListener!
    /// Gestures are serialized: XCUITest synthesis is not reentrant, and two
    /// overlapping drags produce input the game interprets as neither.
    private let gestureQueue = DispatchQueue(label: "crossy.gesture")
    private let netQueue = DispatchQueue(label: "crossy.net")

    private var swipeDist: CGFloat = 0.18      // fraction of the shorter screen axis
    private var swipeDurMs: UInt16 = 50
    /// Cached once. Querying the screen per gesture (a screenshot, or an
    /// accessibility round trip) would add tens of ms to every lateral move.
    private var shorterAxis: CGFloat = 390

    override func setUp() {
        super.setUp()
        continueAfterFailure = true
        // Without this the default test timeout kills the runner mid-night on
        // its own schedule (runbook §10.2). supervisor.py restarts it, but each
        // restart costs ~20s of runner boot plus a lost run.
        executionTimeAllowance = 60 * 60 * 12
    }

    func testServe() throws {
        app = XCUIApplication(bundleIdentifier: kCrossyBundleID)
        app.activate()
        XCTAssertTrue(app.wait(for: .runningForeground, timeout: 30),
                      "Crossy Road did not foreground — check the bundle ID.")

        let bounds = app.frame
        if bounds.width > 0, bounds.height > 0 {
            shorterAxis = min(bounds.width, bounds.height)
        }
        NSLog("[runner] screen \(bounds.size), swipe travel base \(shorterAxis)pt")

        try startListener()
        NSLog("[runner] listening on :\(kPort)")

        // Park the test on the run loop. The expectation is never fulfilled;
        // the runner lives until the supervisor kills it or the allowance ends.
        let forever = XCTestExpectation(description: "serve")
        wait(for: [forever], timeout: TimeInterval(executionTimeAllowance) - 30)
    }

    // MARK: - Networking

    private func startListener() throws {
        let params = NWParameters.tcp
        params.allowLocalEndpointReuse = true
        if let tcp = params.defaultProtocolStack.internetProtocol as? NWProtocolTCP.Options {
            tcp.noDelay = true            // never Nagle a 10-byte command
        }
        listener = try NWListener(using: params, on: NWEndpoint.Port(rawValue: kPort)!)
        listener.newConnectionHandler = { [weak self] conn in
            guard let self else { return }
            NSLog("[runner] client connected")
            conn.start(queue: self.netQueue)
            self.receive(on: conn, buffer: Data())
        }
        listener.start(queue: netQueue)
    }

    /// Commands are fixed-width, so framing is just "accumulate to 10 bytes".
    /// A partial write can never desynchronize the stream permanently.
    private func receive(on conn: NWConnection, buffer: Data) {
        conn.receive(minimumIncompleteLength: 1, maximumLength: 4096) { [weak self] data, _, done, error in
            guard let self else { return }
            var buf = buffer
            if let data, !data.isEmpty { buf.append(data) }

            while buf.count >= 10 {
                let frame = buf.prefix(10)
                buf = buf.dropFirst(10)
                self.dispatch(frame: Data(frame), conn: conn)
            }

            if let error {
                NSLog("[runner] recv error: \(error)")
                conn.cancel()
                return
            }
            if done {
                NSLog("[runner] client disconnected")
                conn.cancel()
                return
            }
            self.receive(on: conn, buffer: buf)
        }
    }

    private func u16(_ d: Data, _ i: Int) -> UInt16 {
        (UInt16(d[d.startIndex + i]) << 8) | UInt16(d[d.startIndex + i + 1])
    }

    private func dispatch(frame: Data, conn: NWConnection) {
        let rawOp = frame[frame.startIndex]
        let seq = frame[frame.startIndex + 1]
        let x = CGFloat(u16(frame, 2)) / 10000.0
        let y = CGFloat(u16(frame, 4)) / 10000.0
        let dist = CGFloat(u16(frame, 6)) / 10000.0
        let dur = u16(frame, 8)

        guard let op = Op(rawValue: rawOp) else {
            ack(conn, seq: seq, status: .badOpcode, durMs: 0)
            return
        }

        // Hand off immediately so the network queue stays responsive while a
        // gesture (which can take >100ms) is synthesized.
        gestureQueue.async { [weak self] in
            guard let self else { return }
            let t0 = Date()

            // XCUIApplication is main-thread-only. activate() does not merely
            // misbehave off-main, it throws NSInternalInconsistencyException and
            // takes the whole runner down — and `activate` is what supervisor.py
            // sends to recover from a stall, so the crash lands exactly when
            // recovery is being attempted. `state` and gesture synthesis have the
            // same affinity requirement; they just fail less loudly.
            //
            // gestureQueue still does the serialization it was added for. Hopping
            // to main here supplies thread affinity on top of it. Both are needed:
            // main alone would let the network queue enqueue overlapping gestures,
            // gestureQueue alone crashes.
            let status: Status = DispatchQueue.main.sync {
                switch op {
                case .ping:
                    return .ok
                case .setDefaults:
                    if dist > 0 { self.swipeDist = dist }
                    if dur > 0 { self.swipeDurMs = dur }
                    return .ok
                case .activate:
                    self.app.activate()
                    return .ok
                case .relaunch:
                    // The escape hatch CLAUDE.md:59 requires: a fullscreen
                    // interstitial does not care that you activated the app,
                    // because the app IS frontmost — the ad is inside it. Only a
                    // terminate breaks out. activate() alone is why a stuck
                    // 2am session stays stuck.
                    //
                    // NOT for use in the scoring path. Invariant 6 forbids
                    // force-quitting a run in progress: the score only submits if
                    // the run ends in-game. This is for ad/unknown-state recovery
                    // only, where there is no run worth preserving.
                    self.app.terminate()
                    self.app.activate()
                    return .ok
                case .tap:
                    guard self.app.state == .runningForeground else { return .notForeground }
                    self.app.coordinate(withNormalizedOffset: CGVector(dx: x, dy: y)).tap()
                    return .ok
                case .swipeLeft, .swipeRight, .swipeUp, .swipeDown:
                    guard self.app.state == .runningForeground else { return .notForeground }
                    self.swipe(op, x: x, y: y, dist: dist, durMs: dur)
                    return .ok
                }
            }

            let ms = UInt16(min(65535, Int(Date().timeIntervalSince(t0) * 1000)))
            self.ack(conn, seq: seq, status: status, durMs: ms)
        }
    }

    private func swipe(_ op: Op, x: CGFloat, y: CGFloat, dist: CGFloat, durMs: UInt16) {
        let travel = (dist > 0 ? dist : swipeDist) * shorterAxis
        let duration = TimeInterval(durMs > 0 ? durMs : swipeDurMs) / 1000.0

        var d = CGVector(dx: 0, dy: 0)
        switch op {
        case .swipeLeft:  d = CGVector(dx: -travel, dy: 0)
        case .swipeRight: d = CGVector(dx:  travel, dy: 0)
        case .swipeUp:    d = CGVector(dx: 0, dy: -travel)
        case .swipeDown:  d = CGVector(dx: 0, dy:  travel)
        default: return
        }

        let origin = app.coordinate(withNormalizedOffset: CGVector(dx: x, dy: y))
        origin.press(forDuration: duration, thenDragTo: origin.withOffset(d))
    }

    private func ack(_ conn: NWConnection, seq: UInt8, status: Status, durMs: UInt16) {
        var out = Data([seq, status.rawValue])
        out.append(UInt8(durMs >> 8))
        out.append(UInt8(durMs & 0xFF))
        // Fire and forget: a wedged client must never back-pressure the runner.
        conn.send(content: out, completion: .contentProcessed { _ in })
    }
}
