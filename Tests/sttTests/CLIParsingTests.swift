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

    @Test func recordMeetingModeWithOutputDir() throws {
        let record = try Record.parse(["--mode", "meeting", "--output-dir", "session1"])
        #expect(record.mode == .meeting)
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

    @Test func transcribeMeetingParsesSourcesAndOptions() throws {
        let transcribe = try TranscribeMeeting.parse([
            "mic.wav",
            "system.wav",
            "--output", "out.md",
            "--json", "out.json",
            "--device", "gpu",
            "--timeout", "30",
            "--model", "custom/model",
            "--max-new-tokens", "2048",
            "--python-backend", "/tmp/backend",
            "--require-backend-ready"
        ])
        #expect(transcribe.micAudioPath == "mic.wav")
        #expect(transcribe.systemAudioPath == "system.wav")
        #expect(transcribe.output == "out.md")
        #expect(transcribe.json == "out.json")
        #expect(transcribe.device == .gpu)
        #expect(transcribe.timeout == 30)
        #expect(transcribe.model == "custom/model")
        #expect(transcribe.maxNewTokens == 2048)
        #expect(transcribe.pythonBackend == "/tmp/backend")
        #expect(transcribe.requireBackendReady)
    }

    @Test func transcribeMeetingParsesDiarizeFlags() throws {
        let transcribe = try TranscribeMeeting.parse([
            "mic.wav", "system.wav",
            "--diarize",
            "--diarize-num-speakers", "2",
            "--diarize-provider", "mfcc-test",
            "--config", "/tmp/config.json"
        ])
        #expect(transcribe.diarize)
        #expect(transcribe.diarizeNumSpeakers == 2)
        #expect(transcribe.diarizeProvider == "mfcc-test")
        #expect(transcribe.config == "/tmp/config.json")
        #expect(transcribe.diarizeDistanceThreshold == nil)
    }

    @Test func transcribeMeetingRejectsMutuallyExclusiveDiarizeCountFlags() throws {
        let transcribe = try TranscribeMeeting.parse([
            "mic.wav", "system.wav",
            "--diarize-num-speakers", "2",
            "--diarize-distance-threshold", "0.2"
        ])
        #expect(throws: (any Error).self) {
            try transcribe.run()
        }
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
            "--mix-mode", "raw",
            "--meeting-transcription", "mixed"
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
        #expect(pipeline.meetingTranscription == .mixed)
    }

    @Test func pipelineMixModeDefaultsToBalanced() throws {
        let pipeline = try Pipeline.parse(["--mode", "meeting"])
        #expect(pipeline.mixMode == .balanced)
    }

    @Test func pipelineMeetingTranscriptionDefaultsToSeparate() throws {
        let pipeline = try Pipeline.parse(["--mode", "meeting"])
        #expect(pipeline.meetingTranscription == .separate)
    }

    @Test func pipelineParsesDiarizeFlags() throws {
        let pipeline = try Pipeline.parse([
            "--mode", "meeting",
            "--diarize",
            "--diarize-distance-threshold", "0.2",
            "--diarize-provider", "mfcc-test"
        ])
        #expect(pipeline.diarize)
        #expect(pipeline.diarizeDistanceThreshold == 0.2)
        #expect(pipeline.diarizeProvider == "mfcc-test")
        #expect(pipeline.diarizeNumSpeakers == nil)
    }

    @Test func pipelineRejectsMutuallyExclusiveDiarizeCountFlags() throws {
        let pipeline = try Pipeline.parse([
            "--mode", "meeting",
            "--diarize-num-speakers", "2",
            "--diarize-distance-threshold", "0.2"
        ])
        #expect(throws: (any Error).self) {
            try pipeline.run()
        }
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

    @Test func speakerEnrollParsesAudioAndProviderOptions() throws {
        let enroll = try Speaker.Enroll.parse([
            "Larry Song", "--audio", "/tmp/larry.wav", "--provider", "mfcc-test",
            "--minimum-speech-seconds", "8.0", "--replace"
        ])
        #expect(enroll.displayName == "Larry Song")
        #expect(enroll.audio == "/tmp/larry.wav")
        #expect(enroll.duration == nil)
        #expect(enroll.provider == "mfcc-test")
        #expect(enroll.minimumSpeechSeconds == 8.0)
        #expect(enroll.replace)
    }

    @Test func speakerEnrollRequiresExactlyOneOfAudioOrDuration() throws {
        let neither = try Speaker.Enroll.parse(["Larry Song"])
        #expect(throws: (any Error).self) {
            try neither.run()
        }

        let both = try Speaker.Enroll.parse(["Larry Song", "--audio", "/tmp/a.wav", "--duration", "10"])
        #expect(throws: (any Error).self) {
            try both.run()
        }
    }

    @Test func speakerRenameParsesBothNames() throws {
        let rename = try Speaker.Rename.parse(["Larry", "Larry Song"])
        #expect(rename.existingName == "Larry")
        #expect(rename.newName == "Larry Song")
    }

    @Test func speakerRemoveRequiresYesFlag() throws {
        let withoutYes = try Speaker.Remove.parse(["Larry Song"])
        #expect(throws: (any Error).self) {
            try withoutYes.run()
        }

        let withYes = try Speaker.Remove.parse(["Larry Song", "--yes"])
        #expect(withYes.yes)
    }

    @Test func identifyParsesThresholdAndMarginOptions() throws {
        let identify = try Identify.parse([
            "/tmp/clip.wav", "--provider", "mfcc-test", "--threshold", "0.8", "--margin", "0.1",
            "--json", "/tmp/out.json"
        ])
        #expect(identify.audioPath == "/tmp/clip.wav")
        #expect(identify.provider == "mfcc-test")
        #expect(identify.threshold == 0.8)
        #expect(identify.margin == 0.1)
        #expect(identify.json == "/tmp/out.json")
    }

    @Test func topLevelCommandRoutesToSpeakerListSubcommand() throws {
        let command = try STT.parseAsRoot(["speaker", "list"])
        #expect(command is Speaker.ListProfiles)
    }

    @Test func topLevelCommandRoutesToIdentifySubcommand() throws {
        let command = try STT.parseAsRoot(["identify", "/tmp/clip.wav"])
        #expect(command is Identify)
    }

    // MARK: - Task 08: stt speaker audit / purity-preview / enroll-ranges / suggest-labels

    @Test func speakerAuditParsesRequiredAndOptionalFlags() throws {
        let audit = try Speaker.Audit.parse([
            "--transcript", "/tmp/session/transcript.json",
            "--force",
            "--min-useful-speech", "5.0",
            "--mixed-span-ratio", "3.0",
            "--json", "/tmp/out.json"
        ])
        #expect(audit.transcript == "/tmp/session/transcript.json")
        #expect(audit.force)
        #expect(audit.minUsefulSpeech == 5.0)
        #expect(audit.mixedSpanRatio == 3.0)
        #expect(audit.json == "/tmp/out.json")
    }

    @Test func speakerAuditDefaultsForceToFalse() throws {
        let audit = try Speaker.Audit.parse(["--transcript", "/tmp/session/transcript.json"])
        #expect(!audit.force)
        #expect(audit.minUsefulSpeech == nil)
    }

    @Test func speakerPurityPreviewParsesSpeakerIdAndRepeatedRanges() throws {
        let preview = try Speaker.PurityPreview.parse([
            "--transcript", "/tmp/session/transcript.json",
            "--speaker-id", "4",
            "--range", "12.0-45.0",
            "--range", "100.0-120.0",
            "--preview-seconds", "12",
            "--no-play",
            "--no-normalize"
        ])
        #expect(preview.speakerId == "4")
        #expect(preview.range == ["12.0-45.0", "100.0-120.0"])
        #expect(preview.previewSeconds == 12)
        #expect(preview.noPlay)
        #expect(preview.noNormalize)
    }

    @Test func speakerPurityPreviewDefaultsRangeToEmpty() throws {
        let preview = try Speaker.PurityPreview.parse([
            "--transcript", "/tmp/session/transcript.json",
            "--speaker-id", "4"
        ])
        #expect(preview.range.isEmpty)
        #expect(!preview.noPlay)
        #expect(!preview.noNormalize)
    }

    @Test func speakerEnrollRangesParsesDisplayNameRangesAndFlags() throws {
        let enrollRanges = try Speaker.EnrollRanges.parse([
            "Domingo",
            "--transcript", "/tmp/session/transcript.json",
            "--speaker-id", "4",
            "--range", "12.0-45.0",
            "--source", "system",
            "--sample-seconds", "60",
            "--no-enroll"
        ])
        #expect(enrollRanges.displayName == "Domingo")
        #expect(enrollRanges.speakerId == "4")
        #expect(enrollRanges.range == ["12.0-45.0"])
        #expect(enrollRanges.source == "system")
        #expect(enrollRanges.sampleSeconds == 60)
        #expect(enrollRanges.noEnroll)
    }

    @Test func speakerEnrollRangesRequiresAtLeastOneRangeAtRunTime() throws {
        // Parsing succeeds (range defaults to []); the explicit "at least one
        // range" requirement is enforced in run(), not by ArgumentParser.
        let enrollRanges = try Speaker.EnrollRanges.parse([
            "Domingo",
            "--transcript", "/tmp/session/transcript.json",
            "--speaker-id", "4"
        ])
        #expect(enrollRanges.range.isEmpty)
        #expect(throws: (any Error).self) {
            try enrollRanges.run()
        }
    }

    @Test func speakerSuggestLabelsParsesThresholdMarginAndWindowOptions() throws {
        let suggest = try Speaker.SuggestLabels.parse([
            "--transcript", "/tmp/session/transcript.json",
            "--threshold", "0.8",
            "--margin", "0.1",
            "--provider", "mfcc-test",
            "--no-windows",
            "--json", "/tmp/suggestions.json"
        ])
        #expect(suggest.transcript == "/tmp/session/transcript.json")
        #expect(suggest.threshold == 0.8)
        #expect(suggest.margin == 0.1)
        #expect(suggest.provider == "mfcc-test")
        #expect(suggest.noWindows)
        #expect(suggest.json == "/tmp/suggestions.json")
    }

    @Test func speakerSuggestLabelsDefaultsAreNilOrFalse() throws {
        let suggest = try Speaker.SuggestLabels.parse(["--transcript", "/tmp/session/transcript.json"])
        #expect(suggest.threshold == nil)
        #expect(suggest.margin == nil)
        #expect(!suggest.noWindows)
        #expect(suggest.nWindows == nil)
    }

    @Test func topLevelCommandRoutesToSpeakerAuditSubcommand() throws {
        let command = try STT.parseAsRoot([
            "speaker", "audit", "--transcript", "/tmp/session/transcript.json"
        ])
        #expect(command is Speaker.Audit)
    }

    @Test func topLevelCommandRoutesToSpeakerSuggestLabelsSubcommand() throws {
        let command = try STT.parseAsRoot([
            "speaker", "suggest-labels", "--transcript", "/tmp/session/transcript.json"
        ])
        #expect(command is Speaker.SuggestLabels)
    }
}
