import Foundation
import AVFoundation
import CoreAudio
import AudioToolbox
#if canImport(Darwin)
import Darwin
#endif

public enum SystemAudioCaptureMethod: String, Codable {
    /// Real native system-output capture via CoreAudio Process Taps / Aggregate Device
    /// (macOS 14.4+ `AudioHardwareCreateProcessTap` / `AudioHardwareCreateAggregateDevice`).
    case coreAudioTap
    /// Best-effort fallback: records from a named input device (e.g. BlackHole,
    /// or any user-configured Aggregate Device) using the standard mic-recording path.
    case namedInputDeviceFallback
}

public struct NativeTapAvailability: Equatable, Codable {
    public let osVersionSupported: Bool
    public let createProcessTapSymbolAvailable: Bool
    public let destroyProcessTapSymbolAvailable: Bool
    public let createAggregateDeviceSymbolAvailable: Bool

    public init(osVersionSupported: Bool,
                createProcessTapSymbolAvailable: Bool,
                destroyProcessTapSymbolAvailable: Bool = true,
                createAggregateDeviceSymbolAvailable: Bool) {
        self.osVersionSupported = osVersionSupported
        self.createProcessTapSymbolAvailable = createProcessTapSymbolAvailable
        self.destroyProcessTapSymbolAvailable = destroyProcessTapSymbolAvailable
        self.createAggregateDeviceSymbolAvailable = createAggregateDeviceSymbolAvailable
    }

    public var isPotentiallyAvailable: Bool {
        osVersionSupported && createProcessTapSymbolAvailable && destroyProcessTapSymbolAvailable && createAggregateDeviceSymbolAvailable
    }

    public var summary: String {
        if isPotentiallyAvailable {
            return "CoreAudio process-tap symbols appear available, but capture wiring is not implemented yet."
        }
        var missing: [String] = []
        if !osVersionSupported { missing.append("macOS 14.4+") }
        if !createProcessTapSymbolAvailable { missing.append("AudioHardwareCreateProcessTap") }
        if !destroyProcessTapSymbolAvailable { missing.append("AudioHardwareDestroyProcessTap") }
        if !createAggregateDeviceSymbolAvailable { missing.append("AudioHardwareCreateAggregateDevice") }
        return "CoreAudio process-tap unavailable or unsupported in this SDK/runtime (missing: \(missing.joined(separator: ", ")))."
    }
}

public struct NativeTapDiagnostic: Equatable, Codable {
    public let availability: NativeTapAvailability
    public let createDestroyAttempted: Bool
    public let createDestroySucceeded: Bool?
    public let createOSStatus: Int32?
    public let destroyOSStatus: Int32?
    public let tapID: UInt32?

    public init(availability: NativeTapAvailability,
                createDestroyAttempted: Bool,
                createDestroySucceeded: Bool? = nil,
                createOSStatus: Int32? = nil,
                destroyOSStatus: Int32? = nil,
                tapID: UInt32? = nil) {
        self.availability = availability
        self.createDestroyAttempted = createDestroyAttempted
        self.createDestroySucceeded = createDestroySucceeded
        self.createOSStatus = createOSStatus
        self.destroyOSStatus = destroyOSStatus
        self.tapID = tapID
    }

    public var summary: String {
        guard availability.isPotentiallyAvailable else { return availability.summary }
        guard createDestroyAttempted else {
            return "CoreAudio process-tap symbols appear available. Create/destroy diagnostic not run; set STT_NATIVE_TAP_DIAGNOSTIC=1 to attempt it."
        }
        if createDestroySucceeded == true {
            return "CoreAudio process-tap create/destroy diagnostic succeeded. Capture wiring is still not implemented yet."
        }
        let create = createOSStatus.map(String.init) ?? "not attempted"
        let destroy = destroyOSStatus.map(String.init) ?? "not attempted"
        return "CoreAudio process-tap create/destroy diagnostic failed (create OSStatus: \(create), destroy OSStatus: \(destroy))."
    }
}

