import Foundation
import ArgumentParser

public enum TranscriberDevice: String, ExpressibleByArgument, CaseIterable, Codable {
    case auto
    case gpu
    case cpu
}

public struct TranscriptionResult: Codable {
    public let audioPath: String
    public let transcriptText: String?
    public let transcriptTextPath: String?
    public let transcriptJSONPath: String?
    public let backend: String?
    public let language: String?
    public let durationSeconds: Double?
    public let raw: String

    public init(audioPath: String,
                transcriptText: String?,
                transcriptTextPath: String?,
                transcriptJSONPath: String?,
                backend: String?,
                language: String?,
                durationSeconds: Double?,
                raw: String) {
        self.audioPath = audioPath
        self.transcriptText = transcriptText
        self.transcriptTextPath = transcriptTextPath
        self.transcriptJSONPath = transcriptJSONPath
        self.backend = backend
        self.language = language
        self.durationSeconds = durationSeconds
        self.raw = raw
    }
}

public enum PythonTranscriberError: Error, LocalizedError {
    case python3NotFound
    case processFailed(exitCode: Int32, stderr: String)
    case timedOut(seconds: TimeInterval)
    case invalidJSONOutput(String)

    public var errorDescription: String? {
        switch self {
        case .python3NotFound:
            return "Could not locate python3 on PATH. Install Python 3 or ensure it is on PATH."
        case .processFailed(let exitCode, let stderr):
            return "Python transcription backend exited with code \(exitCode): \(stderr)"
        case .timedOut(let seconds):
            return "Python transcription backend timed out after \(formatTimeout(seconds))s. Increase --timeout/--transcribe-timeout, verify the selected model is appropriate for this machine, or check for a hung backend subprocess."
        case .invalidJSONOutput(let output):
            return "Could not parse JSON from transcription backend output: \(output.prefix(500))"
        }
    }

    private func formatTimeout(_ seconds: TimeInterval) -> String {
        if seconds.rounded() == seconds {
            return String(Int(seconds))
        }
        return String(format: "%.3g", seconds)
    }
}

/// Invokes the Python transcription backend (`python -m stt_vibevoice.transcribe`)
/// via `Foundation.Process`, capturing stdout/stderr robustly and parsing the
/// backend's JSON result.
public enum PythonTranscriber {

    /// Locates a usable python3 interpreter, preferring `python3` on PATH.
    public static func locatePython3() -> String? {
        ProcessRunner.resolvePath("python3") ?? ProcessRunner.resolvePath("python")
    }

    /// Runs the Python backend's environment status report. This is safe for
    /// `doctor`: the Python module never imports MLX directly and exits zero
    /// even when the local transcription dependencies are missing.
    public static func statusReport(workingDirectory: URL?, timeout: TimeInterval = 5, requireReady: Bool = false) throws -> ProcessResult {
        guard let python = locatePython3() else {
            throw PythonTranscriberError.python3NotFound
        }
        var arguments = ["-m", "stt_vibevoice.status"]
        if requireReady {
            arguments.append("--fail-if-not-ready")
        }
        do {
            return try ProcessRunner.run(
                executablePath: python,
                arguments: arguments,
                currentDirectory: workingDirectory,
                timeout: timeout
            )
        } catch ProcessRunnerError.timedOut {
            throw PythonTranscriberError.timedOut(seconds: timeout)
        }
    }

