import Foundation
import Testing
@testable import sttCore

@Suite("Record meeting mix outcome")
struct RecordMeetingMixOutcomeTests {

    @Test func mixesSuccessfullyWhenSampleRatesMatch() throws {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let micURL = tmpDir.appendingPathComponent("mic.wav")
        let systemURL = tmpDir.appendingPathComponent("system.wav")
        let mixedURL = tmpDir.appendingPathComponent("mixed.wav")
        try WAVPCMFile(sampleRate: 8_000, samples: [1_000, 2_000]).encodedData().write(to: micURL)
        try WAVPCMFile(sampleRate: 8_000, samples: [3_000]).encodedData().write(to: systemURL)

        let outcome = Record.resolveMeetingMixOutcome(
            micResult: RecordingResult(outputURL: micURL, durationSeconds: 0.25, fileSizeBytes: 48),
            systemResult: RecordingResult(outputURL: systemURL, durationSeconds: 0.25, fileSizeBytes: 46),
            mixedURL: mixedURL
        )

        #expect(outcome.note == nil)
        #expect(outcome.mixedResult?.outputURL == mixedURL)
        #expect(outcome.mixedResult?.fileSizeBytes == UInt64(44 + 4))
        #expect(FileManager.default.fileExists(atPath: mixedURL.path))

        let mixed = try WAVPCMFile.parse(Data(contentsOf: mixedURL))
        #expect(mixed.samples == [4_000, 2_000])
    }

    @Test func fallsBackWithNoteWhenSampleRatesMismatch() throws {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let micURL = tmpDir.appendingPathComponent("mic.wav")
        let systemURL = tmpDir.appendingPathComponent("system.wav")
        let mixedURL = tmpDir.appendingPathComponent("mixed.wav")
        try WAVPCMFile(sampleRate: 16_000, samples: [1]).encodedData().write(to: micURL)
        try WAVPCMFile(sampleRate: 44_100, samples: [1]).encodedData().write(to: systemURL)

        let outcome = Record.resolveMeetingMixOutcome(
            micResult: RecordingResult(outputURL: micURL, durationSeconds: 0.1, fileSizeBytes: 46),
            systemResult: RecordingResult(outputURL: systemURL, durationSeconds: 0.1, fileSizeBytes: 46),
            mixedURL: mixedURL
        )

        #expect(outcome.mixedResult == nil)
        #expect(outcome.note?.contains("Mixed track unavailable") == true)
        #expect(outcome.note?.contains("mic.wav and system.wav remain available separately") == true)
        #expect(!FileManager.default.fileExists(atPath: mixedURL.path))
    }
}
