import Foundation
#if canImport(Darwin)
import Darwin
#endif

/// Result of running a subprocess to completion.
public struct ProcessResult {
    public let exitCode: Int32
    public let standardOutput: String
    public let standardError: String

    public var succeeded: Bool { exitCode == 0 }
}

public enum ProcessRunnerError: Error, LocalizedError {
    case executableNotFound(String)
    case timedOut(String)
    case launchFailed(String)

    public var errorDescription: String? {
        switch self {
        case .executableNotFound(let name):
            return "Could not find executable on PATH: \(name)"
        case .timedOut(let name):
            return "Process timed out: \(name)"
        case .launchFailed(let reason):
            return "Failed to launch process: \(reason)"
        }
    }
}

/// Generic subprocess execution helper used by PythonTranscriber and the
/// `doctor` command's ffmpeg/ffprobe checks.
public enum ProcessRunner {

    /// Runs an executable at an absolute or PATH-resolved path with the
    /// given arguments, capturing stdout/stderr, with an optional timeout.
    @discardableResult
    public static func run(executablePath: String,
                            arguments: [String] = [],
                            currentDirectory: URL? = nil,
                            environment: [String: String]? = nil,
                            timeout: TimeInterval? = nil) throws -> ProcessResult {
        let process = Process()

        if executablePath.contains("/") {
            process.executableURL = URL(fileURLWithPath: executablePath)
        } else {
            // Let /usr/bin/env resolve it from PATH.
            process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            process.arguments = [executablePath] + arguments
        }
        if process.arguments == nil {
            process.arguments = arguments
        }

        if let currentDirectory {
            process.currentDirectoryURL = currentDirectory
        }
        if let environment {
            process.environment = environment
        }

        let stdoutPipe = Pipe()
        let stderrPipe = Pipe()
        process.standardOutput = stdoutPipe
        process.standardError = stderrPipe

        let stdoutBuffer = LockedOutputBuffer()
        let stderrBuffer = LockedOutputBuffer()
        let outputGroup = DispatchGroup()
        captureOutput(from: stdoutPipe, into: stdoutBuffer, group: outputGroup)
        captureOutput(from: stderrPipe, into: stderrBuffer, group: outputGroup)

        do {
            try process.run()
        } catch {
            stdoutPipe.fileHandleForReading.readabilityHandler = nil
            stderrPipe.fileHandleForReading.readabilityHandler = nil
            throw ProcessRunnerError.launchFailed(error.localizedDescription)
        }

        let processGroup = DispatchGroup()
        processGroup.enter()
        DispatchQueue.global().async {
            process.waitUntilExit()
            processGroup.leave()
        }

        var timedOut = false
        if let timeout {
            if processGroup.wait(timeout: .now() + timeout) == .timedOut {
                timedOut = true
                process.terminate()
                if processGroup.wait(timeout: .now() + terminationGracePeriod) == .timedOut {
                    forceKill(process)
                    _ = processGroup.wait(timeout: .now() + terminationGracePeriod)
                }
            }
        } else {
            processGroup.wait()
        }

        // Wait briefly for EOF so normal processes keep complete stdout/stderr,
        // but never block forever on timeout paths where a child/grandchild keeps
        // inherited pipe file descriptors open after the parent is terminated.
        _ = outputGroup.wait(timeout: .now() + outputDrainGracePeriod)
        stdoutPipe.fileHandleForReading.readabilityHandler = nil
        stderrPipe.fileHandleForReading.readabilityHandler = nil

        let stdout = String(data: stdoutBuffer.data(), encoding: .utf8) ?? ""
        let stderr = String(data: stderrBuffer.data(), encoding: .utf8) ?? ""

        if timedOut {
            throw ProcessRunnerError.timedOut(executablePath)
        }

        return ProcessResult(exitCode: process.terminationStatus, standardOutput: stdout, standardError: stderr)
    }

    private static let terminationGracePeriod: TimeInterval = 0.75
    private static let outputDrainGracePeriod: TimeInterval = 0.5

    private static func captureOutput(from pipe: Pipe, into buffer: LockedOutputBuffer, group: DispatchGroup) {
        group.enter()
        pipe.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            if data.isEmpty {
                handle.readabilityHandler = nil
                group.leave()
            } else {
                buffer.append(data)
            }
        }
    }

    private static func forceKill(_ process: Process) {
        guard process.isRunning else { return }
        #if canImport(Darwin)
        kill(process.processIdentifier, SIGKILL)
        #else
        process.terminate()
        #endif
    }

    private final class LockedOutputBuffer: @unchecked Sendable {
        private let lock = NSLock()
        private var storage = Data()

        func append(_ data: Data) {
            lock.lock()
            storage.append(data)
            lock.unlock()
        }

        func data() -> Data {
            lock.lock()
            defer { lock.unlock() }
            return storage
        }
    }

    /// Returns true if an executable with the given name is resolvable on PATH.
    public static func isOnPath(_ name: String) -> Bool {
        guard let result = try? run(executablePath: "which", arguments: [name]) else {
            return false
        }
        return result.succeeded && !result.standardOutput.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    /// Resolves the absolute path of an executable on PATH, if any.
    public static func resolvePath(_ name: String) -> String? {
        guard let result = try? run(executablePath: "which", arguments: [name]), result.succeeded else {
            return nil
        }
        let path = result.standardOutput.trimmingCharacters(in: .whitespacesAndNewlines)
        return path.isEmpty ? nil : path
    }
}
