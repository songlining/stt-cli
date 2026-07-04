import Foundation
import ArgumentParser
import AVFoundation
#if canImport(Darwin)
import Darwin
#endif

public struct STT: ParsableCommand {
    public static let configuration = CommandConfiguration(
        commandName: "stt",
        abstract: "Record and transcribe microphone, system, or meeting audio on macOS.",
        subcommands: [
            Doctor.self,
            Devices.self,
            Record.self,
            Transcribe.self,
            Pipeline.self,
            Mix.self,
            Permissions.self
        ]
    )

    public init() {}
}

// MARK: - stt doctor

public struct Doctor: ParsableCommand {
    public static let configuration = CommandConfiguration(abstract: "Check environment, dependencies, and permissions.")

    @Option(name: .long, help: "Path to the Python backend directory containing stt_vibevoice.")
    public var pythonBackend: String?

    @Flag(name: .long, help: "Exit non-zero if the Python transcription backend is not ready.")
    public var requireBackendReady: Bool = false

    public init() {}

    public func run() throws {
        print("stt doctor")
        print("==========")

        let osVersion = ProcessInfo.processInfo.operatingSystemVersion
        print("macOS version: \(osVersion.majorVersion).\(osVersion.minorVersion).\(osVersion.patchVersion)")

        #if arch(arm64)
        print("Architecture: arm64 (Apple Silicon)  [OK]")
        #else
        print("Architecture: x86_64 (Intel)  [WARNING: MLX/local transcription typically requires Apple Silicon]")
        #endif

        printToolCheck(name: "ffmpeg")
        printToolCheck(name: "ffprobe")
        printToolCheck(name: "python3")

        for line in BundleAttribution.diagnosticLines(
            bundlePath: Bundle.main.bundlePath,
            bundleIdentifier: Bundle.main.bundleIdentifier
        ) {
            print(line)
        }

        let micStatus = AudioPermissions.microphoneStatus()
        print("Microphone permission: \(micStatus.rawValue)")

        let screenStatus = AudioPermissions.screenRecordingStatus()
        print("Screen Recording permission (only used by ScreenCaptureKit fallback): \(screenStatus.rawValue)")

        let tapAvailability = SystemAudioRecorder.probeNativeTapAvailability()
        print("System-audio capture: \(tapAvailability.summary)")
        print("  falls back to named virtual input device (e.g. BlackHole) — see `stt devices`.")

        print("Transcription backend:")
        var backendReadyFailure: String?
        let backendDir = try Transcribe.resolvePythonBackendDirectory(overridePath: pythonBackend)
        do {
            if let backendDir {
                print("  backend path: \(backendDir.path)")
                let status = try PythonTranscriber.statusReport(workingDirectory: backendDir, timeout: 5, requireReady: requireBackendReady)
                let output = status.standardOutput.trimmingCharacters(in: .whitespacesAndNewlines)
                if output.isEmpty {
                    print("  status: no output from python backend status check")
                } else {
                    for line in output.split(separator: "\n") {
                        print("  \(line)")
                    }
                }
                if !status.succeeded {
                    print("  status check exited with code \(status.exitCode)")
                    let stderr = status.standardError.trimmingCharacters(in: .whitespacesAndNewlines)
                    if !stderr.isEmpty { print("  stderr: \(stderr)") }
                    backendReadyFailure = "Python transcription backend is not ready"
                }
            } else {
                print("  python backend directory not found (set --python-backend or STT_PYTHON_BACKEND)")
                backendReadyFailure = "Python transcription backend directory not found"
            }
        } catch {
            print("  status check failed: \(error.localizedDescription)")
            backendReadyFailure = error.localizedDescription
        }
        if requireBackendReady, let backendReadyFailure {
            throw ValidationError(backendReadyFailure)
        }
    }

    private func printToolCheck(name: String) {
        if let path = ProcessRunner.resolvePath(name) {
            print("\(name): found at \(path)  [OK]")
        } else {
            print("\(name): NOT FOUND on PATH  [WARNING]")
        }
    }
}

// MARK: - stt devices

