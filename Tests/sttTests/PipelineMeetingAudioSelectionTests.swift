import Foundation
import Testing
@testable import sttCore

@Suite("Pipeline meeting audio selection")
struct PipelineMeetingAudioSelectionTests {

    @Test func selectsMixedTrackWhenMixingSucceeds() throws {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let micURL = tmpDir.appendingPathComponent("mic.wav")
        let systemURL = tmpDir.appendingPathComponent("system.wav")
        let mixedURL = tmpDir.appendingPathComponent("mixed.wav")
        try WAVPCMFile(sampleRate: 8_000, samples: [1_000, 2_000]).encodedData().write(to: micURL)
        try WAVPCMFile(sampleRate: 8_000, samples: [3_000]).encodedData().write(to: systemURL)

        let selection = Pipeline.resolveMeetingAudioSource(micURL: micURL, systemURL: systemURL, mixedURL: mixedURL, mode: .raw)

        #expect(selection.audioToTranscribeURL == mixedURL)
        #expect(selection.outputURLs == [micURL, systemURL, mixedURL])
        #expect(selection.note == nil)
        #expect(selection.driftNote == nil)
        #expect(FileManager.default.fileExists(atPath: mixedURL.path))

        // Explicitly exercises `.raw` mode so this assertion continues to
        // reflect exact PCM summation, independent of the new `.balanced`
        // default.
        let mixed = try WAVPCMFile.parse(Data(contentsOf: mixedURL))
        #expect(mixed.samples == [4_000, 2_000])
    }

    @Test func fallsBackToMicTrackWhenMixingFails() throws {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let micURL = tmpDir.appendingPathComponent("mic.wav")
        let systemURL = tmpDir.appendingPathComponent("system.wav")
        let mixedURL = tmpDir.appendingPathComponent("mixed.wav")
        try WAVPCMFile(sampleRate: 16_000, samples: [1]).encodedData().write(to: micURL)
        try Data("not a wav".utf8).write(to: systemURL)

        let selection = Pipeline.resolveMeetingAudioSource(micURL: micURL, systemURL: systemURL, mixedURL: mixedURL)

        #expect(selection.audioToTranscribeURL == micURL)
        #expect(selection.outputURLs == [micURL, systemURL])
        #expect(selection.note?.contains("Mixed track unavailable") == true)
        #expect(selection.note?.contains("transcribing mic.wav instead") == true)
        #expect(selection.driftNote == nil)
        #expect(!FileManager.default.fileExists(atPath: mixedURL.path))
    }

    @Test func reportsDriftWhenMixedDurationsDiverge() throws {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let micURL = tmpDir.appendingPathComponent("mic.wav")
        let systemURL = tmpDir.appendingPathComponent("system.wav")
        let mixedURL = tmpDir.appendingPathComponent("mixed.wav")
        try WAVPCMFile(sampleRate: 10, samples: Array(repeating: 1, count: 10)).encodedData().write(to: micURL)
        try WAVPCMFile(sampleRate: 10, samples: Array(repeating: 1, count: 4)).encodedData().write(to: systemURL)

        let selection = Pipeline.resolveMeetingAudioSource(micURL: micURL, systemURL: systemURL, mixedURL: mixedURL)

        #expect(selection.audioToTranscribeURL == mixedURL)
        #expect(selection.outputURLs == [micURL, systemURL, mixedURL])
        #expect(selection.note == nil)
        #expect(selection.driftNote?.contains("duration drift detected: 0.60s") == true)
        #expect(selection.driftNote?.contains("--separate-tracks") == true)
        #expect(FileManager.default.fileExists(atPath: mixedURL.path))
    }
}
