import Foundation

public enum MeetingRecorderError: Error, LocalizedError {
    case micStartFailed(Error)
    case systemStartFailed(Error)

    public var errorDescription: String? {
        switch self {
        case .micStartFailed(let error): return "Failed to start mic capture: \(error.localizedDescription)"
        case .systemStartFailed(let error): return "Failed to start system-audio capture: \(error.localizedDescription)"
        }
    }
}

public struct MeetingRecordingResult {
    public let micResult: RecordingResult?
    public let systemResult: RecordingResult?
    public let systemCaptureMethod: SystemAudioCaptureMethod?
}

/// Combines microphone + system-audio capture for "meeting" mode.
///
/// IMPLEMENTED: separate-track capture. Mic and system audio are recorded to
/// two independent WAV files via `MicRecorder` and `SystemAudioRecorder`
/// (the latter falling back to a named virtual input device unless/until
/// native CoreAudio taps are implemented — see `SystemAudioRecorder`). This
/// is the recommended mode for downstream diarization, per the project's
/// architecture doc.
///
/// BEST-EFFORT: single-file "mixed" output. True sample-accurate mixing of
/// two independently-clocked AVAudioEngine/CoreAudio streams into one
/// interleaved WAV would require a shared clock or an offline mixer pass
/// after capture. This recorder intentionally records both tracks separately
/// (to avoid losing audio) and reports both output paths. CLI callers such as
/// `record --mode meeting` and `pipeline --mode meeting` may then attempt a
/// post-capture `WAVMixer` mix-down when both files are compatible PCM WAVs.
public final class MeetingRecorder {
    private let micRecorder = MicRecorder()
    private let systemRecorder = SystemAudioRecorder()

    public init() {}

    /// Starts both mic and system-audio capture. On any failure, whatever
    /// was already started is stopped before rethrowing.
    public func start(micOutputURL: URL, systemOutputURL: URL, fallbackDeviceName: String? = nil) throws -> SystemAudioCaptureMethod {
        do {
            try micRecorder.start(outputURL: micOutputURL)
        } catch {
            throw MeetingRecorderError.micStartFailed(error)
        }

        do {
            let method = try systemRecorder.start(outputURL: systemOutputURL, fallbackDeviceName: fallbackDeviceName)
            return method
        } catch {
            _ = try? micRecorder.stop()
            throw MeetingRecorderError.systemStartFailed(error)
        }
    }

    @discardableResult
    public func stop() throws -> MeetingRecordingResult {
        let method = systemRecorder.activeMethod
        let micResult = try? micRecorder.stop()
        let systemResult = try? systemRecorder.stop()
        return MeetingRecordingResult(
            micResult: micResult,
            systemResult: systemResult,
            systemCaptureMethod: method
        )
    }

    public var isRunning: Bool { micRecorder.isRunning || systemRecorder.isRunning }
}