public enum SystemAudioRecorderError: Error, LocalizedError {
    case tapUnavailable(String)
    case nativeCaptureFailed(String)
    case noFallbackDeviceConfigured

    public var errorDescription: String? {
        switch self {
        case .tapUnavailable(let reason):
            return "Native system-audio capture unavailable: \(reason)"
        case .nativeCaptureFailed(let reason):
            return "Native system-audio capture failed: \(reason)"
        case .noFallbackDeviceConfigured:
            return Self.fallbackConfigurationGuidance()
        }
    }

    public static func fallbackConfigurationGuidance() -> String {
        """
        No fallback input device configured or found for system-audio capture.
        Native CoreAudio process-tap capture could not start, so system mode needs a routed virtual/aggregate input device fallback.
        Try:
          1. Install/configure BlackHole or an Aggregate Device.
          2. Confirm it appears in `stt devices`.
          3. Record with: stt record --mode system --input-device "BlackHole 2ch" --fail-if-empty --output system.wav
          4. Validate routed audio with: STT_SYSTEM_DEVICE="BlackHole 2ch" ./scripts/validate.sh
        """
    }
}

/// Captures macOS system/output audio.
///
/// STATUS (read this before relying on `.coreAudioTap`):
///
/// - IMPLEMENTED: `.namedInputDeviceFallback` — records system audio by
///   treating a virtual/aggregate CoreAudio input device (BlackHole,
///   "Aggregate Device", a Multi-Output+BlackHole setup, etc.) as a normal
///   microphone input via `MicRecorder`. This is fully functional today and
///   is what `stt record --mode system` uses unless a native tap is proven
///   available and working.
///
/// - STUBBED / BEST-EFFORT: `.coreAudioTap` — real system-output capture via
///   the macOS 14.4+ Core Audio "process tap" APIs
///   (`AudioHardwareCreateProcessTap`, `AudioHardwareCreateAggregateDevice`
///   with a tap description, `kAudioSubTapUIDKey`, etc.). These APIs exist as
///   C functions in CoreAudio/AudioToolbox but (as of this writing) largely
///   lack public Swift-friendly headers/signatures in the standard macOS SDK
///   shipped with swift-argument-parser-era toolchains, and require careful
///   CFDictionary-based aggregate-device descriptions plus IOProc callback
///   wiring that is materially riskier to get right than the fallback path.
///   `probeNativeTapAvailability()` below performs a conservative capability
///   check (macOS version + presence of the runtime symbols) and reports
///   whether the tap path *could* be attempted, but `startNativeTap()`
///   currently always fails fast with `.tapUnavailable`, deferring the full
///   implementation to a follow-up iteration. Every caller must treat this
///   as a soft failure and fall back to `.namedInputDeviceFallback`.
public final class SystemAudioRecorder {
    private let micRecorder = MicRecorder()
    private var nativeLifecycle: NativeTapLifecycle?
    private var nativeBridge: NativeTapWAVBridge?
    private var nativeStartTime: Date?
    private var nativeOutputURL: URL?
    public private(set) var activeMethod: SystemAudioCaptureMethod?

    public init() {}

    /// Returns true if this macOS version is new enough to plausibly support
    /// CoreAudio process taps (14.4+). This is a necessary, not sufficient,
    /// condition — see the type-level doc comment for why the tap path is
    /// still considered best-effort/stubbed.
    public static func isNativeTapOSVersionSupported() -> Bool {
        let version = ProcessInfo.processInfo.operatingSystemVersion
        if version.majorVersion > 14 { return true }
        if version.majorVersion == 14 && version.minorVersion >= 4 { return true }
        return false
    }

    /// Conservative runtime probe for the native CoreAudio process-tap path.
    /// This does not attempt capture; it only checks whether the OS is new
    /// enough and whether the expected CoreAudio symbols are visible.
    public static func probeNativeTapAvailability() -> NativeTapAvailability {
        NativeTapAvailability(
            osVersionSupported: isNativeTapOSVersionSupported(),
            createProcessTapSymbolAvailable: coreAudioSymbolAvailable("AudioHardwareCreateProcessTap"),
            destroyProcessTapSymbolAvailable: coreAudioSymbolAvailable("AudioHardwareDestroyProcessTap"),
            createAggregateDeviceSymbolAvailable: coreAudioSymbolAvailable("AudioHardwareCreateAggregateDevice")
        )
    }

