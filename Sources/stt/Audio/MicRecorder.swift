import Foundation
import AVFoundation
import AudioToolbox

public enum MicRecorderError: Error, LocalizedError {
    case alreadyRecording
    case notRecording
    case engineStartFailed(String)
    case fileCreationFailed(String)

    public var errorDescription: String? {
        switch self {
        case .alreadyRecording: return "Recorder is already running"
        case .notRecording: return "Recorder is not currently running"
        case .engineStartFailed(let reason): return "Failed to start audio engine: \(reason)"
        case .fileCreationFailed(let path): return "Failed to create output file at \(path)"
        }
    }
}

public struct RecordingResult {
    public let outputURL: URL
    public let durationSeconds: Double
    public let fileSizeBytes: UInt64?

    public init(outputURL: URL, durationSeconds: Double, fileSizeBytes: UInt64? = nil) {
        self.outputURL = outputURL
        self.durationSeconds = durationSeconds
        self.fileSizeBytes = fileSizeBytes
    }

    /// AVAudioFile may leave a small header-only file (often 4096 bytes) when
    /// no audio frames are captured. This is a heuristic smoke-test signal,
    /// not a substitute for decoding the file.
    public var likelyContainsAudioData: Bool {
        guard let fileSizeBytes else { return true }
        return fileSizeBytes > 4096
    }

    public var emptyAudioWarning: String? {
        guard !likelyContainsAudioData else { return nil }
        return "WARNING: output file is very small (\(fileSizeBytes ?? 0) bytes); no audio frames may have been captured. Check permissions, selected device, and audio routing."
    }
}

/// AVAudioEngine-based microphone recorder that writes PCM straight to a WAV
/// file via AVAudioFile. Supports a signal-safe `stop()` so it can be driven
/// from a SIGINT handler.
public final class MicRecorder {
    private let engine = AVAudioEngine()
    private var audioFile: AVAudioFile?
    private var audioRecorder: AVAudioRecorder?
    private var isTapInstalled = false
    private let lock = NSLock()
    private var startTime: Date?
    private(set) var outputURL: URL?

    public init() {}

    /// Starts recording from the given input node. When `inputDeviceID` is
    /// provided, the engine's input audio unit is pointed at that specific
    /// CoreAudio device (e.g. a named microphone, or a virtual/aggregate
    /// device such as BlackHole used as the system-audio fallback path)
    /// instead of the system default input.
    public func start(outputURL: URL, inputDeviceID: UInt32? = nil) throws {
        lock.lock()
        defer { lock.unlock() }

        guard audioFile == nil, audioRecorder == nil else { throw MicRecorderError.alreadyRecording }

        try Paths.ensureDirectoryExists(outputURL.deletingLastPathComponent())

        if inputDeviceID == nil {
            try startDefaultInputRecorder(outputURL: outputURL)
            return
        }

        let inputNode = engine.inputNode

        if let inputDeviceID {
            try Self.setInputDevice(inputDeviceID, on: inputNode)
        }
        let inputFormat = inputNode.outputFormat(forBus: 0)

        // Device-specific capture still uses AVAudioEngine so CoreAudio input
        // devices such as BlackHole/Aggregate Device can be selected. Use the
        // original PCM file settings here: this path is primarily a fallback
        // for virtual devices and will be replaced by a dedicated native
        // system-audio implementation.
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVSampleRateKey: inputFormat.sampleRate,
            AVNumberOfChannelsKey: inputFormat.channelCount,
            AVLinearPCMBitDepthKey: 16,
            AVLinearPCMIsFloatKey: false,
            AVLinearPCMIsBigEndianKey: false
        ]

        let file: AVAudioFile
        do {
            file = try AVAudioFile(forWriting: outputURL, settings: settings)
        } catch {
            throw MicRecorderError.fileCreationFailed(outputURL.path)
        }

        self.audioFile = file
        self.outputURL = outputURL
        self.startTime = Date()

        inputNode.installTap(onBus: 0, bufferSize: 4096, format: inputFormat) { [weak self] buffer, _ in
            guard let self else { return }
            try? self.audioFile?.write(from: buffer)
        }
        isTapInstalled = true

        do {
            engine.prepare()
            try engine.start()
        } catch {
            inputNode.removeTap(onBus: 0)
            isTapInstalled = false
            throw MicRecorderError.engineStartFailed(error.localizedDescription)
        }

        // State was set before installing the tap so the callback can write
        // immediately after the engine starts.
    }

    /// Stops the engine, removes the tap, and closes out the WAV file.
    /// Safe to call from a signal handler context (only touches simple state
    /// and AVFoundation APIs that are safe to call once).
    @discardableResult
    public func stop() throws -> RecordingResult {
        lock.lock()
        defer { lock.unlock() }

        guard let outputURL, let startTime else {
            throw MicRecorderError.notRecording
        }

        if let audioRecorder {
            audioRecorder.stop()
            self.audioRecorder = nil
        }
        if engine.isRunning {
            engine.stop()
        }
        if isTapInstalled {
            engine.inputNode.removeTap(onBus: 0)
            isTapInstalled = false
        }
        audioFile = nil

        let duration = Date().timeIntervalSince(startTime)
        self.outputURL = nil
        self.startTime = nil

        return RecordingResult(
            outputURL: outputURL,
            durationSeconds: duration,
            fileSizeBytes: Self.fileSizeBytes(for: outputURL)
        )
    }

    public var isRunning: Bool {
        lock.lock()
        defer { lock.unlock() }
        return audioFile != nil || audioRecorder != nil
    }

    private func startDefaultInputRecorder(outputURL: URL) throws {
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVSampleRateKey: 44_100.0,
            AVNumberOfChannelsKey: 1,
            AVLinearPCMBitDepthKey: 16,
            AVLinearPCMIsFloatKey: false,
            AVLinearPCMIsBigEndianKey: false
        ]

        let recorder: AVAudioRecorder
        do {
            recorder = try AVAudioRecorder(url: outputURL, settings: settings)
        } catch {
            throw MicRecorderError.fileCreationFailed(outputURL.path)
        }

        guard recorder.prepareToRecord(), recorder.record() else {
            throw MicRecorderError.engineStartFailed("AVAudioRecorder failed to start")
        }

        self.audioRecorder = recorder
        self.outputURL = outputURL
        self.startTime = Date()
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

    /// Points the given AVAudioInputNode's underlying audio unit at a
    /// specific CoreAudio device ID, using the `kAudioOutputUnitProperty_CurrentDevice`
    /// AudioUnit property. Best-effort: on failure the engine falls back to
    /// whatever the current system default input device is.
    private static func setInputDevice(_ deviceID: UInt32, on inputNode: AVAudioInputNode) throws {
        guard let audioUnit = inputNode.audioUnit else {
            throw MicRecorderError.engineStartFailed("input node has no underlying audio unit")
        }
        var mutableDeviceID = deviceID
        let status = AudioUnitSetProperty(
            audioUnit,
            kAudioOutputUnitProperty_CurrentDevice,
            kAudioUnitScope_Global,
            0,
            &mutableDeviceID,
            UInt32(MemoryLayout<UInt32>.size)
        )
        guard status == noErr else {
            throw MicRecorderError.engineStartFailed("AudioUnitSetProperty failed with OSStatus \(status)")
        }
    }
}
