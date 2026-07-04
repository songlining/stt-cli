import Foundation

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

        do {
            try process.run()
        } catch {
            throw ProcessRunnerError.launchFailed(error.localizedDescription)
        }

        var timedOut = false
        if let timeout {
            let deadline = DispatchTime.now() + timeout
            let group = DispatchGroup()
            group.enter()
            DispatchQueue.global().async {
                process.waitUntilExit()
                group.leave()
            }
            if group.wait(timeout: deadline) == .timedOut {
                timedOut = true
                process.terminate()
                process.waitUntilExit()
            }
        } else {
            process.waitUntilExit()
        }

        let stdoutData = stdoutPipe.fileHandleForReading.readDataToEndOfFile()
        let stderrData = stderrPipe.fileHandleForReading.readDataToEndOfFile()
        let stdout = String(data: stdoutData, encoding: .utf8) ?? ""
        let stderr = String(data: stderrData, encoding: .utf8) ?? ""

        if timedOut {
            throw ProcessRunnerError.timedOut(executablePath)
        }

        return ProcessResult(exitCode: process.terminationStatus, standardOutput: stdout, standardError: stderr)
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