public struct Devices: ParsableCommand {
    public static let configuration = CommandConfiguration(abstract: "List audio input devices.")

    public init() {}

    public func run() throws {
        let devices = try DeviceList.inputDevices()
        if devices.isEmpty {
            print("No input devices found.")
            return
        }
        print("Audio input devices:")
        for device in devices {
            let marker = device.isDefaultInput ? " (default input)" : ""
            print("  [\(device.id)] \(device.name) — \(device.inputChannelCount) ch\(marker)")
        }
    }
}

// MARK: - stt record

public enum RecordModeArgument: String, ExpressibleByArgument, CaseIterable {
    case mic
    case system
    case meeting
}

public struct Record: ParsableCommand {
    public static let configuration = CommandConfiguration(abstract: "Record microphone, system, or meeting audio to WAV.")

    @Option(name: .long, help: "Recording mode: mic, system, or meeting.")
    public var mode: RecordModeArgument = .mic

    @Option(name: .long, help: "Output WAV file path (mic/system mode).")
    public var output: String?

    @Option(name: .long, help: "Named input device to use (e.g. \"BlackHole 2ch\").")
    public var inputDevice: String?

    @Option(name: .long, help: "Optional recording duration in seconds; omit to record until Ctrl-C.")
    public var duration: Double?

    @Flag(name: .long, help: "Fail if the output looks header-only or contains no captured audio frames.")
    public var failIfEmpty: Bool = false

    @Flag(name: .long, help: "For meeting mode, write mic and system audio to separate WAV files.")
    public var separateTracks: Bool = false

    @Option(name: .long, help: "Output directory for meeting mode (used with --separate-tracks).")
    public var outputDir: String?

    public init() {}

    public func run() throws {
        if let duration, duration <= 0 {
            throw ValidationError("--duration must be greater than 0 seconds")
        }

        switch mode {
        case .mic:
            try runMic()
        case .system:
            try runSystem()
        case .meeting:
            try runMeeting()
        }
    }

    private func resolvedOutputURL(defaultName: String) -> URL {
        if let output {
            return URL(fileURLWithPath: output)
        }
        return Paths.recordingsDirectory().appendingPathComponent(defaultName)
    }

    private func runMic() throws {
        let outputURL = resolvedOutputURL(defaultName: "mic-\(Paths.timestampToken()).wav")
        let recorder = MicRecorder()

        var deviceID: UInt32?
        if let inputDevice {
            let device = try DeviceList.resolveInputDevice(named: inputDevice)
            deviceID = device.id
            print("Using input device: \(device.name) [\(device.id)]")
        }

        try recorder.start(outputURL: outputURL, inputDeviceID: deviceID)
        print("Recording microphone to \(outputURL.path)")
        print(durationStopMessage())

        let result = waitForStopTriggerAndStop(duration: duration) { try recorder.stop() }
        report(result: result)
        try enforceNonEmptyIfRequested(result)
    }

    private func runSystem() throws {
        let outputURL = resolvedOutputURL(defaultName: "system-\(Paths.timestampToken()).wav")
        let recorder = SystemAudioRecorder()

        let method = try recorder.start(outputURL: outputURL, fallbackDeviceName: inputDevice)
        switch method {
        case .coreAudioTap:
            print("Recording system audio via native CoreAudio process tap to \(outputURL.path)")
        case .namedInputDeviceFallback:
            print("[NOTE] Native CoreAudio system-audio tap is not available/implemented on this build.")
            print("Falling back to named virtual input device capture (e.g. BlackHole/Aggregate Device).")
            print("Recording system audio (fallback) to \(outputURL.path)")
        }
        print(durationStopMessage())

        let result = waitForStopTriggerAndStop(duration: duration) { try recorder.stop() }
        report(result: result)
        try enforceNonEmptyIfRequested(result)
    }

