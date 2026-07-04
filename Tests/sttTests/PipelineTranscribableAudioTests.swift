import Foundation
import Testing
@testable import sttCore

@Suite("Pipeline transcribable audio preflight")
struct PipelineTranscribableAudioTests {

    @Test func acceptsNonEmptyAudioFile() throws {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let audioURL = tmpDir.appendingPathComponent("audio.wav")
        FileManager.default.createFile(atPath: audioURL.path, contents: Data("x".utf8))

        try Pipeline.requireTranscribableAudio(at: audioURL)
    }

    @Test func rejectsMissingAudioFile() throws {
        let missingURL = FileManager.default.temporaryDirectory.appendingPathComponent("definitely-missing-stt-pipeline-\(UUID().uuidString).wav")

        do {
            try Pipeline.requireTranscribableAudio(at: missingURL)
            Issue.record("Expected missing audio file preflight error")
        } catch {
            #expect(error.localizedDescription.contains("Audio file not found: \(missingURL.path)"))
        }
    }

    @Test func rejectsEmptyAudioFile() throws {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let audioURL = tmpDir.appendingPathComponent("empty.wav")
        FileManager.default.createFile(atPath: audioURL.path, contents: Data())

        do {
            try Pipeline.requireTranscribableAudio(at: audioURL)
            Issue.record("Expected empty audio file preflight error")
        } catch {
            #expect(error.localizedDescription.contains("Audio file is empty (0 bytes): \(audioURL.path)"))
        }
    }

    @Test func rejectsEmptyMeetingFallbackMicFile() throws {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let micURL = tmpDir.appendingPathComponent("mic.wav")
        let systemURL = tmpDir.appendingPathComponent("system.wav")
        let mixedURL = tmpDir.appendingPathComponent("mixed.wav")
        FileManager.default.createFile(atPath: micURL.path, contents: Data())
        try WAVPCMFile(sampleRate: 44_100, samples: [1]).encodedData().write(to: systemURL)

        let selection = Pipeline.resolveMeetingAudioSource(micURL: micURL, systemURL: systemURL, mixedURL: mixedURL)
        #expect(selection.audioToTranscribeURL == micURL)

        do {
            try Pipeline.requireTranscribableAudio(at: selection.audioToTranscribeURL)
            Issue.record("Expected empty fallback mic preflight error")
        } catch {
            #expect(error.localizedDescription.contains("Audio file is empty (0 bytes): \(micURL.path)"))
        }
    }
}
