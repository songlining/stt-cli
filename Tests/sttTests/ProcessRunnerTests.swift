import Foundation
import Testing
@testable import sttCore

@Suite("ProcessRunner")
struct ProcessRunnerTests {

    @Test func runEchoCapturesStdout() throws {
        let result = try ProcessRunner.run(executablePath: "/bin/echo", arguments: ["hello", "world"])
        #expect(result.succeeded)
        #expect(result.exitCode == 0)
        #expect(result.standardOutput.trimmingCharacters(in: .whitespacesAndNewlines) == "hello world")
        #expect(result.standardError == "")
    }

    @Test func runNonZeroExitIsReportedNotThrown() throws {
        // /usr/bin/false always exits 1 and should not throw, just report failure.
        let result = try ProcessRunner.run(executablePath: "/usr/bin/false")
        #expect(result.succeeded == false)
        #expect(result.exitCode == 1)
    }

    @Test func isOnPathForKnownAndUnknownExecutables() {
        #expect(ProcessRunner.isOnPath("ls"))
        #expect(ProcessRunner.isOnPath("definitely-not-a-real-executable-xyz123") == false)
    }

    @Test func resolvePathReturnsAbsolutePath() {
        let resolved = ProcessRunner.resolvePath("ls")
        #expect(resolved != nil)
        #expect(resolved?.hasPrefix("/") == true)
    }

    @Test func runWithCurrentDirectory() throws {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let result = try ProcessRunner.run(executablePath: "/bin/pwd", currentDirectory: tmpDir)
        #expect(result.succeeded)
        // Resolve symlinks (e.g. /tmp -> /private/tmp on macOS) before comparing.
        let expected = tmpDir.resolvingSymlinksInPath().path
        let actual = result.standardOutput.trimmingCharacters(in: .whitespacesAndNewlines)
        #expect(actual == expected)
    }
}