    private func runMeeting() throws {
        let baseDir: URL
        if let outputDir {
            baseDir = URL(fileURLWithPath: outputDir)
        } else {
            baseDir = Paths.recordingsDirectory().appendingPathComponent("meeting-\(Paths.timestampToken())")
        }
        try Paths.ensureDirectoryExists(baseDir)

        let micURL = baseDir.appendingPathComponent("mic.wav")
        let systemURL = baseDir.appendingPathComponent("system.wav")

        let recorder = MeetingRecorder()
        let method = try recorder.start(micOutputURL: micURL, systemOutputURL: systemURL, fallbackDeviceName: inputDevice)

        switch method {
        case .coreAudioTap:
            print("Recording meeting (mic + native system-audio tap) into \(baseDir.path)")
        case .namedInputDeviceFallback:
            print("[NOTE] Native CoreAudio system-audio tap is not available/implemented on this build.")
            print("System-audio track uses a named virtual input device fallback (e.g. BlackHole).")
            print("Recording meeting (mic + fallback system-audio) into \(baseDir.path)")
        }
        if !separateTracks {
            print("[NOTE] --separate-tracks not passed: recording mic.wav/system.wav first, then attempting mixed.wav after capture.")
        }
        print(durationStopMessage())

        waitForStopTrigger(duration: duration)

        let result = try recorder.stop(separateTracks: separateTracks)
        if let mic = result.micResult {
            print("Mic track: \(mic.outputURL.path) (\(String(format: "%.1f", mic.durationSeconds))s, \(formatFileSize(mic.fileSizeBytes)))")
            if let warning = mic.emptyAudioWarning { print(warning) }
        }
        if let system = result.systemResult {
            print("System track: \(system.outputURL.path) (\(String(format: "%.1f", system.durationSeconds))s, \(formatFileSize(system.fileSizeBytes)))")
            if let warning = system.emptyAudioWarning { print(warning) }
        }
        try enforceNonEmptyIfRequested(result.micResult)
        try enforceNonEmptyIfRequested(result.systemResult)

        if !separateTracks, let mic = result.micResult, let system = result.systemResult {
            let mixedURL = baseDir.appendingPathComponent("mixed.wav")
            let mixOutcome = Self.resolveMeetingMixOutcome(micResult: mic, systemResult: system, mixedURL: mixedURL)
            if let mixed = mixOutcome.mixedResult {
                print("Mixed track: \(mixed.outputURL.path) (\(String(format: "%.1f", mixed.durationSeconds))s, \(formatFileSize(mixed.fileSizeBytes)))")
                if let warning = mixed.emptyAudioWarning { print(warning) }
                try enforceNonEmptyIfRequested(mixed)
            } else if let note = mixOutcome.note {
                print("[NOTE] \(note)")
            }
        }
    }

    public struct MeetingMixOutcome {
        public let mixedResult: RecordingResult?
        public let note: String?
    }

    public static func resolveMeetingMixOutcome(micResult: RecordingResult,
                                                systemResult: RecordingResult,
                                                mixedURL: URL) -> MeetingMixOutcome {
        do {
            let mixed = try WAVMixer.mixFiles(micResult.outputURL, systemResult.outputURL, outputURL: mixedURL)
            return MeetingMixOutcome(mixedResult: mixed, note: nil)
        } catch {
            return MeetingMixOutcome(
                mixedResult: nil,
                note: "Mixed track unavailable (\(error.localizedDescription)); mic.wav and system.wav remain available separately."
            )
        }
    }

    private func enforceNonEmptyIfRequested(_ result: RecordingResult?) throws {
        guard failIfEmpty, let result, !result.likelyContainsAudioData else { return }
        throw ValidationError(result.emptyAudioWarning ?? "Recording output did not contain captured audio frames")
    }

    private func report(result: RecordingResult) {
        print("Saved: \(result.outputURL.path)")
        print("Duration: \(String(format: "%.1f", result.durationSeconds))s")
        print("File size: \(formatFileSize(result.fileSizeBytes))")
        if let warning = result.emptyAudioWarning { print(warning) }
    }

    private func formatFileSize(_ bytes: UInt64?) -> String {
        guard let bytes else { return "unknown size" }
        if bytes < 1024 { return "\(bytes) bytes" }
        return String(format: "%.1f KB", Double(bytes) / 1024.0)
    }

