import Foundation
import Testing
@testable import sttCore

@Suite("SessionStateStore")
struct SessionStateTests {

    @Test func writeAndReadRoundTripPreservesSessionState() throws {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let startedAt = Date(timeIntervalSince1970: 1_788_000_000)
        let finishedAt = Date(timeIntervalSince1970: 1_788_000_123)
        let state = SessionState(
            runID: "20260704-153300",
            name: "Customer Call",
            mode: .meeting,
            startedAt: startedAt,
            finishedAt: finishedAt,
            durationSeconds: 123.4,
            outputPaths: ["/tmp/mic.wav", "/tmp/system.wav", "/tmp/mixed.wav"],
            separateTracks: true,
            transcribedAudioPath: "/tmp/mixed.wav",
            transcriptTextPath: "/tmp/transcript.txt",
            transcriptJSONPath: "/tmp/transcript.json",
            backend: "vibevoice-mlx",
            notes: "smoke test"
        )

        let metadataURL = try SessionStateStore.write(state, toRunDirectory: tmpDir)
        #expect(metadataURL.lastPathComponent == "metadata.json")
        #expect(FileManager.default.fileExists(atPath: metadataURL.path))

        let decoded = try SessionStateStore.read(fromRunDirectory: tmpDir)
        #expect(decoded == state)
    }

    @Test func writeCreatesRunDirectory() throws {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let runDir = tmpDir.appendingPathComponent("runs/20260704-153300")
        let state = SessionState(runID: "20260704-153300", name: "quick note", mode: .mic)

        let metadataURL = try SessionStateStore.write(state, toRunDirectory: runDir)
        #expect(FileManager.default.fileExists(atPath: metadataURL.path))

        let decoded = try SessionStateStore.read(fromRunDirectory: runDir)
        #expect(decoded.runID == "20260704-153300")
        #expect(decoded.name == "quick note")
        #expect(decoded.mode == .mic)
    }

    @Test func decodesLegacyMetadataWithoutTranscribedAudioPath() throws {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: tmpDir) }
        try FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)

        let legacyJSON = """
        {
          "backend" : "fake-backend",
          "durationSeconds" : 12.5,
          "finishedAt" : "2026-07-04T15:35:00Z",
          "mode" : "meeting",
          "name" : "legacy",
          "outputPaths" : [
            "/tmp/mic.wav",
            "/tmp/system.wav"
          ],
          "runID" : "20260704-153300",
          "separateTracks" : true,
          "startedAt" : "2026-07-04T15:33:00Z",
          "transcriptJSONPath" : "/tmp/legacy.json",
          "transcriptTextPath" : "/tmp/legacy.txt"
        }
        """
        try legacyJSON.write(to: tmpDir.appendingPathComponent("metadata.json"), atomically: true, encoding: .utf8)

        let decoded = try SessionStateStore.read(fromRunDirectory: tmpDir)
        #expect(decoded.runID == "20260704-153300")
        #expect(decoded.mode == .meeting)
        #expect(decoded.outputPaths == ["/tmp/mic.wav", "/tmp/system.wav"])
        #expect(decoded.transcribedAudioPath == nil)
    }
}
