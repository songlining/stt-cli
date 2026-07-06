import Foundation
import Testing
import ArgumentParser
@testable import sttCore

@Suite("CLI parsing")
struct CLIParsingTests {

    @Test func doctorParsesPythonBackendOptions() throws {
        let doctor = try Doctor.parse(["--python-backend", "/tmp/backend", "--require-backend-ready"])
        #expect(doctor.pythonBackend == "/tmp/backend")
        #expect(doctor.requireBackendReady)
    }

    @Test func doctorRejectsMissingPythonBackendOverrideBeforeStatusCheck() throws {
        let doctor = try Doctor.parse(["--python-backend", "/definitely/missing/stt-backend"])
        #expect(throws: (any Error).self) {
            try doctor.run()
        }
    }

    @Test func recordMicModeParsesOutputAndMode() throws {
        let record = try Record.parse(["--mode", "mic", "--output", "foo.wav"])
        #expect(record.mode == .mic)
        #expect(record.output == "foo.wav")
        #expect(record.inputDevice == nil)
        #expect(record.separateTracks == false)
        #expect(record.outputDir == nil)
    }

    @Test func recordDefaultsToMicModeWhenOmitted() throws {
        let record = try Record.parse(["--output", "foo.wav"])
        #expect(record.mode == .mic)
    }

    @Test func recordSystemModeWithInputDevice() throws {
        let record = try Record.parse(["--mode", "system", "--output", "sys.wav", "--input-device", "BlackHole 2ch"])
        #expect(record.mode == .system)
        #expect(record.inputDevice == "BlackHole 2ch")
    }

    @Test func recordParsesOptionalDurationAndFailIfEmpty() throws {
        let record = try Record.parse(["--mode", "mic", "--output", "timed.wav", "--duration", "1.5", "--fail-if-empty"])
        #expect(record.duration == 1.5)
        #expect(record.failIfEmpty)
    }

    @Test func recordMeetingModeWithSeparateTracksAndOutputDir() throws {
        let record = try Record.parse(["--mode", "meeting", "--separate-tracks", "--output-dir", "session1"])
        #expect(record.mode == .meeting)
        #expect(record.separateTracks)
        #expect(record.outputDir == "session1")
        #expect(record.mixMode == .balanced)
    }

    @Test func recordMixModeDefaultsToBalancedAndAcceptsRaw() throws {
        let defaultRecord = try Record.parse(["--mode", "meeting"])
        #expect(defaultRecord.mixMode == .balanced)

        let rawRecord = try Record.parse(["--mode", "meeting", "--mix-mode", "raw"])
        #expect(rawRecord.mixMode == .raw)
    }

    @Test func recordRejectsInvalidMixMode() {
        #expect(throws: (any Error).self) {
            try Record.parse(["--mode", "meeting", "--mix-mode", "bogus"])
        }
    }

    @Test func recordRejectsInvalidMode() {
        #expect(throws: (any Error).self) {
            try Record.parse(["--mode", "bogus"])
        }
    }

    @Test func recordRejectsNonPositiveDurationBeforeStartingCapture() throws {
        let record = try Record.parse(["--mode", "mic", "--duration", "0"])
        #expect(throws: (any Error).self) {
            try record.run()
        }
    }

    @Test func transcribeParsesPositionalAndOptions() throws {
        let transcribe = try Transcribe.parse([
            "meeting.wav",
            "--output", "out.txt",
            "--json", "out.json",
            "--device", "gpu",
            "--timeout", "30",
            "--model", "custom/model",
            "--max-new-tokens", "2048",
            "--python-backend", "/tmp/backend",
            "--require-backend-ready"
        ])
        #expect(transcribe.audioPath == "meeting.wav")
        #expect(transcribe.output == "out.txt")
        #expect(transcribe.json == "out.json")
        #expect(transcribe.device == .gpu)
        #expect(transcribe.timeout == 30)
        #expect(transcribe.model == "custom/model")
        #expect(transcribe.maxNewTokens == 2048)
        #expect(transcribe.pythonBackend == "/tmp/backend")
        #expect(transcribe.requireBackendReady)
    }

    @Test func transcribeRejectsNonPositiveTimeoutBeforeLaunchingBackend() throws {
        let transcribe = try Transcribe.parse(["meeting.wav", "--timeout", "0"])
        #expect(throws: (any Error).self) {
            try transcribe.run()
        }
    }

    @Test func transcribeRejectsNonPositiveMaxTokensBeforeLaunchingBackend() throws {
        let transcribe = try Transcribe.parse(["meeting.wav", "--max-new-tokens", "0"])
        #expect(throws: (any Error).self) {
            try transcribe.run()
        }
    }

    @Test func transcribeRejectsMissingPythonBackendOverrideBeforeLaunchingBackend() throws {
        let transcribe = try Transcribe.parse(["meeting.wav", "--python-backend", "/definitely/missing/stt-backend"])
        #expect(throws: (any Error).self) {
            try transcribe.run()
        }
    }

    @Test func transcribeRejectsMissingAudioFileBeforeLaunchingBackend() throws {
        let missing = "/tmp/definitely-missing-stt-audio-\(UUID().uuidString).wav"
        let transcribe = try Transcribe.parse([missing])
        do {
            try transcribe.run()
            Issue.record("Expected missing audio file preflight error")
        } catch {
            #expect(error.localizedDescription.contains("Audio file not found: \(missing)"))
        }
    }

    @Test func transcribeRejectsEmptyAudioFileBeforeLaunchingBackend() throws {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmpDir) }
        let empty = tmpDir.appendingPathComponent("empty.wav")
        FileManager.default.createFile(atPath: empty.path, contents: Data())

        let transcribe = try Transcribe.parse([empty.path])
        do {
            try transcribe.run()
            Issue.record("Expected empty audio file preflight error")
        } catch {
            #expect(error.localizedDescription.contains("Audio file is empty (0 bytes): \(empty.path)"))
        }
    }

    @Test func transcribeStillPrioritizesInvalidBackendOverrideBeforeMissingAudioFile() throws {
        let transcribe = try Transcribe.parse([
            "/tmp/definitely-missing-stt-audio-\(UUID().uuidString).wav",
            "--python-backend", "/definitely/missing/stt-backend"
        ])
        do {
            try transcribe.run()
            Issue.record("Expected invalid backend preflight error")
        } catch {
            #expect(error.localizedDescription.contains("--python-backend must point to an existing directory"))
        }
    }

    @Test func transcribeDefaultsDeviceToAuto() throws {
        let transcribe = try Transcribe.parse(["meeting.wav"])
        #expect(transcribe.device == .auto)
    }

    @Test func pipelineParsesModeAndName() throws {
        let pipeline = try Pipeline.parse([
            "--mode", "meeting",
            "--name", "Customer Call",
            "--input-device", "BlackHole 2ch",
            "--duration", "5",
            "--fail-if-empty",
            "--transcribe-timeout", "120",
            "--device", "cpu",
            "--model", "custom/model",
            "--max-new-tokens", "2048",
            "--python-backend", "/tmp/backend",
            "--require-backend-ready",
            "--mix-mode", "raw"
        ])
        #expect(pipeline.mode == .meeting)
        #expect(pipeline.name == "Customer Call")
        #expect(pipeline.inputDevice == "BlackHole 2ch")
        #expect(pipeline.duration == 5)
        #expect(pipeline.failIfEmpty)
        #expect(pipeline.transcribeTimeout == 120)
        #expect(pipeline.device == .cpu)
        #expect(pipeline.model == "custom/model")
        #expect(pipeline.maxNewTokens == 2048)
        #expect(pipeline.pythonBackend == "/tmp/backend")
        #expect(pipeline.requireBackendReady)
        #expect(pipeline.mixMode == .raw)
    }

    @Test func pipelineMixModeDefaultsToBalanced() throws {
        let pipeline = try Pipeline.parse(["--mode", "meeting"])
        #expect(pipeline.mixMode == .balanced)
    }

    @Test func mixParsesInputsAndOptions() throws {
        let mix = try Mix.parse(["mic.wav", "system.wav", "--output", "mixed.wav", "--fail-if-empty", "--mix-mode", "raw"])
        #expect(mix.firstAudioPath == "mic.wav")
        #expect(mix.secondAudioPath == "system.wav")
        #expect(mix.output == "mixed.wav")
        #expect(mix.failIfEmpty)
        #expect(mix.mixMode == .raw)
    }

    @Test func mixMixModeDefaultsToBalanced() throws {
        let mix = try Mix.parse(["mic.wav", "system.wav"])
        #expect(mix.mixMode == .balanced)
    }

    @Test func mixRejectsMissingInputFileBeforeMixing() throws {
        let missing = "/tmp/definitely-missing-stt-mix-input-\(UUID().uuidString).wav"
        let mix = try Mix.parse([missing, missing])
        do {
            try mix.run()
            Issue.record("Expected missing mix input preflight error")
        } catch {
            #expect(error.localizedDescription.contains("Audio file not found: \(missing)"))
        }
    }

    @Test func mixSucceedsAndWritesDefaultMixedFileNextToFirstInput() throws {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let micURL = tmpDir.appendingPathComponent("mic.wav")
        let systemURL = tmpDir.appendingPathComponent("system.wav")
        let mixedURL = tmpDir.appendingPathComponent("mixed.wav")
        try WAVPCMFile(sampleRate: 8_000, samples: [1_000, 2_000]).encodedData().write(to: micURL)
        try WAVPCMFile(sampleRate: 8_000, samples: [3_000]).encodedData().write(to: systemURL)

        let mix = try Mix.parse([micURL.path, systemURL.path])
        try mix.run()

        #expect(FileManager.default.fileExists(atPath: mixedURL.path))
        let mixed = try WAVPCMFile.parse(Data(contentsOf: mixedURL))
        #expect(mixed.samples == [4_000, 2_000])
    }

    @Test func mixFailIfEmptyThrowsWhenMixedOutputIsTooSmall() throws {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let micURL = tmpDir.appendingPathComponent("mic.wav")
        let systemURL = tmpDir.appendingPathComponent("system.wav")
        let mixedURL = tmpDir.appendingPathComponent("mixed.wav")
        try WAVPCMFile(sampleRate: 8_000, samples: [1]).encodedData().write(to: micURL)
        try WAVPCMFile(sampleRate: 8_000, samples: [1]).encodedData().write(to: systemURL)

        let mix = try Mix.parse([micURL.path, systemURL.path, "--output", mixedURL.path, "--fail-if-empty"])
        do {
            try mix.run()
            Issue.record("Expected fail-if-empty to reject tiny mixed output")
        } catch {
            #expect(error.localizedDescription.contains("WARNING: output file is very small"))
        }
    }

    @Test func pipelineRejectsInvalidDurationsBeforeStartingCapture() throws {
        let invalidDuration = try Pipeline.parse(["--duration", "0"])
        #expect(throws: (any Error).self) {
            try invalidDuration.run()
        }

        let invalidTimeout = try Pipeline.parse(["--transcribe-timeout", "0"])
        #expect(throws: (any Error).self) {
            try invalidTimeout.run()
        }

        let invalidMaxTokens = try Pipeline.parse(["--max-new-tokens", "0"])
        #expect(throws: (any Error).self) {
            try invalidMaxTokens.run()
        }

        let invalidBackend = try Pipeline.parse(["--python-backend", "/definitely/missing/stt-backend"])
        #expect(throws: (any Error).self) {
            try invalidBackend.run()
        }
    }

    @Test func topLevelCommandRoutesToRecordSubcommand() throws {
        let command = try STT.parseAsRoot(["record", "--mode", "mic", "--output", "foo.wav"])
        #expect(command is Record)
        if let record = command as? Record {
            #expect(record.mode == .mic)
            #expect(record.output == "foo.wav")
        }
    }

    @Test func permissionsResetHelpSubcommandParses() throws {
        let command = try STT.parseAsRoot(["permissions", "reset-help", "--bundle-id", "com.example.stt"])
        #expect(command is Permissions.ResetHelp)
        if let resetHelp = command as? Permissions.ResetHelp {
            #expect(resetHelp.bundleID == "com.example.stt")
        }
    }
}