    private func durationStopMessage() -> String {
        if let duration {
            return "Recording for \(String(format: "%.1f", duration))s. Press Ctrl-C to stop early."
        }
        return "Press Ctrl-C to stop."
    }

    /// Blocks until SIGINT/SIGTERM or the optional duration elapses, then runs
    /// `stop` and returns its result.
    private func waitForStopTriggerAndStop(duration: Double?, stop: @escaping () throws -> RecordingResult) -> RecordingResult {
        waitForStopTrigger(duration: duration)
        do {
            return try stop()
        } catch {
            FileHandle.standardError.write("Error stopping recorder: \(error.localizedDescription)\n".data(using: .utf8)!)
            Darwin.exit(1)
        }
    }

    private func waitForStopTrigger(duration: Double?) {
        let sema = DispatchSemaphore(value: 0)
        installSignalHandlers { sema.signal() }
        if let duration {
            let timer = DispatchSource.makeTimerSource(queue: DispatchQueue.global(qos: .userInitiated))
            timer.schedule(deadline: .now() + duration)
            timer.setEventHandler { sema.signal() }
            timer.resume()
            Self.retainedTimers.append(timer)
        }
        sema.wait()
    }

    private func installSignalHandlers(_ handler: @escaping () -> Void) {
        for signalNumber in [SIGINT, SIGTERM] {
            signal(signalNumber, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: signalNumber, queue: DispatchQueue.global(qos: .userInitiated))
            source.setEventHandler(handler: handler)
            source.resume()
            Self.retainedSignalSources.append(source)
        }
    }

    // Keep dispatch sources alive for the lifetime of the process.
    nonisolated(unsafe) private static var retainedSignalSources: [DispatchSourceSignal] = []
    nonisolated(unsafe) private static var retainedTimers: [DispatchSourceTimer] = []
}

// MARK: - stt transcribe

public struct Transcribe: ParsableCommand {
    public static let configuration = CommandConfiguration(abstract: "Transcribe an audio file using the Python backend.")

    @Argument(help: "Path to the audio file to transcribe.")
    public var audioPath: String

    @Option(name: .long, help: "Path to write a plain-text transcript.")
    public var output: String?

    @Option(name: .long, help: "Path to write structured JSON output.")
    public var json: String?

    @Option(name: .long, help: "Compute device: auto, gpu, or cpu.")
    public var device: TranscriberDevice = .auto

    @Option(name: .long, help: "Optional transcription timeout in seconds.")
    public var timeout: Double?

    @Option(name: .long, help: "Model path or Hugging Face model ID to pass to the Python backend.")
    public var model: String?

    @Option(name: .long, help: "Maximum new tokens for VibeVoice generation.")
    public var maxNewTokens: Int?

    @Option(name: .long, help: "Path to the Python backend directory containing stt_vibevoice.")
    public var pythonBackend: String?

    @Flag(name: .long, help: "Check backend readiness before transcribing and fail early if dependencies are missing.")
    public var requireBackendReady: Bool = false

    public init() {}

    public func run() throws {
        if let timeout, timeout <= 0 {
            throw ValidationError("--timeout must be greater than 0 seconds")
        }
        if let maxNewTokens, maxNewTokens <= 0 {
            throw ValidationError("--max-new-tokens must be greater than 0")
        }

        let workingDir = try Self.resolvePythonBackendDirectory(overridePath: pythonBackend)
        _ = try Paths.requireExistingFile(audioPath)
        if requireBackendReady {
            try Self.requirePythonBackendReady(workingDirectory: workingDir, timeout: timeout)
        }

        let result = try PythonTranscriber.transcribe(
            audioPath: audioPath,
            outputTextPath: output,
            outputJSONPath: json,
            device: device.rawValue,
            workingDirectory: workingDir,
            timeout: timeout,
            modelPath: model,
            maxNewTokens: maxNewTokens
        )

        if let text = result.transcriptText {
            print(text)
        } else {
            print("Transcription complete. Raw backend output:")
            print(result.raw)
        }
    }

