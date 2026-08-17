//  wincap — stream ONE macOS window as rawvideo on stdout, via ScreenCaptureKit.
//
//  WHY THIS EXISTS
//
//  The AirPlay mirror is the only working frame source on this machine (Apple
//  removed the CoreMediaIO DAL plug-in — see docs/capture-paths.md). But AirPlay
//  Receiver renders the phone fullscreen on its own Space, and
//  `ffmpeg -f avfoundation -i "Capture screen 0"` captures whatever Space is
//  currently DISPLAYED. So the moment the operator switches back to their desktop
//  to do anything, the capture silently starts recording their desktop instead of
//  the phone. That makes the machine unusable for the entire length of a run,
//  which is not an acceptable price for a bot that is supposed to run overnight
//  and be debugged interactively.
//
//  ScreenCaptureKit captures a window as a first-class object rather than a
//  rectangle of the visible display: SCContentFilter(desktopIndependentWindow:)
//  keeps delivering frames when the window is on an inactive Space, occluded, or
//  behind fullscreen apps. That is precisely the property needed here.
//
//  Output is raw BGRA, no container, no header — the same shape capture.py
//  already consumes from ffmpeg's rawvideo, so the reader does not care which
//  process is upstream. Dimensions go to stderr so stdout stays a pure frame
//  stream.
//
//  Build:
//    swiftc -O tools/screencap/wincap.swift -o tools/screencap/wincap
//  Use:
//    ./wincap --list
//    ./wincap --find-phone            # picks the window with the phone's aspect
//    ./wincap --window-id 12345 [--scale 0.5]

import AVFoundation
import CoreMedia
import Foundation
import ScreenCaptureKit

let PHONE_ASPECT = 1206.0 / 2622.0
let ASPECT_TOL = 0.03

func err(_ s: String) {
    FileHandle.standardError.write((s + "\n").data(using: .utf8)!)
}

/// onScreenWindowsOnly:false is the load-bearing argument — without it, windows
/// living on another Space are simply absent from the list, which is the exact
/// case this tool exists to handle.
func shareableWindows() async throws -> [SCWindow] {
    let content = try await SCShareableContent.excludingDesktopWindows(
        false, onScreenWindowsOnly: false)
    return content.windows
}

func describe(_ w: SCWindow) -> String {
    let app = w.owningApplication?.applicationName ?? "?"
    let title = w.title ?? ""
    let r = w.frame
    let aspect = r.height > 0 ? r.width / r.height : 0
    return String(format: "%d\t%@\t%@\t%.0fx%.0f\taspect=%.4f",
                  w.windowID, app, title, r.width, r.height, aspect)
}

/// Pick the window most likely to be the mirrored phone.
///
/// Matching on ASPECT rather than "the AirPlay app's window" on purpose: the
/// receiver's window is letterboxed differently across macOS versions, and other
/// tools (QuickTime, a simulator) can host the same content. A window whose
/// aspect matches the phone and which is reasonably large is a far more stable
/// signal than a process name.
/// System UI that is tall-and-narrow and would otherwise be a plausible match.
/// Control Center measures 522x1262 = aspect 0.4136 against the phone's 0.4600 —
/// outside tolerance, but close enough that a slightly looser bound or a
/// different macOS version would silently select it and hand the planner a
/// picture of the menu bar. Excluding by owner is cheap insurance.
let EXCLUDED_OWNERS: Set<String> = [
    "Control Center", "Notification Center", "Dock", "WindowManager",
    "Spotlight", "Screenshot",
]

func findPhoneWindow(_ windows: [SCWindow]) -> SCWindow? {
    windows
        .filter { $0.frame.height > 400 && $0.frame.width > 150 }
        .filter { !EXCLUDED_OWNERS.contains($0.owningApplication?.applicationName ?? "") }
        .filter { abs($0.frame.width / $0.frame.height - PHONE_ASPECT) < ASPECT_TOL }
        .max { $0.frame.height < $1.frame.height }
}

final class Out: NSObject, SCStreamOutput {
    private let out = FileHandle.standardOutput
    private var announced = false

    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer,
                of type: SCStreamOutputType) {
        guard type == .screen, sb.isValid else { return }

        // SCStream emits status-only buffers (occlusion changes, idle ticks) with
        // no image. Writing those as frames would desynchronise the raw stream.
        guard let pb = CMSampleBufferGetImageBuffer(sb) else { return }

        CVPixelBufferLockBaseAddress(pb, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(pb, .readOnly) }
        let w = CVPixelBufferGetWidth(pb)
        let h = CVPixelBufferGetHeight(pb)
        let stride = CVPixelBufferGetBytesPerRow(pb)
        guard let base = CVPixelBufferGetBaseAddress(pb) else { return }

        if !announced {
            err("wincap: \(w)x\(h) BGRA")
            announced = true
        }

        // Row-by-row: CVPixelBuffer rows are padded to an alignment that is almost
        // never w*4. Blitting the plane wholesale ships the padding too and shears
        // the image — which downstream looks exactly like a CV bug, not an I/O bug.
        var frame = Data(capacity: w * h * 4)
        for row in 0..<h {
            frame.append(Data(bytes: base.advanced(by: row * stride), count: w * 4))
        }
        out.write(frame)
    }
}

func run() async {
    let args = CommandLine.arguments
    let windows: [SCWindow]
    do {
        windows = try await shareableWindows()
    } catch {
        err("SCShareableContent failed: \(error.localizedDescription)")
        err("This usually means Screen Recording permission is not granted to the "
            + "process that spawned wincap.")
        exit(2)
    }

    if args.contains("--list") {
        for w in windows.sorted(by: { $0.frame.height > $1.frame.height }) {
            print(describe(w))
        }
        exit(0)
    }

    var target: SCWindow?
    if let i = args.firstIndex(of: "--window-id"), i + 1 < args.count,
       let id = UInt32(args[i + 1]) {
        target = windows.first { $0.windowID == id }
        if target == nil { err("no window with id \(id)"); exit(1) }
    } else {
        target = findPhoneWindow(windows)
        if target == nil {
            err("no window matching the phone's aspect (\(PHONE_ASPECT)). "
                + "Is Screen Mirroring running? Try --list.")
            exit(1)
        }
    }
    let win = target!
    err("wincap: window \(win.windowID) '\(win.title ?? "")' "
        + "\(Int(win.frame.width))x\(Int(win.frame.height))")

    var scale = 1.0
    if let i = args.firstIndex(of: "--scale"), i + 1 < args.count,
       let s = Double(args[i + 1]) { scale = s }

    let cfg = SCStreamConfiguration()
    cfg.width = Int(win.frame.width * scale)
    cfg.height = Int(win.frame.height * scale)
    cfg.pixelFormat = kCVPixelFormatType_32BGRA
    cfg.minimumFrameInterval = CMTime(value: 1, timescale: 60)
    // Shallow queue on purpose. A deep queue buys smoothness by adding latency,
    // and every buffered frame is extra observation staleness the planner cannot
    // see — CLAUDE.md invariant 8. Same reasoning as capture.py's single slot.
    cfg.queueDepth = 3
    cfg.showsCursor = false

    let filter = SCContentFilter(desktopIndependentWindow: win)
    let stream = SCStream(filter: filter, configuration: cfg, delegate: nil)
    let sink = Out()
    do {
        try stream.addStreamOutput(sink, type: .screen,
                                   sampleHandlerQueue: DispatchQueue(label: "wincap.frames"))
        try await stream.startCapture()
    } catch {
        err("startCapture failed: \(error.localizedDescription)")
        exit(3)
    }
}

Task { await run() }
dispatchMain()