    /// Runs the transcription backend against a given audio file.
    ///
    /// - Parameters:
    ///   - audioPath: path to the (already-recorded) audio file.
    ///   - outputTextPath: optional path to write a plain-text transcript.
    ///   - outputJSONPath: optional path to write structured JSON output.
    ///   - device: "auto" | "gpu" | "cpu" — forwarded to the backend.
    ///   - workingDirectory: directory to run the python module from
    ///     (typically the repo's `python/` directory containing `stt_vibevoice`).
    ///   - timeout: optional wall-clock timeout in seconds.
    public static func transcribe(audioPath: String,
                                   outputTextPath: String?,
                                   outputJSONPath: String?,
                                   device: String,
                                   workingDirectory: URL?,
                                   timeout: TimeInterval? = nil,
                                   modelPath: String? = nil,
                                   maxNewTokens: Int? = nil) throws -> TranscriptionResult {
        guard let python = locatePython3() else {
            throw PythonTranscriberError.python3NotFound
        }

        let arguments = buildTranscribeArguments(
            audioPath: audioPath,
            outputTextPath: outputTextPath,
            outputJSONPath: outputJSONPath,
            device: device,
            modelPath: modelPath,
            maxNewTokens: maxNewTokens
        )

        let result: ProcessResult
        do {
            result = try ProcessRunner.run(
                executablePath: python,
                arguments: arguments,
                currentDirectory: workingDirectory,
                timeout: timeout
            )
        } catch ProcessRunnerError.timedOut {
            throw PythonTranscriberError.timedOut(seconds: timeout ?? 0)
        }

        guard result.succeeded else {
            throw PythonTranscriberError.processFailed(exitCode: result.exitCode, stderr: result.standardError)
        }

        return try parseResult(audioPath: audioPath,
                                stdout: result.standardOutput,
                                outputTextPath: outputTextPath,
                                outputJSONPath: outputJSONPath)
    }

    public static func buildTranscribeArguments(audioPath: String,
                                                outputTextPath: String?,
                                                outputJSONPath: String?,
                                                device: String,
                                                modelPath: String? = nil,
                                                maxNewTokens: Int? = nil) -> [String] {
        var arguments = ["-m", "stt_vibevoice.transcribe", audioPath, "--device", device]
        if let outputTextPath {
            arguments += ["--output", outputTextPath]
        }
        if let outputJSONPath {
            arguments += ["--json", outputJSONPath]
        }
        if let modelPath, !modelPath.isEmpty {
            arguments += ["--model", modelPath]
        }
        if let maxNewTokens {
            arguments += ["--max-new-tokens", String(maxNewTokens)]
        }
        return arguments
    }

    /// Parses the backend's stdout, expecting a single JSON object somewhere
    /// in the output (tolerating log lines before/after it).
    public static func parseResult(audioPath: String,
                                    stdout: String,
                                    outputTextPath: String?,
                                    outputJSONPath: String?) throws -> TranscriptionResult {
        guard let jsonObject = extractJSONObject(from: stdout) else {
            // Not fatal: the backend may only have written files without
            // JSON on stdout. Surface whatever text is available.
            return TranscriptionResult(
                audioPath: audioPath,
                transcriptText: stdout.isEmpty ? nil : stdout,
                transcriptTextPath: outputTextPath,
                transcriptJSONPath: outputJSONPath,
                backend: nil,
                language: nil,
                durationSeconds: nil,
                raw: stdout
            )
        }

        let backend = jsonObject["backend"] as? String
        let language = (jsonObject["detected_language"] as? String) ?? (jsonObject["language"] as? String)
        let duration = jsonObject["duration"] as? Double
        let text = jsonObject["transcript_text"] as? String ?? jsonObject["text"] as? String

        return TranscriptionResult(
            audioPath: audioPath,
            transcriptText: text,
            transcriptTextPath: outputTextPath ?? (jsonObject["transcript_file"] as? String),
            transcriptJSONPath: outputJSONPath,
            backend: backend,
            language: language,
            durationSeconds: duration,
            raw: stdout
        )
    }

    /// Finds and decodes the first valid top-level JSON object substring in
    /// a blob of text (defensive against extra log lines around it, including
    /// log lines that themselves contain braces).
    private static func extractJSONObject(from text: String) -> [String: Any]? {
        let startIndices = text.indices.filter { text[$0] == "{" }
        let endIndices = text.indices.filter { text[$0] == "}" }

        var lastObject: [String: Any]?
        for start in startIndices {
            for end in endIndices where start < end {
                let candidate = String(text[start...end])
                guard let data = candidate.data(using: .utf8),
                      let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                    continue
                }
                lastObject = object
            }
        }
        return lastObject
    }
}