    /// Locates the `python/` directory (containing `stt_vibevoice`). Search
    /// order: explicit `STT_PYTHON_BACKEND`, current working directory, then
    /// bundled app resources. This lets the app wrapper run from outside the
    /// repo while still finding the packaged Python module.
    public static func findPythonBackendDirectory(fileManager: FileManager = .default,
                                                  environment: [String: String] = ProcessInfo.processInfo.environment,
                                                  currentDirectory: URL = URL(fileURLWithPath: FileManager.default.currentDirectoryPath),
                                                  bundleResourceURL: URL? = Bundle.main.resourceURL) -> URL? {
        let candidates: [URL] = [
            environment["STT_PYTHON_BACKEND"].flatMap { $0.isEmpty ? nil : URL(fileURLWithPath: $0, isDirectory: true) },
            currentDirectory.appendingPathComponent("python", isDirectory: true),
            bundleResourceURL?.appendingPathComponent("python", isDirectory: true)
        ].compactMap { $0 }

        for candidate in candidates {
            if isDirectory(candidate, fileManager: fileManager) {
                return candidate
            }
        }
        return nil
    }

    public static func requirePythonBackendReady(workingDirectory: URL?, timeout: Double? = nil) throws {
        let status = try PythonTranscriber.statusReport(
            workingDirectory: workingDirectory,
            timeout: timeout ?? 5,
            requireReady: true
        )
        guard status.succeeded else {
            throw ValidationError("Python transcription backend is not ready")
        }
    }

    public static func resolvePythonBackendDirectory(overridePath: String?, fileManager: FileManager = .default) throws -> URL? {
        guard let overridePath, !overridePath.isEmpty else {
            return findPythonBackendDirectory(fileManager: fileManager)
        }
        let overrideURL = URL(fileURLWithPath: overridePath, isDirectory: true)
        guard isDirectory(overrideURL, fileManager: fileManager) else {
            throw ValidationError("--python-backend must point to an existing directory")
        }
        return overrideURL
    }

    private static func isDirectory(_ url: URL, fileManager: FileManager) -> Bool {
        var isDirectory: ObjCBool = false
        return fileManager.fileExists(atPath: url.path, isDirectory: &isDirectory) && isDirectory.boolValue
    }
}

// MARK: - stt pipeline

public struct Pipeline: ParsableCommand {
    public static let configuration = CommandConfiguration(abstract: "Record then transcribe in one step.")

    @Option(name: .long, help: "Recording mode: mic, system, or meeting.")
    public var mode: RecordModeArgument = .mic

    @Option(name: .long, help: "Session name, used to derive output file names.")
    public var name: String = "session"

    @Option(name: .long, help: "Named input device to use for system fallback capture.")
    public var inputDevice: String?

    @Option(name: .long, help: "Optional recording duration in seconds; omit to record until Ctrl-C.")
    public var duration: Double?

    @Flag(name: .long, help: "Fail if the recording output looks header-only before transcribing.")
    public var failIfEmpty: Bool = false

    @Option(name: .long, help: "Optional transcription timeout in seconds.")
    public var transcribeTimeout: Double?

    @Option(name: .long, help: "Compute device for transcription: auto, gpu, or cpu.")
    public var device: TranscriberDevice = .auto

    @Option(name: .long, help: "Model path or Hugging Face model ID to pass to the Python backend.")
    public var model: String?

    @Option(name: .long, help: "Maximum new tokens for VibeVoice generation.")
    public var maxNewTokens: Int?

    @Option(name: .long, help: "Path to the Python backend directory containing stt_vibevoice.")
    public var pythonBackend: String?

    @Flag(name: .long, help: "Check backend readiness before recording and fail early if dependencies are missing.")
    public var requireBackendReady: Bool = false

    public init() {}