    /// Opt-in diagnostic that actually creates and destroys a private global
    /// process tap. This may trigger macOS permission/TCC behavior depending
    /// on OS version, bundle attribution, and entitlement state, so callers
    /// should keep it behind an explicit user/env gate.
    public static func probeNativeTapDiagnostic(attemptCreateDestroy: Bool = false) -> NativeTapDiagnostic {
        let availability = probeNativeTapAvailability()
        guard availability.isPotentiallyAvailable else {
            return NativeTapDiagnostic(availability: availability, createDestroyAttempted: false)
        }
        guard attemptCreateDestroy else {
            return NativeTapDiagnostic(availability: availability, createDestroyAttempted: false)
        }

        if #available(macOS 14.2, *) {
            let description = NativeTapLifecycle.defaultTapDescription(name: "stt native tap diagnostic")
            do {
                let tapResource = try NativeProcessTapResource(description: description)
                let tapID = tapResource.tapID
                let destroyStatus = tapResource.invalidate() ?? noErr
                return NativeTapDiagnostic(
                    availability: availability,
                    createDestroyAttempted: true,
                    createDestroySucceeded: destroyStatus == noErr,
                    createOSStatus: noErr,
                    destroyOSStatus: destroyStatus,
                    tapID: tapID
                )
            } catch let error as NativeTapLifecycleError {
                let createStatus: OSStatus
                if case .createProcessTapFailed(let status) = error {
                    createStatus = status
                } else {
                    createStatus = -1
                }
                return NativeTapDiagnostic(
                    availability: availability,
                    createDestroyAttempted: true,
                    createDestroySucceeded: false,
                    createOSStatus: createStatus,
                    destroyOSStatus: nil,
                    tapID: nil
                )
            } catch {
                return NativeTapDiagnostic(
                    availability: availability,
                    createDestroyAttempted: true,
                    createDestroySucceeded: false,
                    createOSStatus: -1,
                    destroyOSStatus: nil,
                    tapID: nil
                )
            }
        }

