import Foundation

/// Result of `python -m stt_vibevoice.diarize`. Each input segment is
/// returned with its assigned `speaker_id` filled in as a string ("0",
/// "1", ...) plus per-speaker summary statistics.
public struct DiarizationResult: Codable, Equatable {
    public let provider: String
    public let model: String?
    public let embeddingModel: String?
    public let numSpeakers: Int
    public let distanceThreshold: Double?
    public let segments: [DiarizationSegment]
    public let speakers: [DiarizationSpeakerSummary]

    public init(provider: String,
                model: String?,
                embeddingModel: String?,
                numSpeakers: Int,
                distanceThreshold: Double?,
                segments: [DiarizationSegment],
                speakers: [DiarizationSpeakerSummary]) {
        self.provider = provider
        self.model = model
        self.embeddingModel = embeddingModel
        self.numSpeakers = numSpeakers
        self.distanceThreshold = distanceThreshold
        self.segments = segments
        self.speakers = speakers
    }
}

/// One diarised segment. Mirrors the diarize.py segment keys (text,
/// start_time, end_time, duration, speaker_id as a string).
public struct DiarizationSegment: Codable, Equatable {
    /// diarize.py preserves null/missing input fields for unclusterable
    /// segments while still assigning them a speaker id.
    public let text: String?
    public let startTime: Double?
    public let endTime: Double?
    public let duration: Double?
    public let speakerID: String

    public init(text: String?,
                startTime: Double?,
                endTime: Double?,
                duration: Double?,
                speakerID: String) {
        self.text = text
        self.startTime = startTime
        self.endTime = endTime
        self.duration = duration
        self.speakerID = speakerID
    }

    enum CodingKeys: String, CodingKey {
        case text
        case startTime = "start_time"
        case endTime = "end_time"
        case duration
        case speakerID = "speaker_id"
    }
}

/// Per-speaker summary produced by diarize.py.
public struct DiarizationSpeakerSummary: Codable, Equatable {
    public let id: String
    public let segmentCount: Int
    public let totalSpeechSeconds: Double

    public init(id: String, segmentCount: Int, totalSpeechSeconds: Double) {
        self.id = id
        self.segmentCount = segmentCount
        self.totalSpeechSeconds = totalSpeechSeconds
    }
}

public enum PythonDiarizerError: Error, LocalizedError, Equatable {
    case python3NotFound
    case processFailed(exitCode: Int32, stderr: String)
    case timedOut(seconds: TimeInterval)
    case invalidJSONOutput(String)

    public var errorDescription: String? {
        switch self {
        case .python3NotFound:
            return "Could not locate python3 on PATH. Install Python 3 or ensure it is on PATH."
        case .processFailed(let exitCode, let stderr):
            return "Python diarization backend exited with code \(exitCode): \(stderr)"
        case .timedOut(let seconds):
            return "Python diarization backend timed out after \(seconds)s."
        case .invalidJSONOutput(let output):
            return "Could not parse JSON from diarization backend output: \(output.prefix(500))"
        }
    }
}

/// Invokes `python -m stt_vibevoice.diarize` via `Foundation.Process`,
/// mirroring `PythonSpeakerIdentifier`'s process invocation and Python
/// discovery patterns.
public enum PythonDiarizer {

    /// Diarises the segments in `transcriptJSONPath` against `audioPath`,
    /// assigning a `speaker_id` to each segment by clustering per-segment
    /// speaker embeddings.
    public static func diarize(audioPath: String,
                               transcriptJSONPath: String,
                               provider: String,
                               numSpeakers: Int?,
                               distanceThreshold: Double?,
                               minSpeechSeconds: Double?,
                               workingDirectory: URL?,
                               speakerIdOffset: Int = 0,
                               timeout: TimeInterval? = nil) throws -> DiarizationResult {
        guard let python = PythonTranscriber.locatePython3(preferredRuntimeRoot: workingDirectory) else {
            throw PythonDiarizerError.python3NotFound
        }

        let jsonOutputURL = temporaryJSONURL(prefix: "stt-diarize-")
        defer { try? FileManager.default.removeItem(at: jsonOutputURL) }

        var arguments = [
            "-m", "stt_vibevoice.diarize",
            "--audio", audioPath,
            "--segments", transcriptJSONPath,
            "--provider", provider
        ]
        // --num-speakers and --distance-threshold are mutually exclusive in
        // diarize.py's argparse; pass whichever is non-nil. If both are nil,
        // pass neither (diarize.py defaults distance-threshold to 0.15).
        if let numSpeakers {
            arguments += ["--num-speakers", String(numSpeakers)]
        } else if let distanceThreshold {
            arguments += ["--distance-threshold", String(distanceThreshold)]
        }
        if let minSpeechSeconds {
            arguments += ["--min-speech-seconds", String(minSpeechSeconds)]
        }
        if speakerIdOffset > 0 {
            arguments += ["--speaker-id-offset", String(speakerIdOffset)]
        }
        arguments += ["--json", jsonOutputURL.path]

        let result: ProcessResult
        do {
            result = try ProcessRunner.run(
                executablePath: python,
                arguments: arguments,
                currentDirectory: workingDirectory,
                timeout: timeout
            )
        } catch ProcessRunnerError.timedOut {
            throw PythonDiarizerError.timedOut(seconds: timeout ?? 0)
        }

        guard result.succeeded, FileManager.default.fileExists(atPath: jsonOutputURL.path) else {
            throw PythonDiarizerError.processFailed(exitCode: result.exitCode, stderr: result.standardError)
        }

        let data = try Data(contentsOf: jsonOutputURL)
        do {
            return try JSONDecoder().decode(DiarizationResult.self, from: data)
        } catch {
            throw PythonDiarizerError.invalidJSONOutput(String(data: data, encoding: .utf8) ?? "")
        }
    }

    private static func temporaryJSONURL(prefix: String) -> URL {
        FileManager.default.temporaryDirectory.appendingPathComponent("\(prefix)\(UUID().uuidString).json")
    }
}