    public func run() throws {
        if let duration, duration <= 0 {
            throw ValidationError("--duration must be greater than 0 seconds")
        }
        if let transcribeTimeout, transcribeTimeout <= 0 {
            throw ValidationError("--transcribe-timeout must be greater than 0 seconds")
        }
        if let maxNewTokens, maxNewTokens <= 0 {
            throw ValidationError("--max-new-tokens must be greater than 0")
        }

        let backendDir = try Transcribe.resolvePythonBackendDirectory(overridePath: pythonBackend)
        if requireBackendReady {
            try Transcribe.requirePythonBackendReady(workingDirectory: backendDir, timeout: transcribeTimeout)
        }

        let runID = Paths.timestampToken()
        let runDir = Paths.runDirectory(runID: runID)
        try Paths.ensureDirectoryExists(runDir)

        let startedAt = Date()
        let recordingMode = RecordingMode(rawValue: mode.rawValue) ?? .mic
        let transcriptTextURL = runDir.appendingPathComponent("\(name).txt")
        let transcriptJSONURL = runDir.appendingPathComponent("\(name).json")
        let micURL = runDir.appendingPathComponent("mic.wav")
        let systemURL = runDir.appendingPathComponent("system.wav")
        let mixedURL = runDir.appendingPathComponent("mixed.wav")
        var outputURLs: [URL]
        var audioToTranscribeURL: URL

        if mode == .meeting {
            outputURLs = [micURL, systemURL]
            audioToTranscribeURL = micURL
        } else {
            let outputURL = runDir.appendingPathComponent("\(name).wav")
            outputURLs = [outputURL]
            audioToTranscribeURL = outputURL
        }

        var state = SessionState(
            runID: runID,
            name: name,
            mode: recordingMode,
            startedAt: startedAt,
            outputPaths: outputURLs.map(\.path),
            separateTracks: mode == .meeting,
            transcribedAudioPath: audioToTranscribeURL.path,
            transcriptTextPath: transcriptTextURL.path,
            transcriptJSONPath: transcriptJSONURL.path
        )

        func persistState(notes: String? = nil, backend: String? = nil) {
            state.finishedAt = Date()
            state.durationSeconds = state.finishedAt?.timeIntervalSince(startedAt)
            state.notes = notes
            state.backend = backend
            _ = try? SessionStateStore.write(state, toRunDirectory: runDir)
        }

        print("Pipeline mode=\(mode.rawValue) name=\"\(name)\" run=\(runID)")
        if let duration {
            print("Note: `stt pipeline` records for \(String(format: "%.1f", duration))s, then transcribes automatically.")
        } else {
            print("Note: `stt pipeline` records until you press Ctrl-C, then transcribes automatically.")
        }
        if mode == .meeting {
            print("Note: meeting pipeline records separate mic/system tracks, then transcribes mixed.wav when mixing succeeds (falls back to mic.wav otherwise).")
        }

        do {
            var record = Record()
            record.mode = mode
            record.inputDevice = inputDevice
            record.duration = duration
            record.failIfEmpty = failIfEmpty
            var meetingMixNote: String?
            if mode == .meeting {
                record.outputDir = runDir.path
                record.separateTracks = true
            } else {
                record.output = audioToTranscribeURL.path
            }
            try record.run()

            if mode == .meeting {
                let selection = Self.resolveMeetingAudioSource(micURL: micURL, systemURL: systemURL, mixedURL: mixedURL)
                audioToTranscribeURL = selection.audioToTranscribeURL
                outputURLs = selection.outputURLs
                state.outputPaths = outputURLs.map(\.path)
                state.transcribedAudioPath = audioToTranscribeURL.path
                meetingMixNote = selection.note
                if let note = selection.note {
                    print("[NOTE] \(note)")
                } else {
                    print("Transcribing mixed meeting audio: \(audioToTranscribeURL.path)")
                }
            }

            let result = try PythonTranscriber.transcribe(
                audioPath: audioToTranscribeURL.path,
                outputTextPath: transcriptTextURL.path,
                outputJSONPath: transcriptJSONURL.path,
                device: device.rawValue,
                workingDirectory: backendDir,
                timeout: transcribeTimeout,
                modelPath: model,
                maxNewTokens: maxNewTokens
            )

            if let text = result.transcriptText {
                print(text)
            } else {
                print("Transcription complete. Raw backend output:")
                print(result.raw)
            }
            // Successful meeting runs may carry a non-fatal mix fallback note;
            // mic/system modes keep nil notes so existing success metadata stays clean.
            persistState(notes: meetingMixNote, backend: result.backend)
        } catch {
            persistState(notes: "Pipeline failed: \(error.localizedDescription)")
            throw error
        }
    }

