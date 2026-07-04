import Foundation
import ArgumentParser
import sttCore

struct CheckFailure: Error, CustomStringConvertible {
    let message: String
    var description: String { message }
}

func check(_ condition: @autoclosure () -> Bool, _ message: String) throws {
    if !condition() { throw CheckFailure(message: message) }
}

func checkEqual<T: Equatable>(_ actual: T, _ expected: T, _ message: String) throws {
    if actual != expected {
        throw CheckFailure(message: "\(message): expected \(expected), got \(actual)")
    }
}

func runChecks() throws {
    let doctor = try Doctor.parse(["--python-backend", "/tmp/backend", "--require-backend-ready"])
    try checkEqual(doctor.pythonBackend, "/tmp/backend", "doctor python backend parses")
    try check(doctor.requireBackendReady, "doctor require-backend-ready parses")

    let resetHelp = try Permissions.ResetHelp.parse(["--bundle-id", "com.example.stt"])
    try checkEqual(resetHelp.bundleID, "com.example.stt", "permissions reset-help bundle-id parses")

    let record = try Record.parse(["--mode", "mic", "--output", "foo.wav", "--duration", "1.5", "--fail-if-empty"])
    try checkEqual(record.mode, .mic, "record mode parses")
    try checkEqual(record.output, "foo.wav", "record output parses")
    try checkEqual(record.duration, 1.5, "record duration parses")
    try check(record.failIfEmpty, "record fail-if-empty parses")

    let system = try Record.parse(["--mode", "system", "--input-device", "BlackHole 2ch"])
    try checkEqual(system.mode, .system, "system mode parses")
    try checkEqual(system.inputDevice, "BlackHole 2ch", "input device parses")

    let transcribe = try Transcribe.parse([
        "meeting.wav", "--output", "out.txt", "--json", "out.json", "--device", "gpu",
        "--timeout", "30", "--model", "custom/model", "--max-new-tokens", "2048",
        "--python-backend", "/tmp/backend", "--require-backend-ready"
    ])
    try checkEqual(transcribe.audioPath, "meeting.wav", "transcribe audio path parses")
    try checkEqual(transcribe.output, "out.txt", "transcribe output parses")
    try checkEqual(transcribe.json, "out.json", "transcribe json parses")
    try checkEqual(transcribe.device, .gpu, "transcribe device parses")
    try checkEqual(transcribe.timeout, 30, "transcribe timeout parses")
    try checkEqual(transcribe.model, "custom/model", "transcribe model parses")
    try checkEqual(transcribe.maxNewTokens, 2048, "transcribe max-new-tokens parses")
    try checkEqual(transcribe.pythonBackend, "/tmp/backend", "transcribe python backend parses")
    try check(transcribe.requireBackendReady, "transcribe require-backend-ready parses")

    let pipeline = try Pipeline.parse([
        "--mode", "meeting", "--name", "Customer Call", "--input-device", "BlackHole 2ch",
        "--duration", "5", "--fail-if-empty", "--transcribe-timeout", "120", "--device", "cpu",
        "--model", "custom/model", "--max-new-tokens", "2048", "--python-backend", "/tmp/backend",
        "--require-backend-ready"
    ])
    try checkEqual(pipeline.mode, .meeting, "pipeline mode parses")
    try checkEqual(pipeline.name, "Customer Call", "pipeline name parses")
    try checkEqual(pipeline.inputDevice, "BlackHole 2ch", "pipeline input device parses")
    try checkEqual(pipeline.duration, 5, "pipeline duration parses")
    try check(pipeline.failIfEmpty, "pipeline fail-if-empty parses")
    try checkEqual(pipeline.transcribeTimeout, 120, "pipeline timeout parses")
    try checkEqual(pipeline.device, .cpu, "pipeline transcribe device parses")
    try checkEqual(pipeline.model, "custom/model", "pipeline model parses")
    try checkEqual(pipeline.maxNewTokens, 2048, "pipeline max-new-tokens parses")
    try checkEqual(pipeline.pythonBackend, "/tmp/backend", "pipeline python backend parses")
    try check(pipeline.requireBackendReady, "pipeline require-backend-ready parses")

    let env = ["STT_HOME": "/tmp/stt-test-home"]
    try checkEqual(Paths.appSupportDirectory(environment: env).path, "/tmp/stt-test-home", "STT_HOME override works")
    try checkEqual(Paths.recordingsDirectory(environment: env).path, "/tmp/stt-test-home/recordings", "recordings dir nested")

    let header = WAVWriter.header(sampleRate: 16_000, channels: 1, bitDepth: 16, dataSize: 32_000)
    try checkEqual(header.count, 44, "WAV header size")
    try checkEqual(header.subdata(in: 0..<4), Data("RIFF".utf8), "WAV RIFF marker")
    try checkEqual(header.subdata(in: 8..<12), Data("WAVE".utf8), "WAV WAVE marker")
    try checkEqual(WAVWriter.totalFileSize(dataSize: 1_000), 1_044, "WAV total file size")

    let headerOnlyRecording = RecordingResult(
        outputURL: URL(fileURLWithPath: "/tmp/header-only.wav"),
        durationSeconds: 1,
        fileSizeBytes: 4096
    )
    try check(!headerOnlyRecording.likelyContainsAudioData, "header-only recording detected")
    try check(headerOnlyRecording.emptyAudioWarning?.contains("no audio frames") == true, "header-only recording warning")

    try check(BundleAttribution.isRunningFromAppBundle(bundlePath: "/Applications/stt.app"), "app bundle path detected")
    try check(!BundleAttribution.isRunningFromAppBundle(bundlePath: "/usr/local/bin/stt"), "bare binary path detected")
    let bundleDiagnostic = BundleAttribution.diagnosticLines(bundlePath: "/usr/local/bin/stt", bundleIdentifier: nil).joined(separator: "\n")
    try check(bundleDiagnostic.contains("./scripts/build-app-bundle.sh"), "bundle diagnostic includes build command")

    let attributionGuidance = AudioPermissions.tccAttributionGuidance(bundleID: "com.example.stt")
    try check(attributionGuidance.contains("./scripts/build-app-bundle.sh"), "TCC attribution guidance includes bundle build command")
    try check(attributionGuidance.contains("STT_RESET_TCC=1 ./scripts/manual-tcc-smoke.sh"), "TCC attribution guidance includes smoke command")

    let microphoneGuidance = AudioPermissions.microphoneResetGuidance(bundleID: "com.example.stt")
    try check(microphoneGuidance.contains("tccutil reset Microphone com.example.stt"), "microphone guidance includes reset command")

    let unavailableTap = NativeTapAvailability(
        osVersionSupported: true,
        createProcessTapSymbolAvailable: false,
        createAggregateDeviceSymbolAvailable: true
    )
    try check(!unavailableTap.isPotentiallyAvailable, "native tap availability requires process tap symbol")
    try check(unavailableTap.summary.contains("AudioHardwareCreateProcessTap"), "native tap summary names missing symbol")
    let runtimeTap = SystemAudioRecorder.probeNativeTapAvailability()
    try check(!runtimeTap.summary.isEmpty, "runtime native tap probe has summary")

    let stateTempDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: stateTempDir) }
    let state = SessionState(
        runID: "20260704-153300",
        name: "Customer Call",
        mode: .meeting,
        startedAt: Date(timeIntervalSince1970: 1_788_000_000),
        finishedAt: Date(timeIntervalSince1970: 1_788_000_123),
        durationSeconds: 123.4,
        outputPaths: ["/tmp/mic.wav", "/tmp/system.wav"],
        separateTracks: true,
        transcriptTextPath: "/tmp/transcript.txt",
        transcriptJSONPath: "/tmp/transcript.json",
        backend: "vibevoice-mlx",
        notes: "unit check"
    )
    _ = try SessionStateStore.write(state, toRunDirectory: stateTempDir)
    let decodedState = try SessionStateStore.read(fromRunDirectory: stateTempDir)
    try checkEqual(decodedState, state, "session state round trip")

    let backendArguments = PythonTranscriber.buildTranscribeArguments(
        audioPath: "meeting.wav",
        outputTextPath: "out.txt",
        outputJSONPath: "out.json",
        device: "gpu",
        modelPath: "custom/model",
        maxNewTokens: 2048
    )
    try check(backendArguments.contains("--model"), "backend arguments include model")
    try check(backendArguments.contains("custom/model"), "backend arguments include model path")
    try check(backendArguments.contains("--max-new-tokens"), "backend arguments include max token flag")
    try check(backendArguments.contains("2048"), "backend arguments include max token value")

    let parsedTranscript = try PythonTranscriber.parseResult(
        audioPath: "meeting.wav",
        stdout: "log before\n{\"backend\":\"vibevoice-mlx\",\"detected_language\":\"en\",\"duration\":12.5,\"transcript_text\":\"Hello world\"}\nlog after",
        outputTextPath: "out.txt",
        outputJSONPath: "out.json"
    )
    try checkEqual(parsedTranscript.backend, "vibevoice-mlx", "transcriber backend parses")
    try checkEqual(parsedTranscript.language, "en", "transcriber detected language parses")
    try checkEqual(parsedTranscript.durationSeconds, 12.5, "transcriber duration parses")
    try checkEqual(parsedTranscript.transcriptText, "Hello world", "transcriber text parses")
    try checkEqual(parsedTranscript.transcriptTextPath, "out.txt", "transcriber text path preserved")

    let rawTranscript = try PythonTranscriber.parseResult(
        audioPath: "raw.wav",
        stdout: "plain transcript text",
        outputTextPath: nil,
        outputJSONPath: nil
    )
    try checkEqual(rawTranscript.transcriptText, "plain transcript text", "raw transcript fallback works")

    let locatorTempDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: locatorTempDir) }
    let bundledResource = locatorTempDir.appendingPathComponent("Resources", isDirectory: true)
    let bundledPython = bundledResource.appendingPathComponent("python", isDirectory: true)
    try FileManager.default.createDirectory(at: bundledPython, withIntermediateDirectories: true)
    let locatedBundledPython = Transcribe.findPythonBackendDirectory(
        environment: [:],
        currentDirectory: locatorTempDir.appendingPathComponent("elsewhere", isDirectory: true),
        bundleResourceURL: bundledResource
    )
    try checkEqual(locatedBundledPython?.path, bundledPython.path, "bundled python backend locator")

    let pythonBackendDir = URL(fileURLWithPath: FileManager.default.currentDirectoryPath).appendingPathComponent("python")
    if FileManager.default.fileExists(atPath: pythonBackendDir.path) {
        let status = try PythonTranscriber.statusReport(workingDirectory: pythonBackendDir, timeout: 5)
        try check(status.succeeded, "python backend status command succeeds")
        try check(status.standardOutput.contains("overall ready"), "python backend status reports readiness")
    }

    let echo = try ProcessRunner.run(executablePath: "/bin/echo", arguments: ["hello", "world"])
    try check(echo.succeeded, "echo succeeds")
    try checkEqual(echo.standardOutput.trimmingCharacters(in: .whitespacesAndNewlines), "hello world", "echo stdout captured")
}

do {
    try runChecks()
    print("sttUnitChecks: all checks passed")
} catch {
    FileHandle.standardError.write("sttUnitChecks failed: \(error)\n".data(using: .utf8)!)
    exit(1)
}
