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
    public let createAggregateDeviceSymbolAvailable: Bool

    public init(osVersionSupported: Bool,
                createProcessTapSymbolAvailable: Bool,
                createAggregateDeviceSymbolAvailable: Bool) {
        self.osVersionSupported = osVersionSupported
        self.createProcessTapSymbolAvailable = createProcessTapSymbolAvailable
        self.createAggregateDeviceSymbolAvailable = createAggregateDeviceSymbolAvailable
    }

    public var isPotentiallyAvailable: Bool {
        osVersionSupported && createProcessTapSymbolAvailable && createAggregateDeviceSymbolAvailable
    }

    public var summary: String {
        if isPotentiallyAvailable {
            return "CoreAudio process-tap symbols appear available, but capture wiring is not implemented yet."
        }
        var missing: [String] = []
        if !osVersionSupported { missing.append("macOS 14.4+") }
        if !createProcessTapSymbolAvailable { missing.append("AudioHardwareCreateProcessTap") }
        if !createAggregateDeviceSymbolAvailable { missing.append("AudioHardwareCreateAggregateDevice") }
        return "CoreAudio process-tap unavailable or unsupported in this SDK/runtime (missing: \(missing.joined(separator: ", ")))."
    }
}

public enum SystemAudioRecorderError: Error, LocalizedError {
    case tapUnavailable(String)
    case noFallbackDeviceConfigured

    public var errorDescription: String? {
        switch self {
        case .tapUnavailable(let reason):
            return "Native system-audio capture unavailable: \(reason)"
        case .noFallbackDeviceConfigured:
            return Self.fallbackConfigurationGuidance()
        }
    }

    public static func fallbackConfigurationGuidance() -> String {
        """
        No fallback input device configured or found for system-audio capture.
        Native CoreAudio process-tap capture is not wired yet, so system mode currently needs a routed virtual/aggregate input device.
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
            createAggregateDeviceSymbolAvailable: coreAudioSymbolAvailable("AudioHardwareCreateAggregateDevice")
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

    /// Attempts native CoreAudio process-tap capture. Currently always
    /// throws `.tapUnavailable` — see type-level documentation. Kept as a
    /// distinct entry point so a future implementation can slot in without
    /// changing call sites in `record(...)`.
    private func startNativeTap(outputURL: URL) throws {
        let availability = Self.probeNativeTapAvailability()
        guard availability.isPotentiallyAvailable else {
            throw SystemAudioRecorderError.tapUnavailable(availability.summary)
        }
        // NOT IMPLEMENTED: AudioHardwareCreateProcessTap / AudioHardwareCreateAggregateDevice
        // wiring. See type-level doc comment for rationale and follow-up plan.
        throw SystemAudioRecorderError.tapUnavailable(
            "CoreAudio process-tap capture symbols are available, but capture wiring is not yet implemented in this build; " +
            "falling back to named-input-device capture (e.g. BlackHole/Aggregate Device)."
        )
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
        return try micRecorder.stop()
    }

    public var isRunning: Bool { micRecorder.isRunning }

    /// Common virtual-device names to probe when the caller doesn't specify one.
    private static let commonFallbackDeviceNames = ["BlackHole 2ch", "BlackHole", "Aggregate Device"]

    private static func resolveFallbackDevice(named name: String?) throws -> AudioDeviceInfo {
        if let name {
            return try DeviceList.resolveInputDevice(named: name)
        }
        for candidate in commonFallbackDeviceNames {
            if let device = try? DeviceList.resolveInputDevice(named: candidate) {
                return device
            }
        }
        throw SystemAudioRecorderError.noFallbackDeviceConfigured
    }
}
