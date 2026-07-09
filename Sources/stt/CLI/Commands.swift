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
            TranscribeMeeting.self,
            Pipeline.self,
            Mix.self,
            Speaker.self,
            Identify.self,
            Diarize.self,
            NameSpeakers.self,
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

        let tapDiagnostic = SystemAudioRecorder.probeNativeTapDiagnostic(
            attemptCreateDestroy: ProcessInfo.processInfo.environment["STT_NATIVE_TAP_DIAGNOSTIC"] == "1"
        )
        print("System-audio capture: \(tapDiagnostic.summary)")
        print("  falls back to named virtual input device (e.g. BlackHole) — see `stt devices`.")
        if tapDiagnostic.availability.isPotentiallyAvailable && !tapDiagnostic.createDestroyAttempted {
            print("  set STT_NATIVE_TAP_DIAGNOSTIC=1 to run an opt-in CoreAudio tap create/destroy diagnostic.")
        }
        if tapDiagnostic.availability.isPotentiallyAvailable {
            let attemptPayloadDiagnostic = ProcessInfo.processInfo.environment["STT_NATIVE_TAP_PAYLOAD_DIAGNOSTIC"] == "1"
            let payloadDiagnostic = SystemAudioRecorder.probeNativeTapPayloadDiagnostic(attempt: attemptPayloadDiagnostic)
            if attemptPayloadDiagnostic {
                print("  payload diagnostic: \(payloadDiagnostic.summary)")
            } else {
                print("  set STT_NATIVE_TAP_PAYLOAD_DIAGNOSTIC=1 to run a short opt-in payload/TCC responsibility check.")
            }
        }

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

public enum MixModeArgument: String, ExpressibleByArgument, CaseIterable {
    case raw
    case balanced

    public var mixMode: MixMode {
        switch self {
        case .raw: return .raw
        case .balanced: return .balanced
        }
    }
}

public enum MeetingTranscriptionModeArgument: String, ExpressibleByArgument, CaseIterable {
    case separate
    case mixed
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

    @Option(name: .long, help: "Output directory for meeting mode (mic.wav and system.wav are written here).")
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
        print("[NOTE] Recording mic.wav + system.wav only. To produce a combined mixed.wav run `stt mix mic.wav system.wav --output mixed.wav` afterwards.")
        print(durationStopMessage())

        waitForStopTrigger(duration: duration)

        let result = try recorder.stop()
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
        // A stop trigger has been received (signal or duration). Recording
        // capture is now over and teardown begins (stop recorders, then
        // optionally mix `mixed.wav`). Teardown/mixing can take minutes for
        // long meetings, and during that window nobody is waiting on the
        // semaphore, so the dispatch-source handlers above would just
        // re-signal a semaphore with no waiter -- effectively swallowing any
        // further SIGINT/SIGTERM. Restore the default disposition for both
        // signals so a *second* Ctrl-C/stop request terminates the process
        // immediately instead of appearing to hang.
        Self.restoreDefaultSignalDisposition()
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

    /// Cancels the retained signal dispatch sources and resets SIGINT/SIGTERM
    /// to their default disposition (terminate). Call this once capture has
    /// ended so that the (potentially long) post-capture teardown/mix phase
    /// can be interrupted by a second stop signal instead of hanging.
    private static func restoreDefaultSignalDisposition() {
        for source in retainedSignalSources {
            source.cancel()
        }
        retainedSignalSources.removeAll()
        for signalNumber in [SIGINT, SIGTERM] {
            signal(signalNumber, SIG_DFL)
        }
    }

    // Keep dispatch sources alive for the lifetime of the capture phase.
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
        _ = try Paths.requireNonEmptyFile(audioPath)
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

// MARK: - stt transcribe-meeting

/// Diarisation configuration shared by `transcribe-meeting` and `pipeline`.
/// Resolved from CLI flags before being handed to `transcribeMeeting(...)`;
/// mic is always diarised with offset 0, system with offset = mic speaker count.
///
/// `workingDirectory` is the Python backend dir used for the diarisation step
/// specifically (may differ from the ASR backend: speechbrain needs the
/// Python 3.11 runtime venv, e.g. `runtime/`, while MLX ASR needs `python/`).
public struct MeetingDiarizationConfig {
    public let provider: String
    public let numSpeakers: Int?
    public let distanceThreshold: Double?
    public let workingDirectory: URL?
    public init(provider: String, numSpeakers: Int?, distanceThreshold: Double?, workingDirectory: URL?) {
        self.provider = provider
        self.numSpeakers = numSpeakers
        self.distanceThreshold = distanceThreshold
        self.workingDirectory = workingDirectory
    }
}

/// Speaker identification configuration for `transcribe-meeting --identify`:
/// matches each diarised speaker against enrolled profiles and relabels
/// confident matches with real names.
public struct MeetingIdentificationConfig {
    public let profilesDirectory: URL
    public let matchThreshold: Double
    public let matchMargin: Double
    public let workingDirectory: URL?
    public let provider: String
    public let minSpeechSeconds: Double
    public init(profilesDirectory: URL, matchThreshold: Double, matchMargin: Double, workingDirectory: URL?, provider: String, minSpeechSeconds: Double) {
        self.profilesDirectory = profilesDirectory
        self.matchThreshold = matchThreshold
        self.matchMargin = matchMargin
        self.workingDirectory = workingDirectory
        self.provider = provider
        self.minSpeechSeconds = minSpeechSeconds
    }
}

public struct MeetingTranscriptionResult {
    public let text: String
    public let jsonData: Data
    public let backend: String?
    public let transcribedAudioPaths: [String]
    public let sourceTranscriptPaths: [String]
}

public struct TranscribeMeeting: ParsableCommand {
    public static let configuration = CommandConfiguration(
        commandName: "transcribe-meeting",
        abstract: "Transcribe meeting mic/system tracks separately, then merge by timestamp."
    )

    @Argument(help: "Path to mic.wav.")
    public var micAudioPath: String

    @Argument(help: "Path to system.wav.")
    public var systemAudioPath: String

    @Option(name: .long, help: "Path to write a merged plain-text transcript.")
    public var output: String?

    @Option(name: .long, help: "Path to write merged structured JSON output.")
    public var json: String?

    @Option(name: .long, help: "Compute device: auto, gpu, or cpu.")
    public var device: TranscriberDevice = .auto

    @Option(name: .long, help: "Optional transcription timeout in seconds for each source track.")
    public var timeout: Double?

    @Option(name: .long, help: "Model path or Hugging Face model ID to pass to the Python backend.")
    public var model: String?

    @Option(name: .long, help: "Maximum new tokens for VibeVoice generation.")
    public var maxNewTokens: Int?

    @Option(name: .long, help: "Path to the Python backend directory containing stt_vibevoice.")
    public var pythonBackend: String?

    @Flag(name: .long, help: "Check backend readiness before transcribing and fail early if dependencies are missing.")
    public var requireBackendReady: Bool = false

    @Flag(name: .long, help: "Diarise each track: assign Speaker 0, Speaker 1... by clustering voice embeddings.")
    public var diarize: Bool = false

    @Option(name: .long, help: "Force exactly N speakers per track.")
    public var diarizeNumSpeakers: Int?

    @Option(name: .long, help: "Cosine-distance cut for auto speaker count (default 0.15).")
    public var diarizeDistanceThreshold: Double?

    @Option(name: .long, help: "Path to stt config JSON (for speaker provider).")
    public var config: String?

    @Option(name: .long, help: "Embedding provider override.")
    public var diarizeProvider: String?

