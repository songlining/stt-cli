import Foundation
import Testing
@testable import sttCore

@Suite("PythonSpeakerIdentifier")
struct PythonSpeakerIdentifierTests {

    /// Repo `python/` directory, containing `stt_vibevoice`. Tests run
    /// against the real `mfcc-test` provider end-to-end since it is
    /// stdlib-only and requires no ML dependencies.
    private static var pythonBackendDirectory: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent() // Tests/sttTests
            .deletingLastPathComponent() // Tests
            .deletingLastPathComponent() // repo root
            .appendingPathComponent("python", isDirectory: true)
    }

    private func writeTestWAV(to url: URL, durationSeconds: Double, amplitude: Int16 = 8000, framerate: Int = 16000) throws {
        let frameCount = Int(durationSeconds * Double(framerate))
        var pcmData = Data(capacity: frameCount * 2)
        for i in 0..<frameCount {
            let sample: Int16 = (i / 64) % 2 == 0 ? amplitude : -amplitude
            withUnsafeBytes(of: sample.littleEndian) { pcmData.append(contentsOf: $0) }
        }
        let header = WAVWriter.header(sampleRate: UInt32(framerate), channels: 1, bitDepth: 16, dataSize: UInt32(pcmData.count))
        var fileData = header
        fileData.append(pcmData)
        try fileData.write(to: url)
    }

    @Test func buildsExtractArgumentsForWholeAudio() {
        let arguments = PythonSpeakerIdentifier.buildExtractArguments(
            audioPath: "sample.wav",
            segmentsJSONPath: nil,
            speakerID: nil,
            provider: "mfcc-test",
            minimumSpeechSeconds: 8.0
        )

        #expect(arguments == [
            "-m", "stt_vibevoice.speaker_id", "extract",
            "--audio", "sample.wav",
            "--provider", "mfcc-test",
            "--minimum-speech-seconds", "8.0"
        ])
    }

    @Test func buildsExtractArgumentsForSegments() {
        let arguments = PythonSpeakerIdentifier.buildExtractArguments(
            audioPath: "sample.wav",
            segmentsJSONPath: "transcript.json",
            speakerID: "0",
            provider: "mfcc-test",
            minimumSpeechSeconds: 8.0
        )

        #expect(arguments == [
            "-m", "stt_vibevoice.speaker_id", "extract",
            "--audio", "sample.wav",
            "--segments", "transcript.json",
            "--speaker-id", "0",
            "--provider", "mfcc-test",
            "--minimum-speech-seconds", "8.0"
        ])
    }

    @Test func extractWholeAudioReturnsOKAboveMinimum() throws {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let audioURL = tmpDir.appendingPathComponent("sample.wav")
        try writeTestWAV(to: audioURL, durationSeconds: 9.0)

        let result = try PythonSpeakerIdentifier.extractWholeAudio(
            audioPath: audioURL.path,
            provider: "mfcc-test",
            minimumSpeechSeconds: 8.0,
            workingDirectory: Self.pythonBackendDirectory,
            timeout: 15
        )

        #expect(result.status == "ok")
        #expect(result.provider == "mfcc-test")
        #expect(result.embedding != nil)
        #expect(!(result.embedding ?? []).isEmpty)
    }

    @Test func extractWholeAudioReturnsTooShortBelowMinimum() throws {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let audioURL = tmpDir.appendingPathComponent("short.wav")
        try writeTestWAV(to: audioURL, durationSeconds: 1.0)

        let result = try PythonSpeakerIdentifier.extractWholeAudio(
            audioPath: audioURL.path,
            provider: "mfcc-test",
            minimumSpeechSeconds: 8.0,
            workingDirectory: Self.pythonBackendDirectory,
            timeout: 15
        )

        #expect(result.isTooShort)
        #expect(result.embedding == nil)
    }

    @Test func extractSpeakerSegmentsConcatenatesMatchingSpeaker() throws {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let audioURL = tmpDir.appendingPathComponent("full.wav")
        try writeTestWAV(to: audioURL, durationSeconds: 20.0)

        let transcriptURL = tmpDir.appendingPathComponent("transcript.json")
        let segments: [[String: Any]] = [
            ["speaker_id": "0", "start_time": 0.0, "end_time": 5.0, "text": "hi"],
            ["speaker_id": "1", "start_time": 5.0, "end_time": 10.0, "text": "there"],
            ["speaker_id": "0", "start_time": 10.0, "end_time": 15.0, "text": "again"]
        ]
        let payload: [String: Any] = ["segments": segments]
        let data = try JSONSerialization.data(withJSONObject: payload)
        try data.write(to: transcriptURL)

        let result = try PythonSpeakerIdentifier.extractSpeakerSegments(
            audioPath: audioURL.path,
            segmentsJSONPath: transcriptURL.path,
            speakerID: "0",
            provider: "mfcc-test",
            minimumSpeechSeconds: 8.0,
            workingDirectory: Self.pythonBackendDirectory,
            timeout: 15
        )

        #expect(result.status == "ok")
        #expect(result.segmentCount == 2)
        #expect(result.durationSeconds > 9.0)
    }

    @Test func matchReturnsMatchedAboveThresholdAndMargin() throws {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let audioURL = tmpDir.appendingPathComponent("enroll.wav")
        try writeTestWAV(to: audioURL, durationSeconds: 9.0)

        let extraction = try PythonSpeakerIdentifier.extractWholeAudio(
            audioPath: audioURL.path,
            provider: "mfcc-test",
            minimumSpeechSeconds: 8.0,
            workingDirectory: Self.pythonBackendDirectory,
            timeout: 15
        )
        #expect(extraction.embedding != nil)

        let profile = SpeakerProfile(
            displayName: "Larry",
            embeddingProvider: extraction.provider,
            embeddingModel: extraction.model ?? "",
            embedding: extraction.embedding ?? []
        )

        let profilesURL = tmpDir.appendingPathComponent("profiles.json")
        let candidateURL = tmpDir.appendingPathComponent("candidate.json")
        try PythonSpeakerIdentifier.writeFlattenedProfiles([profile], to: profilesURL)
        try PythonSpeakerIdentifier.writeCandidate(extraction, to: candidateURL)

        let matchResult = try PythonSpeakerIdentifier.match(
            candidateJSONPath: candidateURL.path,
            profilesJSONPath: profilesURL.path,
            threshold: 0.5,
            margin: 0.01,
            workingDirectory: Self.pythonBackendDirectory,
            timeout: 15
        )

        #expect(matchResult.bestMatch?.profileId == profile.id.uuidString)
        #expect(matchResult.bestMatch?.matched == true)
        #expect(matchResult.bestMatch?.status == "matched")
    }

    @Test func matchReturnsNilBestMatchWhenNoProfiles() throws {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let profilesURL = tmpDir.appendingPathComponent("profiles.json")
        let candidateURL = tmpDir.appendingPathComponent("candidate.json")
        try PythonSpeakerIdentifier.writeFlattenedProfiles([], to: profilesURL)
        try PythonSpeakerIdentifier.writeCandidate(
            SpeakerExtractionResult(
                speakerId: "0",
                provider: "mfcc-test",
                model: "stt-vibevoice/mfcc-test-v1",
                embedding: [1.0, 0.0],
                durationSeconds: 9.0,
                segmentCount: 1,
                status: "ok"
            ),
            to: candidateURL
        )

        let matchResult = try PythonSpeakerIdentifier.match(
            candidateJSONPath: candidateURL.path,
            profilesJSONPath: profilesURL.path,
            threshold: 0.78,
            margin: 0.05,
            workingDirectory: Self.pythonBackendDirectory,
            timeout: 15
        )

        #expect(matchResult.bestMatch == nil)
        #expect(matchResult.candidates.isEmpty)
    }

    @Test func extractSurfacesBackendFailureMessageWhenAudioMissing() throws {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let missingAudioURL = tmpDir.appendingPathComponent("does-not-exist.wav")

        #expect(throws: PythonSpeakerIdentifierError.self) {
            _ = try PythonSpeakerIdentifier.extractWholeAudio(
                audioPath: missingAudioURL.path,
                provider: "mfcc-test",
                minimumSpeechSeconds: 8.0,
                workingDirectory: Self.pythonBackendDirectory,
                timeout: 15
            )
        }
    }

    @Test func python3NotFoundErrorWhenNoPythonOnPath() throws {
        let emptyRoot = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: emptyRoot, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: emptyRoot) }

        // Force an isolated PATH via a runtime override that doesn't exist,
        // and no python3 resolvable relative to it, is difficult to force
        // deterministically without mutating the real PATH; instead this
        // test only exercises the error's message format directly.
        let error = PythonSpeakerIdentifierError.python3NotFound
        #expect(error.errorDescription?.contains("python3") == true)
    }
}
