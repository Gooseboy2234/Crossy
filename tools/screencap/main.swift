//  screencap — stream a USB-tethered iPhone's screen as rawvideo on stdout.
//
//  WHY THIS EXISTS
//
//  capture.py's docstring is right that a tethered iPhone appears to macOS as an
//  AVCaptureDevice — that is what QuickTime's "Movie Recording -> iPhone" uses.
//  What it misses is that those devices are HIDDEN from enumeration until some
//  process sets kCMIOHardwarePropertyAllowScreenCaptureDevices on the CoreMediaIO
//  system object. QuickTime sets it. ffmpeg does not, and has no flag to. So
//  `ffmpeg -f avfoundation -list_devices` shows the phone's Continuity cameras
//  and never its screen, which is why find_iphone_index() latched onto the Desk
//  View Camera instead.
//
//  Setting that property is the whole trick. Everything after it is a stock
//  AVCaptureSession.
//
//  Output is raw BGRA frames, no container and no header, straight to stdout —
//  the same shape capture.py already expects from ffmpeg's rawvideo, so the
//  reader thread does not care which one is upstream.
//
//  Build:  swiftc -O tools/screencap/main.swift -o tools/screencap/screencap
//  List:   ./screencap --list
//  Stream: ./screencap --device-uid <uid>   (or omit to take the first screen device)

import AVFoundation
import CoreMediaIO
import Foundation

/// Unhide iOS screen-capture devices. Must run BEFORE any discovery call —
/// AVFoundation caches its device list, so ordering here is not cosmetic.
func allowScreenCaptureDevices() {
    var address = CMIOObjectPropertyAddress(
        mSelector: CMIOObjectPropertySelector(kCMIOHardwarePropertyAllowScreenCaptureDevices),
        mScope: CMIOObjectPropertyScope(kCMIOObjectPropertyScopeGlobal),
        mElement: CMIOObjectPropertyElement(kCMIOObjectPropertyElementMain)
    )
    var allow: UInt32 = 1
    let status = CMIOObjectSetPropertyData(
        CMIOObjectID(kCMIOObjectSystemObject), &address, 0, nil,
        UInt32(MemoryLayout<UInt32>.size), &allow
    )
    if status != OSStatus(kCMIOHardwareNoError) {
        FileHandle.standardError.write("warn: CMIOObjectSetPropertyData -> \(status)\n".data(using: .utf8)!)
    }
}

func discover() -> [AVCaptureDevice] {
    var types: [AVCaptureDevice.DeviceType] = [.external]
    if #available(macOS 14.0, *) {} else { types = [.externalUnknown] }
    return AVCaptureDevice.DiscoverySession(
        deviceTypes: types, mediaType: .muxed, position: .unspecified
    ).devices + AVCaptureDevice.DiscoverySession(
        deviceTypes: types, mediaType: .video, position: .unspecified
    ).devices
}

/// Exhaustive dump for diagnosis. If the phone's screen is exposed at all it
/// shows up here with uniqueID == the device UDID; anything whose uniqueID is
/// a Continuity-style GUID is a camera, not the screen.
func discoverAll() -> [(AVCaptureDevice, String)] {
    var types: [AVCaptureDevice.DeviceType] = [.builtInWideAngleCamera, .external]
    if #available(macOS 14.0, *) { types.append(.continuityCamera) }
    var seen = Set<String>()
    var out: [(AVCaptureDevice, String)] = []
    for mt in [AVMediaType.muxed, .video] {
        for d in AVCaptureDevice.DiscoverySession(
            deviceTypes: types, mediaType: mt, position: .unspecified).devices {
            if seen.insert(d.uniqueID + mt.rawValue).inserted { out.append((d, mt.rawValue)) }
        }
    }
    return out
}

final class Streamer: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    let session = AVCaptureSession()
    private let out = FileHandle.standardOutput
    private var announced = false

    func start(device: AVCaptureDevice) throws {
        session.beginConfiguration()
        let input = try AVCaptureDeviceInput(device: device)
        guard session.canAddInput(input) else { throw Err("cannot add input") }
        session.addInput(input)

        let video = AVCaptureVideoDataOutput()
        video.videoSettings = [kCVPixelBufferPixelFormatTypeKey as String:
                                 Int(kCVPixelFormatType_32BGRA)]
        // Dropping late frames is correct here for the same reason capture.py
        // keeps a single slot: a stale frame is worse than no frame.
        video.alwaysDiscardsLateVideoFrames = true
        video.setSampleBufferDelegate(self, queue: DispatchQueue(label: "screencap.frames"))
        guard session.canAddOutput(video) else { throw Err("cannot add output") }
        session.addOutput(video)
        session.commitConfiguration()
        session.startRunning()
    }

    func captureOutput(_ o: AVCaptureOutput, didOutput sb: CMSampleBuffer,
                       from c: AVCaptureConnection) {
        guard let pb = CMSampleBufferGetImageBuffer(sb) else { return }
        CVPixelBufferLockBaseAddress(pb, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(pb, .readOnly) }

        let w = CVPixelBufferGetWidth(pb)
        let h = CVPixelBufferGetHeight(pb)
        let stride = CVPixelBufferGetBytesPerRow(pb)
        guard let base = CVPixelBufferGetBaseAddress(pb) else { return }

        if !announced {
            // Dimensions go to stderr so stdout stays a pure frame stream. The
            // reader needs w/h to reshape and cannot get them from the bytes.
            FileHandle.standardError.write("screencap: \(w)x\(h) BGRA\n".data(using: .utf8)!)
            announced = true
        }

        // Copy row by row: CVPixelBuffer rows are padded to an alignment that is
        // almost never w*4, and blitting the whole plane ships the padding too,
        // which shears the image in a way that looks exactly like a CV bug.
        var frame = Data(capacity: w * h * 4)
        for row in 0..<h {
            frame.append(Data(bytes: base.advanced(by: row * stride), count: w * 4))
        }
        out.write(frame)
    }

    struct Err: LocalizedError { let m: String; init(_ m: String) { self.m = m }
                                var errorDescription: String? { m } }
}

// MARK: - main

allowScreenCaptureDevices()
let args = CommandLine.arguments
let devices = discover()

if args.contains("--list") {
    // AVFoundation caches aggressively; give it a beat to notice the property
    // change before concluding the screen device does not exist.
    Thread.sleep(forTimeInterval: 1.5)
    let all = discoverAll()
    for (d, mt) in all {
        print("\(d.uniqueID)\t\(d.localizedName)\t[\(d.deviceType.rawValue)]\tmedia=\(mt)")
    }
    exit(all.isEmpty ? 1 : 0)
}

var chosen: AVCaptureDevice?
if let i = args.firstIndex(of: "--device-uid"), i + 1 < args.count {
    chosen = devices.first { $0.uniqueID == args[i + 1] }
} else {
    chosen = devices.first
}

guard let device = chosen else {
    FileHandle.standardError.write("no screen-capture device — is the phone plugged in and unlocked?\n".data(using: .utf8)!)
    exit(1)
}

let streamer = Streamer()
do {
    try streamer.start(device: device)
} catch {
    FileHandle.standardError.write("start failed: \(error.localizedDescription)\n".data(using: .utf8)!)
    exit(1)
}
RunLoop.main.run()