    @Option(name: .long, help: "Python backend dir for the diarisation step (speechbrain needs the Python 3.11 runtime venv, e.g. 'runtime/'; defaults to the ASR backend).")
    public var diarizePythonBackend: String?

    @Flag(name: .long, help: "After diarisation, match speakers against enrolled profiles and relabel with names.")
    public var identify: Bool = false

    @Option(name: .long, help: "Override the configured speaker profiles directory (for --identify).")
    public var identifyProfilesDir: String?

    @Option(name: .long, help: "Minimum match confidence for --identify (default 0.78).")
    public var identifyThreshold: Double?

    @Option(name: .long, help: "Minimum margin over runner-up for --identify (default 0.05).")
    public var identifyMargin: Double?

    @Option(name: .long, help: "Path to the Python backend for speaker extraction/matching (runtime/ venv).")
    public var identifyPythonBackend: String?

    public init() {}

    public func run() throws {
        if let timeout, timeout <= 0 {
            throw ValidationError("--timeout must be greater than 0 seconds")
        }
        if let maxNewTokens, maxNewTokens <= 0 {
            throw ValidationError("--max-new-tokens must be greater than 0")
        }
        if diarizeNumSpeakers != nil && diarizeDistanceThreshold != nil {
            throw ValidationError("--diarize-num-speakers and --diarize-distance-threshold are mutually exclusive.")
        }
        if identify && !diarize {
            throw ValidationError("--identify requires --diarize.")
        }

        let workingDir = try Transcribe.resolvePythonBackendDirectory(overridePath: pythonBackend)
        if requireBackendReady {
            try Transcribe.requirePythonBackendReady(workingDirectory: workingDir, timeout: timeout)
        }

        var diarizationConfig: MeetingDiarizationConfig?
        var identifyConfig: MeetingIdentificationConfig?
        if diarize {
            let sttConfig = try STTConfigLoader.load(explicitPath: config)
            let provider = SpeakerCLISupport.resolvedProvider(cliValue: diarizeProvider, config: sttConfig)
            // speechbrain needs the Python 3.11 runtime venv; default to the
            // ASR backend (works for mfcc-test), override with --diarize-python-backend.
            let diarizeBackend = try Transcribe.resolvePythonBackendDirectory(overridePath: diarizePythonBackend)
            diarizationConfig = MeetingDiarizationConfig(
                provider: provider,
                numSpeakers: diarizeNumSpeakers,
                distanceThreshold: diarizeDistanceThreshold,
                workingDirectory: diarizeBackend
            )
            if identify {
                identifyConfig = MeetingIdentificationConfig(
                    profilesDirectory: SpeakerCLISupport.resolveProfilesDirectory(config: sttConfig, overridePath: identifyProfilesDir),
                    matchThreshold: SpeakerCLISupport.resolvedThreshold(cliValue: identifyThreshold, config: sttConfig),
                    matchMargin: SpeakerCLISupport.resolvedMargin(cliValue: identifyMargin, config: sttConfig),
                    workingDirectory: try Transcribe.resolvePythonBackendDirectory(overridePath: identifyPythonBackend),
                    provider: SpeakerCLISupport.resolvedProvider(cliValue: nil, config: sttConfig),
                    minSpeechSeconds: SpeakerCLISupport.resolvedMinimumSpeechSeconds(cliValue: nil, config: sttConfig)
                )
            }
        }

        let result = try Self.transcribeMeeting(
            micURL: URL(fileURLWithPath: micAudioPath),
            systemURL: URL(fileURLWithPath: systemAudioPath),
            outputTextURL: output.map { URL(fileURLWithPath: $0) },
            outputJSONURL: json.map { URL(fileURLWithPath: $0) },
            device: device.rawValue,
            workingDirectory: workingDir,
            timeout: timeout,
            modelPath: model,
            maxNewTokens: maxNewTokens,
            diarize: diarize,
            diarization: diarizationConfig,
            identify: identify,
            profilesDirectory: identifyConfig?.profilesDirectory,
            matchThreshold: identifyConfig?.matchThreshold ?? 0.78,
            matchMargin: identifyConfig?.matchMargin ?? 0.05,
            identifyWorkingDirectory: identifyConfig?.workingDirectory,
            identifyProvider: identifyConfig?.provider ?? "speechbrain",
            identifyMinSpeechSeconds: identifyConfig?.minSpeechSeconds ?? 8.0
        )
        print(result.text)
    }

