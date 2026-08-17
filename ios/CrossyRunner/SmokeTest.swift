//  SmokeTest.swift — Phase 0 go/no-go.
//
//  The runbook's version tapped five times and called that a PASS. That only
//  proves Unity accepts synthetic *taps*. Crossy Road's lateral moves are
//  swipes, and a synthetic drag is a materially harder thing to get through
//  backboardd into Unity's input layer than a tap is. If swipes don't land, the
//  bot can only move forward, can never line up with a log, and the project is
//  over — so the gate has to test them.
//
//  PASS: five forward hops, then a VISIBLE left step and a VISIBLE right step.
//  FAIL on the swipes: stop. Try the tuning grid below once, then close the file.
//
//  Watch the screen. This test cannot assert on Unity's internal state — you are
//  the oracle.

import XCTest

let kCrossyBundleID = "com.hipsterwhale.crossy"   // NOT com.yesterday.crossyroad

final class SmokeTest: XCTestCase {

    override func setUp() {
        super.setUp()
        continueAfterFailure = true
        // Default allowance will otherwise kill a long runner mid-night for no
        // visible reason (runbook §10.2).
        executionTimeAllowance = 3600
    }

    private func launch() -> XCUIApplication {
        let app = XCUIApplication(bundleIdentifier: kCrossyBundleID)
        app.activate()
        XCTAssertTrue(app.wait(for: .runningForeground, timeout: 20),
                      "Crossy Road did not come to the foreground. Wrong bundle ID? "
                      + "Check with `ideviceinstaller -l`.")
        sleep(6)   // splash, Game Center banner, possible interstitial
        return app
    }

    /// Time one synthesized event. This is XCUITest's own synthesis cost — it is
    /// the floor on latency and no amount of socket tuning gets under it.
    @discardableResult
    private func timed(_ label: String, _ body: () -> Void) -> Double {
        let t0 = Date()
        body()
        let ms = Date().timeIntervalSince(t0) * 1000.0
        print(String(format: "[phase0] %@ synthesis: %.1f ms", label, ms))
        return ms
    }

    // MARK: - Part 1: taps

    func test01_ForwardTaps() throws {
        let app = launch()
        let centre = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.62))

        var times: [Double] = []
        for i in 0..<5 {
            times.append(timed("tap\(i)") { centre.tap() })
            usleep(400_000)
        }
        sleep(2)

        let median = times.sorted()[times.count / 2]
        print(String(format: "[phase0] tap synthesis median: %.1f ms", median))
        print("[phase0] PASS CONDITION: the chicken hopped forward five times.")
    }

    // MARK: - Part 2: swipes — the real gate

    /// A flick: brief press, then drag. `duration` is the hold *before* travel;
    /// keep it small or Unity reads a drag rather than a swipe.
    private func flick(_ app: XCUIApplication,
                       dx: CGFloat, dy: CGFloat,
                       duration: TimeInterval) {
        let start = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.62))
        let end = start.withOffset(CGVector(dx: dx, dy: dy))
        start.press(forDuration: duration, thenDragTo: end)
    }

    func test02_LateralSwipes() throws {
        let app = launch()

        // Hop forward a couple of times first — lateral movement on the menu
        // proves nothing.
        let centre = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.62))
        for _ in 0..<2 { centre.tap(); usleep(400_000) }

        let travel: CGFloat = 140   // points, ~18% of an iPhone Pro's width
        var times: [Double] = []

        print("[phase0] --- swipe LEFT ---")
        times.append(timed("swipe_left") { flick(app, dx: -travel, dy: 0, duration: 0.05) })
        sleep(1)
        print("[phase0] --- swipe RIGHT ---")
        times.append(timed("swipe_right") { flick(app, dx: travel, dy: 0, duration: 0.05) })
        sleep(1)
        print("[phase0] --- swipe RIGHT again ---")
        times.append(timed("swipe_right2") { flick(app, dx: travel, dy: 0, duration: 0.05) })
        sleep(2)

        let median = times.sorted()[times.count / 2]
        print(String(format: "[phase0] swipe synthesis median: %.1f ms", median))
        print("[phase0] PASS CONDITION: one visible step LEFT, then two visible steps RIGHT.")
        print("[phase0] If the chicken did not move sideways, run test03 before giving up.")
    }

    // MARK: - Part 3: tuning grid, only if test02 failed

    /// Unity's gesture recogniser has thresholds on travel distance and on how
    /// long the touch is held before it moves. A synthetic drag that is too slow
    /// reads as a pan and is discarded; too short and it reads as a tap — which
    /// looks like "the swipe became a forward hop", a very diagnostic symptom.
    ///
    /// Watch which combination produces a clean sideways step.
    func test03_SwipeTuningGrid() throws {
        let app = launch()
        let centre = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.62))
        for _ in 0..<2 { centre.tap(); usleep(400_000) }

        let durations: [TimeInterval] = [0.01, 0.05, 0.12, 0.25]
        let travels: [CGFloat] = [60, 120, 200, 320]

        for d in durations {
            for t in travels {
                print(String(format: "[phase0] grid: duration=%.2fs travel=%.0fpt -> LEFT", d, t))
                flick(app, dx: -t, dy: 0, duration: d)
                sleep(1)
                print(String(format: "[phase0] grid: duration=%.2fs travel=%.0fpt -> RIGHT", d, t))
                flick(app, dx: t, dy: 0, duration: d)
                sleep(1)
            }
        }
        print("[phase0] Note the smallest duration that moved the chicken reliably; "
              + "that is swipe_dur_ms in taps.py.")
    }

    // MARK: - Part 4: does swipe-up substitute for tap?

    /// If swipe-up also hops forward, every action shares one gesture path and
    /// therefore one latency profile — which makes latency compensation
    /// meaningfully easier than mixing a fast tap with a slow drag.
    func test04_SwipeUpAsForward() throws {
        let app = launch()
        let centre = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.62))
        centre.tap(); usleep(400_000)

        for _ in 0..<3 {
            flick(app, dx: 0, dy: -140, duration: 0.05)
            sleep(1)
        }
        print("[phase0] PASS CONDITION: three forward hops from swipe-up alone.")
        print("[phase0] If yes, prefer swipe-up for `forward` so all four actions "
              + "share one latency distribution.")
    }
}
