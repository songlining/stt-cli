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
            outputPaths: ["/tmp/mic.wav", "/tmp/system.wav"],
            separateTracks: true,
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
}