    public static func transcribeMeeting(micURL: URL,
                                         systemURL: URL,
                                         outputTextURL: URL?,
                                         outputJSONURL: URL?,
                                         device: String,
                                         workingDirectory: URL?,
                                         timeout: TimeInterval?,
                                         modelPath: String?,
                                         maxNewTokens: Int?,
                                         diarize: Bool = false,
                                         diarization: MeetingDiarizationConfig? = nil,
                                         identify: Bool = false,
                                         profilesDirectory: URL? = nil,
                                         matchThreshold: Double = 0.78,
                                         matchMargin: Double = 0.05,
                                         identifyWorkingDirectory: URL? = nil,
                                         identifyProvider: String = "speechbrain",
                                         identifyMinSpeechSeconds: Double = 8.0) throws -> MeetingTranscriptionResult {
        let micAvailable = (try? Paths.requireNonEmptyFile(micURL.path)) != nil
        let systemAvailable = (try? Paths.requireNonEmptyFile(systemURL.path)) != nil
        guard micAvailable || systemAvailable else {
            _ = try Paths.requireNonEmptyFile(micURL.path)
            _ = try Paths.requireNonEmptyFile(systemURL.path)
            throw ValidationError("No transcribable meeting audio found; both mic and system tracks are missing or empty.")
        }

        let artifactBase = outputJSONURL ?? outputTextURL ?? FileManager.default.temporaryDirectory.appendingPathComponent("stt-meeting-\(UUID().uuidString).json")
        let micJSONURL = sourceArtifactURL(from: artifactBase, source: "mic", extension: "json")
        let micTextURL = sourceArtifactURL(from: outputTextURL ?? artifactBase, source: "mic", extension: "txt")
        let systemJSONURL = sourceArtifactURL(from: artifactBase, source: "system", extension: "json")
        let systemTextURL = sourceArtifactURL(from: outputTextURL ?? artifactBase, source: "system", extension: "txt")

        var backendNames: [String] = []
        var sourceTranscriptPaths: [String] = []
        var transcribedAudioPaths: [String] = []

        if micAvailable {
            let result = try PythonTranscriber.transcribe(
                audioPath: micURL.path,
                outputTextPath: micTextURL.path,
                outputJSONPath: micJSONURL.path,
                device: device,
                workingDirectory: workingDirectory,
                timeout: timeout,
                modelPath: modelPath,
                maxNewTokens: maxNewTokens
            )
            if let backend = result.backend { backendNames.append(backend) }
            sourceTranscriptPaths += [micTextURL.path, micJSONURL.path]
            transcribedAudioPaths.append(micURL.path)
        }

        var micSpeakerCount = 0
        if micAvailable, diarize, let cfg = diarization {
            do {
                let res = try PythonDiarizer.diarize(
                    audioPath: micURL.path, transcriptJSONPath: micJSONURL.path,
                    provider: cfg.provider, numSpeakers: cfg.numSpeakers,
                    distanceThreshold: cfg.distanceThreshold, minSpeechSeconds: nil,
                    workingDirectory: cfg.workingDirectory, speakerIdOffset: 0)
                try TranscriptMerger.applyDiarizationToFile(transcriptURL: micJSONURL, result: res)
                micSpeakerCount = res.numSpeakers
                print("[diarize] mic: \(res.numSpeakers) speakers")
            } catch {
                FileHandle.standardError.write("[diarize] WARNING: mic diarization failed (\(error)); continuing without speaker labels.\n".data(using: .utf8)!)
            }
        }

        if systemAvailable {
            let result = try PythonTranscriber.transcribe(
                audioPath: systemURL.path,
                outputTextPath: systemTextURL.path,
                outputJSONPath: systemJSONURL.path,
                device: device,
                workingDirectory: workingDirectory,
                timeout: timeout,
                modelPath: modelPath,
                maxNewTokens: maxNewTokens
            )
            if let backend = result.backend { backendNames.append(backend) }
            sourceTranscriptPaths += [systemTextURL.path, systemJSONURL.path]
            transcribedAudioPaths.append(systemURL.path)
        }

        if systemAvailable, diarize, let cfg = diarization {
            do {
                let res = try PythonDiarizer.diarize(
                    audioPath: systemURL.path, transcriptJSONPath: systemJSONURL.path,
                    provider: cfg.provider, numSpeakers: cfg.numSpeakers,
                    distanceThreshold: cfg.distanceThreshold, minSpeechSeconds: nil,
                    workingDirectory: cfg.workingDirectory, speakerIdOffset: micSpeakerCount)
                try TranscriptMerger.applyDiarizationToFile(transcriptURL: systemJSONURL, result: res)
                print("[diarize] system: \(res.numSpeakers) speakers (offset \(micSpeakerCount))")
            } catch {
                FileHandle.standardError.write("[diarize] WARNING: system diarization failed (\(error)); continuing without speaker labels.\n".data(using: .utf8)!)
            }
        }

        let merge = try TranscriptMerger.merge(
            micJSONURL: micAvailable ? micJSONURL : nil,
            systemJSONURL: systemAvailable ? systemJSONURL : nil,
            outputTextURL: outputTextURL,
            outputJSONURL: outputJSONURL
        )

        if identify, diarize, let outputJSONURL {
            do {
                let speakerNames = try identifySpeakersInMergedTranscript(
                    mergedJSONURL: outputJSONURL,
                    micJSONURL: micAvailable ? micJSONURL : nil,
                    systemJSONURL: systemAvailable ? systemJSONURL : nil,
                    micURL: micURL,
                    systemURL: systemURL,
                    profilesDirectory: profilesDirectory,
                    provider: identifyProvider,
                    minSpeechSeconds: identifyMinSpeechSeconds,
                    threshold: matchThreshold,
                    margin: matchMargin,
                    workingDirectory: identifyWorkingDirectory
                )
                if !speakerNames.isEmpty {
                    try TranscriptMerger.applySpeakerNames(
                        transcriptURL: outputJSONURL, outputTextURL: outputTextURL,
                        speakerNames: speakerNames)
                    print("[identify] relabeled \(speakerNames.count) speaker(s): \(speakerNames.map { "\($0.value) (Speaker \($0.key))" }.joined(separator: ", "))")
                } else {
                    print("[identify] no confident speaker matches; keeping anonymous labels.")
                }
            } catch {
                FileHandle.standardError.write("[identify] WARNING: speaker identification failed (\(error)); continuing with numeric labels.\n".data(using: .utf8)!)
            }
        } else if identify, diarize {
            FileHandle.standardError.write("[identify] WARNING: no --json output path; cannot apply speaker names. Skipping identification.\n".data(using: .utf8)!)
        }

        return MeetingTranscriptionResult(
            text: merge.text,
            jsonData: merge.jsonData,
            backend: backendNames.isEmpty ? nil : Array(Set(backendNames)).sorted().joined(separator: "+"),
            transcribedAudioPaths: transcribedAudioPaths,
            sourceTranscriptPaths: sourceTranscriptPaths
        )
    }

    private static func sourceArtifactURL(from baseURL: URL, source: String, extension ext: String) -> URL {
        let directory = baseURL.deletingLastPathComponent()
        let stem = baseURL.deletingPathExtension().lastPathComponent
        return directory.appendingPathComponent("\(stem).\(source).\(ext)")
    }

