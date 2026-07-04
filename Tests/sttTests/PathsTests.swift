import Foundation
import Testing
@testable import sttCore

@Suite("Paths")
struct PathsTests {

    @Test func appSupportDirectoryHonoursSTTHomeOverride() {
        let env = ["STT_HOME": "/tmp/stt-test-home"]
        let dir = Paths.appSupportDirectory(environment: env)
        #expect(dir.path == "/tmp/stt-test-home")
    }

    @Test func subdirectoriesAreNestedUnderAppSupport() {
        let env = ["STT_HOME": "/tmp/stt-test-home-2"]
        #expect(Paths.recordingsDirectory(environment: env).path == "/tmp/stt-test-home-2/recordings")
        #expect(Paths.transcriptsDirectory(environment: env).path == "/tmp/stt-test-home-2/transcripts")
        #expect(Paths.runsDirectory(environment: env).path == "/tmp/stt-test-home-2/runs")
        #expect(Paths.runDirectory(runID: "20260101-000000", environment: env).path ==
                "/tmp/stt-test-home-2/runs/20260101-000000")
    }

    @Test func ensureDirectoryExistsCreatesAndIsIdempotent() throws {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let nested = tmpDir.appendingPathComponent("a/b/c")
        let created = try Paths.ensureDirectoryExists(nested)
        #expect(created.path == nested.path)

        var isDir: ObjCBool = false
        #expect(FileManager.default.fileExists(atPath: nested.path, isDirectory: &isDir))
        #expect(isDir.boolValue)

        // Calling again should not throw.
        _ = try Paths.ensureDirectoryExists(nested)
    }

    @Test func ensureDirectoryExistsThrowsIfPathIsAFile() throws {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let filePath = tmpDir.appendingPathComponent("not-a-dir")
        FileManager.default.createFile(atPath: filePath.path, contents: Data("x".utf8))

        #expect(throws: (any Error).self) {
            try Paths.ensureDirectoryExists(filePath)
        }
    }

    @Test func timestampTokenFormat() {
        var components = DateComponents()
        components.year = 2026
        components.month = 7
        components.day = 4
        components.hour = 14
        components.minute = 32
        components.second = 10
        components.timeZone = TimeZone.current
        let calendar = Calendar(identifier: .gregorian)
        let date = calendar.date(from: components)!

        #expect(Paths.timestampToken(date: date) == "20260704-143210")
    }
}