        return NativeTapDiagnostic(
            availability: availability,
            createDestroyAttempted: true,
            createDestroySucceeded: false,
            createOSStatus: nil,
            destroyOSStatus: nil,
            tapID: nil
        )
    }

    private static func coreAudioSymbolAvailable(_ symbol: String) -> Bool {
        #if canImport(Darwin)
        let path = "/System/Library/Frameworks/CoreAudio.framework/CoreAudio"
        guard let handle = dlopen(path, RTLD_LAZY) else { return false }
        defer { dlclose(handle) }
        return dlsym(handle, symbol) != nil
        #else
        return false
        #endif
    }

    /// Attempts native CoreAudio process-tap capture. On success, CoreAudio
    /// callbacks append Float32 tap buffers as interleaved 16-bit PCM WAV data
    /// through `NativeTapWAVBridge`. On any setup failure, all partial native
    /// resources are cleaned up and the caller falls back to named input-device
    /// capture.
    private func startNativeTap(outputURL: URL) throws {
        let availability = Self.probeNativeTapAvailability()
        guard availability.isPotentiallyAvailable else {
            throw SystemAudioRecorderError.tapUnavailable(availability.summary)
        }

        try Paths.ensureDirectoryExists(outputURL.deletingLastPathComponent())

        let bridge: NativeTapWAVBridge
        do {
            bridge = try NativeTapWAVBridge(outputURL: outputURL, sampleRate: 48_000, channels: 2)
        } catch {
            throw SystemAudioRecorderError.nativeCaptureFailed("could not create WAV writer at \(outputURL.path): \(error.localizedDescription)")
        }

        let lifecycle = NativeTapLifecycle(
            tapDescription: NativeTapLifecycle.defaultTapDescription(name: "stt native system-audio tap"),
            audioBridge: bridge
        )

        do {
            try lifecycle.start()
        } catch {
            _ = try? bridge.finish()
            try? FileManager.default.removeItem(at: outputURL)
            throw SystemAudioRecorderError.nativeCaptureFailed(error.localizedDescription)
        }

        nativeBridge = bridge
        nativeLifecycle = lifecycle
        nativeStartTime = Date()
        nativeOutputURL = outputURL
    }

    /// Starts system-audio capture, preferring the native tap and silently
    /// (but visibly, via the returned method + printed guidance from the
    /// caller) falling back to a named virtual input device.
    ///
    /// - Parameter fallbackDeviceName: name (or substring) of the virtual
    ///   input device to use when native capture isn't available, e.g.
    ///   "BlackHole 2ch" or "Aggregate Device". If nil, common default names
    ///   are tried.
    public func start(outputURL: URL, fallbackDeviceName: String? = nil) throws -> SystemAudioCaptureMethod {
        do {
            try startNativeTap(outputURL: outputURL)
            activeMethod = .coreAudioTap
            return .coreAudioTap
        } catch {
            let device = try Self.resolveFallbackDevice(named: fallbackDeviceName)
            try micRecorder.start(outputURL: outputURL, inputDeviceID: device.id)
            activeMethod = .namedInputDeviceFallback
            return .namedInputDeviceFallback
        }
    }

    @discardableResult
    public func stop() throws -> RecordingResult {
        defer { activeMethod = nil }
        switch activeMethod {
        case .coreAudioTap:
            return try stopNativeTap()
        case .namedInputDeviceFallback:
            return try micRecorder.stop()
        case nil:
            if micRecorder.isRunning { return try micRecorder.stop() }
            throw MicRecorderError.notRecording
        }
    }

    public var isRunning: Bool { micRecorder.isRunning || nativeLifecycle?.isStarted == true }

    private func stopNativeTap() throws -> RecordingResult {
        guard let lifecycle = nativeLifecycle,
              let bridge = nativeBridge,
              let startTime = nativeStartTime,
              let outputURL = nativeOutputURL else {
            throw MicRecorderError.notRecording
        }

        var lifecycleError: Error?
        do {
            _ = try lifecycle.stop()
        } catch {
            lifecycleError = error
        }

        let finalURL = try bridge.finish()
        let duration = Date().timeIntervalSince(startTime)

        nativeLifecycle = nil
        nativeBridge = nil
        nativeStartTime = nil
        nativeOutputURL = nil

        if let lifecycleError {
            throw SystemAudioRecorderError.nativeCaptureFailed(lifecycleError.localizedDescription)
        }

        return RecordingResult(
            outputURL: finalURL,
            durationSeconds: duration,
            fileSizeBytes: Self.fileSizeBytes(for: outputURL)
        )
    }

    private static func fileSizeBytes(for url: URL) -> UInt64? {
        guard let size = try? FileManager.default.attributesOfItem(atPath: url.path)[.size] else {
            return nil
        }
        if let number = size as? NSNumber { return number.uint64Value }
        if let uint64 = size as? UInt64 { return uint64 }
        if let int = size as? Int { return UInt64(int) }
        return nil
    }

    /// Common virtual-device names to probe when the caller doesn't specify one.
    public static let commonFallbackDeviceNames = ["BlackHole 2ch", "BlackHole", "Aggregate Device"]

    public static func selectFallbackDevice(named name: String?,
                                            from devices: [AudioDeviceInfo],
                                            candidateNames: [String] = commonFallbackDeviceNames) throws -> AudioDeviceInfo {
        if let name {
            return try DeviceList.selectInputDevice(named: name, from: devices)
        }
        for candidate in candidateNames {
            if let device = try? DeviceList.selectInputDevice(named: candidate, from: devices) {
                return device
            }
        }
        throw SystemAudioRecorderError.noFallbackDeviceConfigured
    }

    private static func resolveFallbackDevice(named name: String?) throws -> AudioDeviceInfo {
        try selectFallbackDevice(named: name, from: DeviceList.inputDevices())
    }
}
