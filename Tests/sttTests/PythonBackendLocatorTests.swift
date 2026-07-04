import Foundation
import Testing
@testable import sttCore

@Suite("Python backend locator")
struct PythonBackendLocatorTests {

    @Test func environmentOverrideWinsWhenDirectoryExists() throws {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let envBackend = tmpDir.appendingPathComponent("env-python", isDirectory: true)
        let cwdBackend = tmpDir.appendingPathComponent("repo/python", isDirectory: true)
        try FileManager.default.createDirectory(at: envBackend, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: cwdBackend, withIntermediateDirectories: true)

        let located = Transcribe.findPythonBackendDirectory(
            environment: ["STT_PYTHON_BACKEND": envBackend.path],
            currentDirectory: tmpDir.appendingPathComponent("repo", isDirectory: true),
            bundleResourceURL: nil
        )

        #expect(located?.path == envBackend.path)
    }

    @Test func currentDirectoryPythonIsUsedBeforeBundleResource() throws {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let repo = tmpDir.appendingPathComponent("repo", isDirectory: true)
        let cwdBackend = repo.appendingPathComponent("python", isDirectory: true)
        let bundleBackend = tmpDir.appendingPathComponent("bundle/python", isDirectory: true)
        try FileManager.default.createDirectory(at: cwdBackend, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: bundleBackend, withIntermediateDirectories: true)

        let located = Transcribe.findPythonBackendDirectory(
            environment: [:],
            currentDirectory: repo,
            bundleResourceURL: tmpDir.appendingPathComponent("bundle", isDirectory: true)
        )

        #expect(located?.path == cwdBackend.path)
    }

    @Test func bundleResourcePythonIsUsedWhenCurrentDirectoryHasNoBackend() throws {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let bundleResource = tmpDir.appendingPathComponent("Resources", isDirectory: true)
        let bundleBackend = bundleResource.appendingPathComponent("python", isDirectory: true)
        try FileManager.default.createDirectory(at: bundleBackend, withIntermediateDirectories: true)

        let located = Transcribe.findPythonBackendDirectory(
            environment: [:],
            currentDirectory: tmpDir.appendingPathComponent("elsewhere", isDirectory: true),
            bundleResourceURL: bundleResource
        )

        #expect(located?.path == bundleBackend.path)
    }

    @Test func missingBackendReturnsNil() {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let located = Transcribe.findPythonBackendDirectory(
            environment: [:],
            currentDirectory: tmpDir,
            bundleResourceURL: nil
        )

        #expect(located == nil)
    }
}
