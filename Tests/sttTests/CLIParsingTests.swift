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

    @Test func recordParsesOptionalDuration() throws {
        let record = try Record.parse(["--mode", "mic", "--output", "timed.wav", "--duration", "1.5"])
        #expect(record.duration == 1.5)
    }

    @Test func recordMeetingModeWithSeparateTracksAndOutputDir() throws {
        let record = try Record.parse(["--mode", "meeting", "--separate-tracks", "--output-dir", "session1"])
        #expect(record.mode == .meeting)
        #expect(record.separateTracks)
        #expect(record.outputDir == "session1")
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
            "--transcribe-timeout", "120",
            "--device", "cpu",
            "--model", "custom/model",
            "--max-new-tokens", "2048",
            "--python-backend", "/tmp/backend",
            "--require-backend-ready"
        ])
        #expect(pipeline.mode == .meeting)
        #expect(pipeline.name == "Customer Call")
        #expect(pipeline.inputDevice == "BlackHole 2ch")
        #expect(pipeline.duration == 5)
        #expect(pipeline.transcribeTimeout == 120)
        #expect(pipeline.device == .cpu)
        #expect(pipeline.model == "custom/model")
        #expect(pipeline.maxNewTokens == 2048)
        #expect(pipeline.pythonBackend == "/tmp/backend")
        #expect(pipeline.requireBackendReady)
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