    public struct MeetingAudioSelection: Equatable {
        public let audioToTranscribeURL: URL
        public let outputURLs: [URL]
        public let note: String?
    }

    public static func resolveMeetingAudioSource(micURL: URL, systemURL: URL, mixedURL: URL) -> MeetingAudioSelection {
        do {
            _ = try WAVMixer.mixFiles(micURL, systemURL, outputURL: mixedURL)
            return MeetingAudioSelection(
                audioToTranscribeURL: mixedURL,
                outputURLs: [micURL, systemURL, mixedURL],
                note: nil
            )
        } catch {
            return MeetingAudioSelection(
                audioToTranscribeURL: micURL,
                outputURLs: [micURL, systemURL],
                note: "Mixed track unavailable (\(error.localizedDescription)); transcribing mic.wav instead."
            )
        }
    }
}

// MARK: - stt mix

public struct Mix: ParsableCommand {
    public static let configuration = CommandConfiguration(abstract: "Mix two compatible WAV files into a mono 16-bit WAV.")

    @Argument(help: "First WAV file, typically mic.wav.")
    public var firstAudioPath: String

    @Argument(help: "Second WAV file, typically system.wav.")
    public var secondAudioPath: String

    @Option(name: .long, help: "Output WAV path. Defaults to mixed.wav next to the first input.")
    public var output: String?

    @Flag(name: .long, help: "Fail if the mixed output looks header-only or contains no audio frames.")
    public var failIfEmpty: Bool = false

    public init() {}

    public func run() throws {
        let firstURL = try Paths.requireExistingFile(firstAudioPath)
        let secondURL = try Paths.requireExistingFile(secondAudioPath)
        let outputURL = output.map { URL(fileURLWithPath: $0) }
            ?? firstURL.deletingLastPathComponent().appendingPathComponent("mixed.wav")

        let result = try WAVMixer.mixFiles(firstURL, secondURL, outputURL: outputURL)
        print("Mixed: \(result.outputURL.path)")
        print("Duration: \(String(format: "%.3f", result.durationSeconds))s")
        print("File size: \(formatFileSize(result.fileSizeBytes))")
        if let warning = result.emptyAudioWarning {
            print(warning)
            if failIfEmpty { throw ValidationError(warning) }
        }
    }

    private func formatFileSize(_ bytes: UInt64?) -> String {
        guard let bytes else { return "unknown size" }
        if bytes < 1024 { return "\(bytes) bytes" }
        return String(format: "%.1f KB", Double(bytes) / 1024.0)
    }
}

// MARK: - stt permissions

public struct Permissions: ParsableCommand {
    public static let configuration = CommandConfiguration(
        abstract: "Show current permission status and recovery guidance.",
        subcommands: [ResetHelp.self]
    )

    public init() {}

    public func run() throws {
        print("Permission status")
        print("=================")
        print("Microphone: \(AudioPermissions.microphoneStatus().rawValue)")
        print("Screen Recording (ScreenCaptureKit fallback only): \(AudioPermissions.screenRecordingStatus().rawValue)")
        print("")
        print("Run `stt permissions reset-help` for tccutil reset commands.")
    }

    public struct ResetHelp: ParsableCommand {
        public static let configuration = CommandConfiguration(commandName: "reset-help", abstract: "Print tccutil reset commands and manual recovery steps.")

        @Option(name: .long, help: "Bundle identifier to use in tccutil reset guidance.")
        public var bundleID: String?

        public init() {}

        public func run() throws {
            let bundleID = bundleID ?? Bundle.main.bundleIdentifier ?? "com.hashicorp.stt"
            print(AudioPermissions.tccAttributionGuidance(bundleID: bundleID))
            print("")
            print(AudioPermissions.microphoneResetGuidance(bundleID: bundleID))
            print("")
            print(AudioPermissions.audioCaptureResetGuidance(bundleID: bundleID))
            print("")
            print(AudioPermissions.screenRecordingResetGuidance(bundleID: bundleID))
        }
    }
}