    /// Extracts a speaker embedding for each diarised speaker in the merged
    /// transcript, matches each against enrolled profiles, and returns a
    /// `[speaker_id: displayName]` map containing ONLY confident matches.
    /// Unmatched speakers are omitted (they keep their "Speaker N" label).
    /// Returns `[:]` when no profiles are enrolled.
    ///
    /// Extraction runs once per speaker (expensive ML step); matching is
    /// cheap. Speakers are processed sequentially (fine for 2–4 speakers).
    private static func identifySpeakersInMergedTranscript(
        mergedJSONURL: URL?,
        micJSONURL: URL?,
        systemJSONURL: URL?,
        micURL: URL,
        systemURL: URL,
        profilesDirectory: URL?,
        provider: String,
        minSpeechSeconds: Double,
        threshold: Double,
        margin: Double,
        workingDirectory: URL?
    ) throws -> [String: String] {
        guard let mergedJSONURL else { return [:] }
        guard let profilesDirectory else { return [:] }

        let store = SpeakerProfileStore(directory: profilesDirectory)
        let profiles = try store.listProfiles()
        guard !profiles.isEmpty else { return [:] }

        // Decode the merged transcript and group segments by speaker_id,
        // recording each speaker's first-segment source track.
        let transcriptData = try Data(contentsOf: mergedJSONURL)
        let transcript = try JSONDecoder().decode(TranscriptJSON.self, from: transcriptData)

        var sourceBySpeaker: [String: String] = [:]
        for segment in transcript.segments {
            guard let speakerID = segment.speakerID else { continue }
            if sourceBySpeaker[speakerID] == nil, let source = segment.source {
                sourceBySpeaker[speakerID] = source
            }
        }
        guard !sourceBySpeaker.isEmpty else { return [:] }

        // Build one candidate per speaker: extract an embedding from that
        // speaker's source track (filtered to their segments), then match.
        var candidates: [SpeakerCandidateInput] = []
        for (speakerID, source) in sourceBySpeaker.sorted(by: { $0.key < $1.key }) {
            let sourceAudio: URL
            let sourceJSON: URL?
            switch source {
            case "mic":
                sourceAudio = micURL
                sourceJSON = micJSONURL
            case "system":
                sourceAudio = systemURL
                sourceJSON = systemJSONURL
            default:
                continue
            }
            guard let sourceJSON, FileManager.default.fileExists(atPath: sourceJSON.path) else {
                continue
            }

            let extraction: SpeakerExtractionResult
            do {
                extraction = try PythonSpeakerIdentifier.extractSpeakerSegments(
                    audioPath: sourceAudio.path,
                    segmentsJSONPath: sourceJSON.path,
                    speakerID: speakerID,
                    provider: provider,
                    minimumSpeechSeconds: minSpeechSeconds,
                    workingDirectory: workingDirectory
                )
            } catch {
                FileHandle.standardError.write("[identify] extraction failed for Speaker \(speakerID) (\(error)); skipping.\n".data(using: .utf8)!)
                candidates.append(SpeakerCandidateInput(speakerId: speakerID, extraction: nil, matchResult: nil))
                continue
            }

            // Matching is only meaningful with a usable embedding.
            var matchResult: SpeakerMatchResult?
            if extraction.isOK {
                matchResult = try? SpeakerCLISupport.match(
                    extraction: extraction,
                    profiles: profiles,
                    threshold: threshold,
                    margin: margin,
                    workingDirectory: workingDirectory
                )
            }
            candidates.append(SpeakerCandidateInput(speakerId: speakerID, extraction: extraction, matchResult: matchResult))
        }

        let assignments = SpeakerLabelResolver.resolve(candidates: candidates, hasProfiles: !profiles.isEmpty)

        // Keep only confident matches; unmatched speakers stay anonymous by
        // being absent from the returned map.
        var speakerNames: [String: String] = [:]
        for (speakerID, assignment) in assignments where assignment.matchStatus == SpeakerMatchStatus.matched {
            speakerNames[speakerID] = assignment.displayName
        }
        return speakerNames
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

    @Option(name: .long, help: "Mixing strategy for mixed.wav in meeting mode: raw or balanced (default: balanced).")
    public var mixMode: MixModeArgument = .balanced

    @Option(name: .long, help: "Meeting transcription strategy: separate transcribes mic/system independently and merges them; mixed preserves legacy single-pass mixed.wav transcription (default: separate).")
    public var meetingTranscription: MeetingTranscriptionModeArgument = .separate

    @Flag(name: .long, help: "Diarise each meeting track: assign Speaker 0, Speaker 1... by clustering voice embeddings (meeting separate mode only).")
    public var diarize: Bool = false

    @Option(name: .long, help: "Force exactly N speakers per track.")
    public var diarizeNumSpeakers: Int?

    @Option(name: .long, help: "Cosine-distance cut for auto speaker count (default 0.15).")
    public var diarizeDistanceThreshold: Double?

    @Option(name: .long, help: "Path to stt config JSON (for speaker provider).")
    public var config: String?

    @Option(name: .long, help: "Embedding provider override.")
    public var diarizeProvider: String?

    @Option(name: .long, help: "Python backend dir for the diarisation step (speechbrain needs the Python 3.11 runtime venv, e.g. 'runtime/'; defaults to the ASR backend).")
    public var diarizePythonBackend: String?

    @Flag(name: .long, help: "After diarisation, match speakers against enrolled profiles and relabel with names (meeting separate mode only; requires --diarize).")
    public var identify: Bool = false

    @Option(name: .long, help: "Override the configured speaker profiles directory (for --identify).")
    public var identifyProfilesDir: String?

    @Option(name: .long, help: "Minimum match confidence for --identify (default 0.78).")
    public var identifyThreshold: Double?

    @Option(name: .long, help: "Minimum margin over runner-up for --identify (default 0.05).")
    public var identifyMargin: Double?

    @Option(name: .long, help: "Path to the Python backend for speaker extraction/matching (runtime/ venv).")
    public var identifyPythonBackend: String?

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
        if diarizeNumSpeakers != nil && diarizeDistanceThreshold != nil {
            throw ValidationError("--diarize-num-speakers and --diarize-distance-threshold are mutually exclusive.")
        }
        if identify && !diarize {
            throw ValidationError("--identify requires --diarize.")
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
            switch meetingTranscription {
            case .separate:
                print("Note: meeting pipeline records separate mic/system tracks, transcribes both independently, then merges by timestamp.")
            case .mixed:
                print("Note: meeting pipeline uses legacy mixed.wav transcription (falls back to mic.wav if mixing fails).")
            }
        }

        do {
            var record = Record()
            record.mode = mode
            record.inputDevice = inputDevice
            record.duration = duration
            record.failIfEmpty = failIfEmpty
            var meetingMixNote: String?
            var meetingDriftNote: String?
            if mode == .meeting {
                record.outputDir = runDir.path
            } else {
                record.output = audioToTranscribeURL.path
            }
            try record.run()

            if mode == .meeting {
                let selection = Self.resolveMeetingAudioSource(micURL: micURL, systemURL: systemURL, mixedURL: mixedURL, mode: mixMode.mixMode)
                outputURLs = selection.outputURLs
                state.outputPaths = outputURLs.map(\.path)
                meetingMixNote = selection.note
                meetingDriftNote = selection.driftNote
                if let note = selection.note {
                    print("[NOTE] \(note)")
                } else if let driftNote = selection.driftNote {
                    print("[NOTE] \(driftNote)")
                }

                switch meetingTranscription {
                case .separate:
                    print("Transcribing meeting tracks separately: \(micURL.path) + \(systemURL.path)")
                    var diarizationConfig: MeetingDiarizationConfig?
                    var identifyConfig: MeetingIdentificationConfig?
                    if diarize {
                        let sttConfig = try STTConfigLoader.load(explicitPath: config)
                        let provider = SpeakerCLISupport.resolvedProvider(cliValue: diarizeProvider, config: sttConfig)
                        let diarizeBackend = try Transcribe.resolvePythonBackendDirectory(overridePath: diarizePythonBackend)
                        diarizationConfig = MeetingDiarizationConfig(
                            provider: provider,
                            numSpeakers: diarizeNumSpeakers,
                            distanceThreshold: diarizeDistanceThreshold,
                            workingDirectory: diarizeBackend
                        )
                        if identify {
                            identifyConfig = MeetingIdentificationConfig(
                                profilesDirectory: SpeakerCLISupport.resolveProfilesDirectory(config: sttConfig, overridePath: identifyProfilesDir),
                                matchThreshold: SpeakerCLISupport.resolvedThreshold(cliValue: identifyThreshold, config: sttConfig),
                                matchMargin: SpeakerCLISupport.resolvedMargin(cliValue: identifyMargin, config: sttConfig),
                                workingDirectory: try Transcribe.resolvePythonBackendDirectory(overridePath: identifyPythonBackend),
                                provider: SpeakerCLISupport.resolvedProvider(cliValue: nil, config: sttConfig),
                                minSpeechSeconds: SpeakerCLISupport.resolvedMinimumSpeechSeconds(cliValue: nil, config: sttConfig)
                            )
                        }
                    }
                    let result = try TranscribeMeeting.transcribeMeeting(
                        micURL: micURL,
                        systemURL: systemURL,
                        outputTextURL: transcriptTextURL,
                        outputJSONURL: transcriptJSONURL,
                        device: device.rawValue,
                        workingDirectory: backendDir,
                        timeout: transcribeTimeout,
                        modelPath: model,
                        maxNewTokens: maxNewTokens,
                        diarize: diarize,
                        diarization: diarizationConfig,
                        identify: identify,
                        profilesDirectory: identifyConfig?.profilesDirectory,
                        matchThreshold: identifyConfig?.matchThreshold ?? 0.78,
                        matchMargin: identifyConfig?.matchMargin ?? 0.05,
                        identifyWorkingDirectory: identifyConfig?.workingDirectory,
                        identifyProvider: identifyConfig?.provider ?? "speechbrain",
                        identifyMinSpeechSeconds: identifyConfig?.minSpeechSeconds ?? 8.0
                    )
                    print(result.text)
                    state.transcribedAudioPath = result.transcribedAudioPaths.joined(separator: "+")
                    state.transcribedAudioPaths = result.transcribedAudioPaths
                    let meetingNotes = [meetingMixNote, meetingDriftNote].compactMap { $0 }.joined(separator: "\n")
                    persistState(notes: meetingNotes.isEmpty ? nil : meetingNotes, backend: result.backend)
                case .mixed:
                    audioToTranscribeURL = selection.audioToTranscribeURL
                    state.transcribedAudioPath = audioToTranscribeURL.path
                    state.transcribedAudioPaths = [audioToTranscribeURL.path]
                    print("Transcribing mixed meeting audio: \(audioToTranscribeURL.path)")
                    try Self.requireTranscribableAudio(at: audioToTranscribeURL)
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
                    let meetingNotes = [meetingMixNote, meetingDriftNote].compactMap { $0 }.joined(separator: "\n")
                    persistState(notes: meetingNotes.isEmpty ? nil : meetingNotes, backend: result.backend)
                }
            } else {
                try Self.requireTranscribableAudio(at: audioToTranscribeURL)

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
                persistState(backend: result.backend)
            }
        } catch {
            persistState(notes: "Pipeline failed: \(error.localizedDescription)")
            throw error
        }
    }

