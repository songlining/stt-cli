import Foundation
import AVFoundation
#if canImport(CoreGraphics)
import CoreGraphics
#endif

public enum PermissionStatus: String, Codable {
    case authorized
    case denied
    case restricted
    case notDetermined
    case unknown
}

/// Wraps AVFoundation microphone authorization plus a best-effort
/// screen-recording permission check (relevant only if/when the
/// ScreenCaptureKit fallback for system audio is used instead of CATap).
public enum AudioPermissions {

    /// Current microphone authorization status without prompting the user.
    public static func microphoneStatus() -> PermissionStatus {
        let status = AVCaptureDevice.authorizationStatus(for: .audio)
        return map(status)
    }

    /// Requests microphone access, prompting the user if status is
    /// `.notDetermined`. Completion is called on an arbitrary queue.
    public static func requestMicrophoneAccess(completion: @escaping @Sendable (Bool) -> Void) {
        AVCaptureDevice.requestAccess(for: .audio, completionHandler: completion)
    }

    /// Async convenience wrapper around `requestMicrophoneAccess`.
    public static func requestMicrophoneAccessAsync() async -> Bool {
        await withCheckedContinuation { continuation in
            requestMicrophoneAccess { granted in
                continuation.resume(returning: granted)
            }
        }
    }

    /// Best-effort screen-recording permission probe. Only meaningful if the
    /// ScreenCaptureKit fallback path for system audio is exercised; the
    /// primary/preferred CoreAudio-tap and named-input-device paths do not
    /// require this permission.
    public static func screenRecordingStatus() -> PermissionStatus {
        #if canImport(CoreGraphics)
        if CGPreflightScreenCaptureAccess() {
            return .authorized
        }
        return .denied
        #else
        return .unknown
        #endif
    }

    private static func map(_ status: AVAuthorizationStatus) -> PermissionStatus {
        switch status {
        case .authorized: return .authorized
        case .denied: return .denied
        case .restricted: return .restricted
        case .notDetermined: return .notDetermined
        @unknown default: return .unknown
        }
    }

    /// Human-readable guidance for recovering from a denied permission,
    /// including the relevant `tccutil reset` invocation.
    public static func microphoneResetGuidance(bundleID: String) -> String {
        """
        Microphone access is currently denied for this tool.

        To reset and re-prompt:
          1. Run: tccutil reset Microphone \(bundleID)
          2. Re-run the failing `stt` command; macOS should prompt again.
          3. Or manually enable it: System Settings > Privacy & Security > Microphone > \(bundleID)
        """
    }

    public static func screenRecordingResetGuidance(bundleID: String) -> String {
        """
        Screen Recording access (only needed for the ScreenCaptureKit fallback
        for system-audio capture) is currently denied.

        To reset and re-prompt:
          1. Run: tccutil reset ScreenCapture \(bundleID)
          2. Re-run the failing `stt` command; macOS should prompt again.
          3. Or manually enable it: System Settings > Privacy & Security > Screen Recording > \(bundleID)
        """
    }

    public static func audioCaptureResetGuidance(bundleID: String) -> String {
        """
        Audio Capture access (used by the CoreAudio process-tap path for
        native system-audio capture) may be denied or unavailable.

        To reset and re-prompt:
          1. Run: tccutil reset AudioCapture \(bundleID)
          2. Re-run the failing `stt` command; macOS should prompt again.
        """
    }
}