    public struct MeetingAudioSelection: Equatable {
        public let audioToTranscribeURL: URL
        public let outputURLs: [URL]
        public let note: String?
        public let driftNote: String?
    }

    public static func requireTranscribableAudio(at url: URL) throws {
        _ = try Paths.requireNonEmptyFile(url.path)
    }

    public static func resolveMeetingAudioSource(micURL: URL, systemURL: URL, mixedURL: URL, mode: MixMode = .balanced) -> MeetingAudioSelection {
        do {
            _ = try WAVMixer.mixFiles(micURL, systemURL, outputURL: mixedURL, mode: mode)
            let driftNote = WAVMixer.driftWarning(lhsURL: micURL, rhsURL: systemURL)
            return MeetingAudioSelection(
                audioToTranscribeURL: mixedURL,
                outputURLs: [micURL, systemURL, mixedURL],
                note: nil,
                driftNote: driftNote
            )
        } catch {
            return MeetingAudioSelection(
                audioToTranscribeURL: micURL,
                outputURLs: [micURL, systemURL],
                note: "Mixed track unavailable (\(error.localizedDescription)); transcribing mic.wav instead.",
                driftNote: nil
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

    @Option(name: .long, help: "Mixing strategy: raw or balanced (default: balanced).")
    public var mixMode: MixModeArgument = .balanced

    public init() {}

    public func run() throws {
        let firstURL = try Paths.requireExistingFile(firstAudioPath)
        let secondURL = try Paths.requireExistingFile(secondAudioPath)
        let outputURL = output.map { URL(fileURLWithPath: $0) }
            ?? firstURL.deletingLastPathComponent().appendingPathComponent("mixed.wav")

        let result = try WAVMixer.mixFiles(firstURL, secondURL, outputURL: outputURL, mode: mixMode.mixMode)
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
            let bundleID = bundleID ?? Bundle.main.bundleIdentifier ?? "com.larrysong.stt"
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

// MARK: - shared speaker-id CLI helpers

public enum SpeakerCLISupport {
    public static func resolveProfilesDirectory(config: STTConfig, overridePath: String?) -> URL {
        if let overridePath, !overridePath.isEmpty {
            return URL(fileURLWithPath: overridePath, isDirectory: true)
        }
        return Paths.speakerProfilesDirectory(config: config)
    }

    public static func resolvedProvider(cliValue: String?, config: STTConfig) -> String {
        cliValue.flatMap { $0.isEmpty ? nil : $0 } ?? config.speakerIdentification.provider
    }

    public static func resolvedMinimumSpeechSeconds(cliValue: Double?, config: STTConfig) -> Double {
        cliValue ?? config.speakerIdentification.minimumSpeechSeconds
    }

    public static func resolvedThreshold(cliValue: Double?, config: STTConfig) -> Double {
        cliValue ?? config.speakerIdentification.matchThreshold
    }

    public static func resolvedMargin(cliValue: Double?, config: STTConfig) -> Double {
        cliValue ?? config.speakerIdentification.matchMargin
    }

    /// Writes flattened profiles + a candidate embedding to temp files and
    /// runs `PythonSpeakerIdentifier.match`, cleaning up the temp files
    /// afterward regardless of outcome.
    public static func match(extraction: SpeakerExtractionResult,
                       profiles: [SpeakerProfile],
                       threshold: Double,
                       margin: Double,
                       workingDirectory: URL?) throws -> SpeakerMatchResult {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent("stt-speaker-match-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let profilesURL = tmpDir.appendingPathComponent("profiles.json")
        let candidateURL = tmpDir.appendingPathComponent("candidate.json")
        try PythonSpeakerIdentifier.writeFlattenedProfiles(profiles, to: profilesURL)
        try PythonSpeakerIdentifier.writeCandidate(extraction, to: candidateURL)

        return try PythonSpeakerIdentifier.match(
            candidateJSONPath: candidateURL.path,
            profilesJSONPath: profilesURL.path,
            threshold: threshold,
            margin: margin,
            workingDirectory: workingDirectory
        )
    }
}

// MARK: - stt speaker

public struct Speaker: ParsableCommand {
    public static let configuration = CommandConfiguration(
        abstract: "Manage local speaker enrollment profiles (stored locally; see privacy notes in README).",
        subcommands: [Enroll.self, ListProfiles.self, Rename.self, Remove.self]
    )

    public init() {}

    public struct Enroll: ParsableCommand {
        public static let configuration = CommandConfiguration(abstract: "Enroll a speaker profile from an audio sample or a short mic recording.")

        @Argument(help: "Display name for this speaker.")
        public var displayName: String

        @Option(name: .long, help: "Path to an existing clean audio sample.")
        public var audio: String?

        @Option(name: .long, help: "Record a mic sample for this many seconds instead of using --audio.")
        public var duration: Double?

        @Flag(name: .long, help: "Replace an existing profile with this display name.")
        public var replace: Bool = false

        @Option(name: .long, help: "Embedding provider to use (default: from config, else speechbrain).")
        public var provider: String?

        @Option(name: .long, help: "Minimum total speech seconds required (default: from config, else 8.0).")
        public var minimumSpeechSeconds: Double?

        @Option(name: .long, help: "Path to stt config JSON.")
        public var config: String?

        @Option(name: .long, help: "Override the configured speaker profiles directory.")
        public var profilesDir: String?

        @Option(name: .long, help: "Path to the Python backend directory containing stt_vibevoice.")
        public var pythonBackend: String?

        public init() {}

        public func run() throws {
            guard (audio != nil) != (duration != nil) else {
                throw ValidationError("Exactly one of --audio or --duration is required.")
            }
            if let duration, duration <= 0 {
                throw ValidationError("--duration must be greater than 0 seconds")
            }

            let sttConfig = try STTConfigLoader.load(explicitPath: config)
            let profilesDirectory = SpeakerCLISupport.resolveProfilesDirectory(config: sttConfig, overridePath: profilesDir)
            let store = SpeakerProfileStore(directory: profilesDirectory)
            let backendDir = try Transcribe.resolvePythonBackendDirectory(overridePath: pythonBackend)
            let providerName = SpeakerCLISupport.resolvedProvider(cliValue: provider, config: sttConfig)
            let minSeconds = SpeakerCLISupport.resolvedMinimumSpeechSeconds(cliValue: minimumSpeechSeconds, config: sttConfig)

            let sampleAudioURL: URL
            var temporaryRecordingURL: URL?
            if let audio {
                sampleAudioURL = try Paths.requireNonEmptyFile(audio)
            } else {
                let recordedURL = FileManager.default.temporaryDirectory.appendingPathComponent("stt-enroll-\(UUID().uuidString).wav")
                temporaryRecordingURL = recordedURL
                let recorder = MicRecorder()
                try recorder.start(outputURL: recordedURL, inputDeviceID: nil)
                print("Recording \(String(format: "%.1f", duration ?? 0))s enrollment sample for \"\(displayName)\"; speak clearly...")
                Thread.sleep(forTimeInterval: duration ?? 0)
                let result = try recorder.stop()
                sampleAudioURL = result.outputURL
                if let warning = result.emptyAudioWarning { print(warning) }
            }
            defer {
                if let temporaryRecordingURL { try? FileManager.default.removeItem(at: temporaryRecordingURL) }
            }

            let extraction = try PythonSpeakerIdentifier.extractWholeAudio(
                audioPath: sampleAudioURL.path,
                provider: providerName,
                minimumSpeechSeconds: minSeconds,
                workingDirectory: backendDir
            )
            guard extraction.isOK, let embedding = extraction.embedding, let model = extraction.model else {
                throw ValidationError("Enrollment sample has only \(String(format: "%.1f", extraction.durationSeconds))s of usable audio; at least \(String(format: "%.1f", minSeconds))s is required.")
            }

            let existingProfile = try? store.findByName(displayName)
            if existingProfile != nil, !replace {
                throw ValidationError("Speaker \"\(displayName)\" already exists; pass --replace to replace its enrollment.")
            }

            let profileID = existingProfile?.id ?? UUID()
            let sampleDirectory = store.sampleDirectory(forProfileID: profileID)
            try Paths.ensureDirectoryExists(sampleDirectory)
            let sampleFileName = "\(Paths.timestampToken()).wav"
            let destinationSampleURL = sampleDirectory.appendingPathComponent(sampleFileName)
            try? FileManager.default.removeItem(at: destinationSampleURL)
            try FileManager.default.copyItem(at: sampleAudioURL, to: destinationSampleURL)
            let relativeSamplePath = "samples/\(profileID.uuidString)/\(sampleFileName)"

            let now = Date()
            let profile = SpeakerProfile(
                id: profileID,
                displayName: displayName,
                createdAt: existingProfile?.createdAt ?? now,
                updatedAt: now,
                embeddingProvider: providerName,
                embeddingModel: model,
                embedding: embedding,
                samplePaths: replace ? [relativeSamplePath] : (existingProfile?.samplePaths ?? []) + [relativeSamplePath],
                sampleDurationSeconds: extraction.durationSeconds,
                notes: existingProfile?.notes
            )
            try store.save(profile)

            print("Enrolled speaker \"\(displayName)\" (id: \(profileID.uuidString)) using provider \(providerName).")
        }
    }

    public struct ListProfiles: ParsableCommand {
        public static let configuration = CommandConfiguration(commandName: "list", abstract: "List enrolled speaker profiles.")

        @Option(name: .long, help: "Path to stt config JSON.")
        public var config: String?

        @Option(name: .long, help: "Override the configured speaker profiles directory.")
        public var profilesDir: String?

        public init() {}

        public func run() throws {
            let sttConfig = try STTConfigLoader.load(explicitPath: config)
            let profilesDirectory = SpeakerCLISupport.resolveProfilesDirectory(config: sttConfig, overridePath: profilesDir)
            let store = SpeakerProfileStore(directory: profilesDirectory)
            let summaries = try store.listSummaries()

            if summaries.isEmpty {
                print("No speaker profiles enrolled. Use `stt speaker enroll <name> --audio <file>` to add one.")
                return
            }

            print("Enrolled speaker profiles (\(profilesDirectory.path)):")
            for summary in summaries {
                print("  [\(summary.id.uuidString)] \(summary.displayName) — provider=\(summary.embeddingProvider) samples=\(summary.sampleCount) updated=\(summary.updatedAt)")
            }
        }
    }

    public struct Rename: ParsableCommand {
        public static let configuration = CommandConfiguration(abstract: "Rename an enrolled speaker profile.")

        @Argument(help: "Existing display name.")
        public var existingName: String

        @Argument(help: "New display name.")
        public var newName: String

        @Option(name: .long, help: "Path to stt config JSON.")
        public var config: String?

        @Option(name: .long, help: "Override the configured speaker profiles directory.")
        public var profilesDir: String?

        public init() {}

        public func run() throws {
            let sttConfig = try STTConfigLoader.load(explicitPath: config)
            let profilesDirectory = SpeakerCLISupport.resolveProfilesDirectory(config: sttConfig, overridePath: profilesDir)
            let store = SpeakerProfileStore(directory: profilesDirectory)
            let profile = try store.findByName(existingName)
            try store.rename(id: profile.id, to: newName)
            print("Renamed \"\(existingName)\" to \"\(newName)\" (id: \(profile.id.uuidString)).")
        }
    }

    public struct Remove: ParsableCommand {
        public static let configuration = CommandConfiguration(abstract: "Remove an enrolled speaker profile and its stored samples.")

        @Argument(help: "Display name of the profile to remove.")
        public var displayName: String

        @Flag(name: .long, help: "Confirm removal (required; there is no interactive prompt).")
        public var yes: Bool = false

        @Option(name: .long, help: "Path to stt config JSON.")
        public var config: String?

        @Option(name: .long, help: "Override the configured speaker profiles directory.")
        public var profilesDir: String?

        public init() {}

        public func run() throws {
            guard yes else {
                throw ValidationError("Refusing to remove speaker \"\(displayName)\" without --yes.")
            }
            let sttConfig = try STTConfigLoader.load(explicitPath: config)
            let profilesDirectory = SpeakerCLISupport.resolveProfilesDirectory(config: sttConfig, overridePath: profilesDir)
            let store = SpeakerProfileStore(directory: profilesDirectory)
            let profile = try store.findByName(displayName)
            try store.delete(id: profile.id)
            print("Removed speaker \"\(displayName)\" (id: \(profile.id.uuidString)) and its stored samples.")
        }
    }
}

// MARK: - stt identify

public struct Identify: ParsableCommand {
    public static let configuration = CommandConfiguration(abstract: "Identify a single audio clip against enrolled speaker profiles.")

    @Argument(help: "Path to the audio clip to identify.")
    public var audioPath: String

    @Option(name: .long, help: "Embedding provider to use (default: from config, else speechbrain).")
    public var provider: String?

    @Option(name: .long, help: "Minimum match confidence required (default: from config, else 0.78).")
    public var threshold: Double?

    @Option(name: .long, help: "Minimum margin over the runner-up required (default: from config, else 0.05).")
    public var margin: Double?

    @Option(name: .long, help: "Minimum total speech seconds required (default: from config, else 8.0).")
    public var minimumSpeechSeconds: Double?

    @Option(name: .long, help: "Path to stt config JSON.")
    public var config: String?

    @Option(name: .long, help: "Override the configured speaker profiles directory.")
    public var profilesDir: String?

    @Option(name: .long, help: "Path to the Python backend directory containing stt_vibevoice.")
    public var pythonBackend: String?

    @Option(name: .long, help: "Path to write the identification result as JSON.")
    public var json: String?

    public init() {}

    public func run() throws {
        let sttConfig = try STTConfigLoader.load(explicitPath: config)
        let profilesDirectory = SpeakerCLISupport.resolveProfilesDirectory(config: sttConfig, overridePath: profilesDir)
        let store = SpeakerProfileStore(directory: profilesDirectory)
        let backendDir = try Transcribe.resolvePythonBackendDirectory(overridePath: pythonBackend)
        let providerName = SpeakerCLISupport.resolvedProvider(cliValue: provider, config: sttConfig)
        let minSeconds = SpeakerCLISupport.resolvedMinimumSpeechSeconds(cliValue: minimumSpeechSeconds, config: sttConfig)
        let thresholdValue = SpeakerCLISupport.resolvedThreshold(cliValue: threshold, config: sttConfig)
        let marginValue = SpeakerCLISupport.resolvedMargin(cliValue: margin, config: sttConfig)

        let audioURL = try Paths.requireNonEmptyFile(audioPath)
        let profiles = try store.listProfiles()

        let extraction = try PythonSpeakerIdentifier.extractWholeAudio(
            audioPath: audioURL.path,
            provider: providerName,
            minimumSpeechSeconds: minSeconds,
            workingDirectory: backendDir
        )

        let candidate = SpeakerCandidateInput(
            speakerId: "0",
            extraction: extraction,
            matchResult: nil
        )

        let assignment: SpeakerLabelAssignment
        if !extraction.isOK {
            assignment = SpeakerLabelResolver.resolve(candidates: [candidate], hasProfiles: !profiles.isEmpty)["0"]
                ?? SpeakerLabelAssignment(displayName: "Speaker 0", profileId: nil, confidence: nil, margin: nil, matchStatus: SpeakerMatchStatus.tooShort)
        } else if profiles.isEmpty {
            assignment = SpeakerLabelResolver.resolve(candidates: [candidate], hasProfiles: false)["0"]
                ?? SpeakerLabelAssignment(displayName: "Speaker 0", profileId: nil, confidence: nil, margin: nil, matchStatus: SpeakerMatchStatus.noProfiles)
        } else {
            let matchResult = try SpeakerCLISupport.match(
                extraction: extraction,
                profiles: profiles,
                threshold: thresholdValue,
                margin: marginValue,
                workingDirectory: backendDir
            )
            let matchedCandidate = SpeakerCandidateInput(speakerId: "0", extraction: extraction, matchResult: matchResult)
            assignment = SpeakerLabelResolver.resolve(candidates: [matchedCandidate], hasProfiles: true)["0"]
                ?? SpeakerLabelAssignment(displayName: "Speaker 0", profileId: nil, confidence: nil, margin: nil, matchStatus: SpeakerMatchStatus.belowThreshold)
        }

        if assignment.matchStatus == SpeakerMatchStatus.matched {
            print("Identified as \"\(assignment.displayName)\" (confidence: \(String(format: "%.3f", assignment.confidence ?? 0)), status: \(assignment.matchStatus))")
        } else {
            print("Not confidently identified (status: \(assignment.matchStatus)). Staying anonymous.")
        }

        if let json {
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
            let data = try encoder.encode(assignment)
            try data.write(to: URL(fileURLWithPath: json), options: .atomic)
        }
    }
}

// MARK: - stt diarize

public struct Diarize: ParsableCommand {
    public static let configuration = CommandConfiguration(
        abstract: "Diarise a transcript: assign a speaker_id to each segment by clustering per-segment embeddings."
    )

    @Option(name: .long, help: "Path to the source WAV file.")
    public var audio: String

    @Option(name: .long, help: "Path to the transcript JSON to read and update in place.")
    public var transcript: String

    @Option(name: .long, help: "Embedding provider to use (default: from config, else speechbrain).")
    public var provider: String?

    @Option(name: .long, help: "Force exactly N speakers (mutually exclusive with --distance-threshold).")
    public var numSpeakers: Int?

    @Option(name: .long, help: "Cosine-distance cut for auto speaker count (mutually exclusive with --num-speakers; default 0.15).")
    public var distanceThreshold: Double?

    @Option(name: .long, help: "Minimum speech seconds for a segment to be clustered (default 1.0).")
    public var minSpeechSeconds: Double?

    @Option(name: .long, help: "Add N to every assigned speaker id (for unifying mic/system namespaces; default 0).")
    public var speakerIdOffset: Int?

    @Option(name: .long, help: "Path to stt config JSON.")
    public var config: String?

    @Option(name: .long, help: "Path to the Python backend directory containing stt_vibevoice.")
    public var pythonBackend: String?

    public init() {}

    public func run() throws {
        guard !(numSpeakers != nil && distanceThreshold != nil) else {
            throw ValidationError("--num-speakers and --distance-threshold are mutually exclusive.")
        }

        let sttConfig = try STTConfigLoader.load(explicitPath: config)
        let backendDir = try Transcribe.resolvePythonBackendDirectory(overridePath: pythonBackend)
        let providerName = SpeakerCLISupport.resolvedProvider(cliValue: provider, config: sttConfig)

        let audioURL = try Paths.requireNonEmptyFile(audio)
        let transcriptURL = try Paths.requireNonEmptyFile(transcript)

        let result = try PythonDiarizer.diarize(
            audioPath: audioURL.path,
            transcriptJSONPath: transcriptURL.path,
            provider: providerName,
            numSpeakers: numSpeakers,
            distanceThreshold: distanceThreshold,
            minSpeechSeconds: minSpeechSeconds,
            workingDirectory: backendDir,
            speakerIdOffset: speakerIdOffset ?? 0
        )

        // Load the existing transcript, copy diarized speaker ids back by index,
        // and write it back in place preserving all top-level fields.
        try TranscriptMerger.applyDiarizationToFile(transcriptURL: transcriptURL, result: result)

        print("Diarized \(result.numSpeakers) speakers:")
        for speaker in result.speakers {
            print("[\(speaker.id)] \(speaker.segmentCount) segments, \(String(format: "%.3f", speaker.totalSpeechSeconds))s speech")
        }
    }
}

// MARK: - stt name-speakers

/// Interactive human-in-the-loop speaker labeling for a diarised transcript.
///
/// For each detected speaker this plays a short sample of their voice, prompts
/// for a name, and enrolls a `SpeakerProfile` from their concatenated speech.
/// This is the labeling step between diarisation (`stt diarize`) and matching
/// (`stt identify`).
public struct NameSpeakers: ParsableCommand {
    public static let configuration = CommandConfiguration(
        commandName: "name-speakers",
        abstract: "Interactively name and enroll each speaker in a diarised transcript."
    )

    @Option(name: .long, help: "Path to the diarised merged transcript JSON (segments with speaker_id + source).")
    public var transcript: String

    @Option(name: .long, help: "Path to the mic-track source WAV (for speakers whose source is \"mic\").")
    public var mic: String?

    @Option(name: .long, help: "Path to the system-track source WAV (for speakers whose source is \"system\").")
    public var system: String?

    @Option(name: .long, help: "Embedding provider to use (default: from config, else speechbrain).")
    public var provider: String?

    @Option(name: .long, help: "How many seconds of audio to play per speaker (default 12).")
    public var previewSeconds: Double = 12

    @Option(name: .long, help: "Cap the enrolled sample clip to this many seconds of speech (default 60; 0 = all).")
    public var sampleSeconds: Double = 60

    @Option(name: .long, help: "Minimum total speech seconds required to enroll a speaker (default: from config, else 8.0).")
    public var minimumSpeechSeconds: Double?

    @Flag(name: .long, help: "Play and prompt but do not save profiles (dry run).")
    public var noEnroll: Bool = false

    @Flag(name: .long, help: "Disable loudness normalization of preview clips (on by default so quiet system-audio tracks play as loud as mic).")
    public var noNormalize: Bool = false

    @Option(name: .long, help: "Path to stt config JSON.")
    public var config: String?

    @Option(name: .long, help: "Override the configured speaker profiles directory.")
    public var profilesDir: String?

    @Option(name: .long, help: "Path to the Python backend directory containing stt_vibevoice.")
    public var pythonBackend: String?

    public init() {}

    public func run() throws {
        let sttConfig = try STTConfigLoader.load(explicitPath: config)
        let profilesDirectory = SpeakerCLISupport.resolveProfilesDirectory(config: sttConfig, overridePath: profilesDir)
        let store = SpeakerProfileStore(directory: profilesDirectory)
        let backendDir = try Transcribe.resolvePythonBackendDirectory(overridePath: pythonBackend)
        let providerName = SpeakerCLISupport.resolvedProvider(cliValue: provider, config: sttConfig)
        let minSeconds = SpeakerCLISupport.resolvedMinimumSpeechSeconds(cliValue: minimumSpeechSeconds, config: sttConfig)

        let transcriptURL = try Paths.requireNonEmptyFile(transcript)

        let micURL: URL? = mic.flatMap { try? Paths.requireNonEmptyFile($0) }
        let systemURL: URL? = system.flatMap { try? Paths.requireNonEmptyFile($0) }

        // Decode the transcript and group segments by speaker_id.
        let transcriptData = try Data(contentsOf: transcriptURL)
        let transcriptJSON = try JSONDecoder().decode(TranscriptJSON.self, from: transcriptData)

        // Tally per-speaker speech seconds and track the source track of each
        // speaker's first segment (speakers do not cross mic/system tracks).
        var speechSecondsBySpeaker: [String: Double] = [:]
        var sourceBySpeaker: [String: String] = [:]
        for segment in transcriptJSON.segments {
            guard let speakerID = segment.speakerID else { continue }
            speechSecondsBySpeaker[speakerID, default: 0] += max(0, segment.endTime - segment.startTime)
            if sourceBySpeaker[speakerID] == nil, let source = segment.source {
                sourceBySpeaker[speakerID] = source
            }
        }

        // Most speech first so the dominant speakers get named first.
        let speakers = speechSecondsBySpeaker.keys.sorted { lhs, rhs in
            let lhsSeconds = speechSecondsBySpeaker[lhs] ?? 0
            let rhsSeconds = speechSecondsBySpeaker[rhs] ?? 0
            if lhsSeconds != rhsSeconds { return lhsSeconds > rhsSeconds }
            return lhs < rhs
        }

        guard !speakers.isEmpty else {
            print("No speakers with speaker_id found in \(transcript); run `stt diarize` first.")
            return
        }

        let summary = speakers.map { speakerID -> String in
            let seconds = speechSecondsBySpeaker[speakerID] ?? 0
            let source = sourceBySpeaker[speakerID] ?? "unknown"
            return "Speaker \(speakerID) (\(source), \(Int(seconds.rounded()))s)"
        }.joined(separator: ", ")
        print("Found \(speakers.count) speakers: \(summary)")

        var enrolledCount = 0
        var skippedCount = 0

        for speakerID in speakers {
            let source = sourceBySpeaker[speakerID] ?? "unknown"
            let sourceWavURL: URL? = source == "mic" ? micURL : (source == "system" ? systemURL : nil)

            guard let sourceWavURL else {
                print("Skipping Speaker \(speakerID): --mic/--system not provided (source=\(source)).")
                skippedCount += 1
                continue
            }

            // Build a playable clip of this speaker's concatenated speech
            // (fast; no ML) via the python `concatenate` subcommand.
            let clipDir = FileManager.default.temporaryDirectory
                .appendingPathComponent("stt-name-speakers-\(UUID().uuidString)", isDirectory: true)
            try FileManager.default.createDirectory(at: clipDir, withIntermediateDirectories: true)
            defer { try? FileManager.default.removeItem(at: clipDir) }

            let clipURL = clipDir.appendingPathComponent("speaker-\(speakerID).wav")

            let concat: SpeakerConcatenateResult
            do {
                concat = try PythonSpeakerIdentifier.concatenate(
                    audioPath: sourceWavURL.path,
                    segmentsJSONPath: transcriptURL.path,
                    speakerID: speakerID,
                    outPath: clipURL.path,
                    workingDirectory: backendDir,
                    maxSeconds: sampleSeconds,
                    normalize: !noNormalize
                )
            } catch {
                print("Speaker \(speakerID): no usable speech segments, skipping.")
                skippedCount += 1
                continue
            }

            // Interactive play -> name loop (allows replay).
            var chosenName: String?
            speakerLoop: while true {
                print("\nSpeaker \(speakerID) (\(source), \(String(format: "%.0f", concat.durationSeconds))s, \(concat.segmentCount) segments)")
                print("Playing \(String(format: "%.0f", previewSeconds))s sample...")
                Self.playAudioSample(at: clipURL.path, seconds: previewSeconds)
                print("[name] enroll as  |  [r] replay  |  [s] skip")
                guard let line = readLine()?.trimmingCharacters(in: .whitespaces), !line.isEmpty else {
                    print("Skipping Speaker \(speakerID) (no name given).")
                    break speakerLoop
                }
                let normalized = line.lowercased()
                if normalized == "s" || normalized == "skip" {
                    print("Skipping Speaker \(speakerID).")
                    break speakerLoop
                } else if normalized == "r" || normalized == "replay" {
                    continue speakerLoop
                } else {
                    chosenName = line
                    break speakerLoop
                }
            }

            guard let name = chosenName else {
                skippedCount += 1
                continue
            }

            // A profile with this name already exists: do not overwrite.
            let existing = try? store.findByName(name)
            if existing != nil {
                print("Speaker \"\(name)\" already enrolled; skipping enrollment (use `stt speaker remove` first to replace).")
                skippedCount += 1
                continue
            }

            if noEnroll {
                print("Would enroll \"\(name)\" from \(String(format: "%.0f", concat.durationSeconds))s of speech.")
                enrolledCount += 1
                continue
            }

            // Extract the embedding from this speaker's speech (ML step).
            let extraction = try PythonSpeakerIdentifier.extractSpeakerSegments(
                audioPath: sourceWavURL.path,
                segmentsJSONPath: transcriptURL.path,
                speakerID: speakerID,
                provider: providerName,
                minimumSpeechSeconds: minSeconds,
                workingDirectory: backendDir
            )
            guard extraction.isOK, let embedding = extraction.embedding, let model = extraction.model else {
                print("Speaker \(speakerID): extraction failed (only \(String(format: "%.0f", concat.durationSeconds))s usable); skipping.")
                skippedCount += 1
                continue
            }

            // Copy the playable clip into the profile's sample directory and
            // build a fresh profile (new id; never replace semantics).
            let profileID = UUID()
            let sampleDirectory = store.sampleDirectory(forProfileID: profileID)
            try Paths.ensureDirectoryExists(sampleDirectory)
            let sampleFileName = "\(Paths.timestampToken()).wav"
            let destinationSampleURL = sampleDirectory.appendingPathComponent(sampleFileName)
            try? FileManager.default.removeItem(at: destinationSampleURL)
            try FileManager.default.copyItem(at: clipURL, to: destinationSampleURL)
            let relativeSamplePath = "samples/\(profileID.uuidString)/\(sampleFileName)"

            let now = Date()
            let profile = SpeakerProfile(
                id: profileID,
                displayName: name,
                createdAt: now,
                updatedAt: now,
                embeddingProvider: providerName,
                embeddingModel: model,
                embedding: embedding,
                samplePaths: [relativeSamplePath],
                sampleDurationSeconds: extraction.durationSeconds
            )
            try store.save(profile)

            print("Enrolled \"\(name)\" from \(String(format: "%.0f", concat.durationSeconds))s of speech (\(concat.segmentCount) segments).")
            enrolledCount += 1
        }

        print("\nDone: \(enrolledCount) enrolled, \(skippedCount) skipped.")
    }

    /// Plays the first `seconds` seconds of the sample at `path` using the
    /// system `afplay` tool. Failures (e.g. no audio device or afplay missing)
    /// print a warning with the clip path but never abort the command, so the
    /// user can still name/enroll a speaker without hearing the preview.
    private static func playAudioSample(at path: String, seconds: Double) {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/afplay")
        process.arguments = ["-t", String(format: "%.0f", seconds), path]
        do {
            try process.run()
            process.waitUntilExit()
        } catch {
            print("[warn] could not play sample (\(error.localizedDescription)). Clip is at: \(path)")
        }
    }
}
