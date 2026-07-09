import Foundation

/// Result of `python -m stt_vibevoice.speaker_id concatenate`, which slices
/// and concatenates one diarized speaker's segments into a single playable
/// WAV (no ML). Used by the interactive `stt name-speakers` loop.
public struct SpeakerConcatenateResult: Codable, Equatable {
    public let speakerId: String?
    public let outputPath: String?
    public let segmentCount: Int
    public let durationSeconds: Double
    public let status: String
    /// Linear gain applied when `--normalize` was requested (1.0 if no
    /// normalization was applied or the input was already at/above target).
    public let normalizedGain: Double?

    public init(speakerId: String?,
                outputPath: String?,
                segmentCount: Int,
                durationSeconds: Double,
                status: String,
                normalizedGain: Double? = nil) {
        self.speakerId = speakerId
        self.outputPath = outputPath
        self.segmentCount = segmentCount
        self.durationSeconds = durationSeconds
        self.status = status
        self.normalizedGain = normalizedGain
    }

    public var isOK: Bool { status == "ok" }
}

/// Result of `python -m stt_vibevoice.speaker_id extract`, for either a
/// whole-file enrollment sample or a per-speaker segment extraction.
public struct SpeakerExtractionResult: Codable, Equatable {
    public let speakerId: String?
    public let provider: String
    public let model: String?
    public let embedding: [Double]?
    public let durationSeconds: Double
    public let segmentCount: Int
    public let status: String

    public init(speakerId: String?,
                provider: String,
                model: String?,
                embedding: [Double]?,
                durationSeconds: Double,
                segmentCount: Int,
                status: String) {
        self.speakerId = speakerId
        self.provider = provider
        self.model = model
        self.embedding = embedding
        self.durationSeconds = durationSeconds
        self.segmentCount = segmentCount
        self.status = status
    }

    public var isTooShort: Bool { status == "too_short" }
    public var isOK: Bool { status == "ok" }
}

/// A single scored candidate profile from `python -m stt_vibevoice.speaker_id match`.
public struct SpeakerMatchCandidateScore: Codable, Equatable {
    public let profileId: String?
    public let displayName: String?
    public let confidence: Double

    public init(profileId: String?, displayName: String?, confidence: Double) {
        self.profileId = profileId
        self.displayName = displayName
        self.confidence = confidence
    }
}

/// The best-scoring candidate, with the conflict-policy status Python
/// computed from `threshold`/`margin` for that single candidate (Swift
/// still owns cross-speaker conflict resolution across all diarized
/// speakers; see `SpeakerLabelResolver`).
public struct SpeakerMatchBestMatch: Codable, Equatable {
    public let profileId: String?
    public let displayName: String?
    public let confidence: Double
    public let margin: Double
    public let matched: Bool
    public let status: String

    public init(profileId: String?, displayName: String?, confidence: Double, margin: Double, matched: Bool, status: String) {
        self.profileId = profileId
        self.displayName = displayName
        self.confidence = confidence
        self.margin = margin
        self.matched = matched
        self.status = status
    }
}

public struct SpeakerMatchSkippedProfile: Codable, Equatable {
    public let profileId: String?
    public let displayName: String?
    public let reason: String

    public init(profileId: String?, displayName: String?, reason: String) {
        self.profileId = profileId
        self.displayName = displayName
        self.reason = reason
    }
}

public struct SpeakerMatchResult: Codable, Equatable {
    public let bestMatch: SpeakerMatchBestMatch?
    public let candidates: [SpeakerMatchCandidateScore]
    public let skippedProfiles: [SpeakerMatchSkippedProfile]
    public let warnings: [String]

    public init(bestMatch: SpeakerMatchBestMatch?,
                candidates: [SpeakerMatchCandidateScore],
                skippedProfiles: [SpeakerMatchSkippedProfile],
                warnings: [String]) {
        self.bestMatch = bestMatch
        self.candidates = candidates
        self.skippedProfiles = skippedProfiles
        self.warnings = warnings
    }
}

public enum PythonSpeakerIdentifierError: Error, LocalizedError, Equatable {
    case python3NotFound
    case processFailed(exitCode: Int32, stderr: String)
    case timedOut(seconds: TimeInterval)
    case invalidJSONOutput(String)

    public var errorDescription: String? {
        switch self {
        case .python3NotFound:
            return "Could not locate python3 on PATH. Install Python 3 or ensure it is on PATH."
        case .processFailed(let exitCode, let stderr):
            return "Python speaker-id backend exited with code \(exitCode): \(stderr)"
        case .timedOut(let seconds):
            return "Python speaker-id backend timed out after \(seconds)s."
        case .invalidJSONOutput(let output):
            return "Could not parse JSON from speaker-id backend output: \(output.prefix(500))"
        }
    }
}

/// Invokes `python -m stt_vibevoice.speaker_id` (extract/match subcommands)
/// via `Foundation.Process`, mirroring `PythonTranscriber`'s process
/// invocation and Python discovery patterns.
public enum PythonSpeakerIdentifier {

    /// Extracts an embedding from an entire audio file (the enrollment
    /// path: a clean, single-speaker sample).
    public static func extractWholeAudio(audioPath: String,
                                         provider: String,
                                         minimumSpeechSeconds: Double,
                                         workingDirectory: URL?,
                                         timeout: TimeInterval? = nil) throws -> SpeakerExtractionResult {
        try runExtract(
            arguments: buildExtractArguments(
                audioPath: audioPath,
                segmentsJSONPath: nil,
                speakerID: nil,
                provider: provider,
                minimumSpeechSeconds: minimumSpeechSeconds
            ),
            workingDirectory: workingDirectory,
            timeout: timeout
        )
    }

    /// Extracts an embedding for one diarized speaker from a full
    /// recording plus its transcript JSON (the relabeling path).
    public static func extractSpeakerSegments(audioPath: String,
                                              segmentsJSONPath: String,
                                              speakerID: String,
                                              provider: String,
                                              minimumSpeechSeconds: Double,
                                              workingDirectory: URL?,
                                              timeout: TimeInterval? = nil) throws -> SpeakerExtractionResult {
        try runExtract(
            arguments: buildExtractArguments(
                audioPath: audioPath,
                segmentsJSONPath: segmentsJSONPath,
                speakerID: speakerID,
                provider: provider,
                minimumSpeechSeconds: minimumSpeechSeconds
            ),
            workingDirectory: workingDirectory,
            timeout: timeout
        )
    }

    /// Builds a single playable WAV from one diarized speaker's segments by
    /// invoking `python -m stt_vibevoice.speaker_id concatenate`. No ML is
    /// involved, so this runs quickly in either Python venv. Used by the
    /// interactive `stt name-speakers` loop to obtain a preview+enrollment
    /// clip per speaker.
    public static func concatenate(audioPath: String,
                                   segmentsJSONPath: String,
                                   speakerID: String,
                                   outPath: String,
                                   workingDirectory: URL?,
                                   maxSeconds: Double? = nil,
                                   normalize: Bool = false,
                                   targetLoudness: Double = -19.0,
                                   timeout: TimeInterval? = nil) throws -> SpeakerConcatenateResult {
        guard let python = PythonTranscriber.locatePython3(preferredRuntimeRoot: workingDirectory) else {
            throw PythonSpeakerIdentifierError.python3NotFound
        }

        let jsonOutputURL = temporaryJSONURL(prefix: "stt-speaker-concatenate-")
        defer { try? FileManager.default.removeItem(at: jsonOutputURL) }

        var arguments = [
            "-m", "stt_vibevoice.speaker_id", "concatenate",
            "--audio", audioPath,
            "--segments", segmentsJSONPath,
            "--speaker-id", speakerID,
            "--out", outPath
        ]
        if let maxSeconds, maxSeconds > 0 {
            arguments += ["--max-seconds", String(format: "%.1f", maxSeconds)]
        }
        if normalize {
            arguments += ["--normalize", "--target-loudness", String(format: "%.1f", targetLoudness)]
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
            throw PythonSpeakerIdentifierError.timedOut(seconds: timeout ?? 0)
        }

        // `concatenate` exits non-zero (and still writes structured JSON) when
        // the speaker has no usable segments; only treat this as a hard failure
        // when no JSON output was produced at all.
        guard FileManager.default.fileExists(atPath: jsonOutputURL.path) else {
            throw PythonSpeakerIdentifierError.processFailed(exitCode: result.exitCode, stderr: result.standardError)
        }

        let data = try Data(contentsOf: jsonOutputURL)
        do {
            return try JSONDecoder().decode(SpeakerConcatenateResult.self, from: data)
        } catch {
            throw PythonSpeakerIdentifierError.invalidJSONOutput(String(data: data, encoding: .utf8) ?? "")
        }
    }

    public static func buildExtractArguments(audioPath: String,
                                             segmentsJSONPath: String?,
                                             speakerID: String?,
                                             provider: String,
                                             minimumSpeechSeconds: Double) -> [String] {
        var arguments = ["-m", "stt_vibevoice.speaker_id", "extract", "--audio", audioPath]
        if let segmentsJSONPath {
            arguments += ["--segments", segmentsJSONPath]
        }
        if let speakerID {
            arguments += ["--speaker-id", speakerID]
        }
        arguments += ["--provider", provider, "--minimum-speech-seconds", String(minimumSpeechSeconds)]
        return arguments
    }

    /// Matches a candidate embedding (already extracted, at `candidateJSONPath`)
    /// against a flattened profile list (at `profilesJSONPath`).
    public static func match(candidateJSONPath: String,
                             profilesJSONPath: String,
                             threshold: Double,
                             margin: Double,
                             workingDirectory: URL?,
                             timeout: TimeInterval? = nil) throws -> SpeakerMatchResult {
        guard let python = PythonTranscriber.locatePython3(preferredRuntimeRoot: workingDirectory) else {
            throw PythonSpeakerIdentifierError.python3NotFound
        }

        let jsonOutputURL = temporaryJSONURL(prefix: "stt-speaker-match-")
        defer { try? FileManager.default.removeItem(at: jsonOutputURL) }

        let arguments = [
            "-m", "stt_vibevoice.speaker_id", "match",
            "--candidate", candidateJSONPath,
            "--profiles", profilesJSONPath,
            "--threshold", String(threshold),
            "--margin", String(margin),
            "--json", jsonOutputURL.path
        ]

        let result: ProcessResult
        do {
            result = try ProcessRunner.run(
                executablePath: python,
                arguments: arguments,
                currentDirectory: workingDirectory,
                timeout: timeout
            )
        } catch ProcessRunnerError.timedOut {
            throw PythonSpeakerIdentifierError.timedOut(seconds: timeout ?? 0)
        }

        guard result.succeeded, FileManager.default.fileExists(atPath: jsonOutputURL.path) else {
            throw PythonSpeakerIdentifierError.processFailed(exitCode: result.exitCode, stderr: result.standardError)
        }

        let data = try Data(contentsOf: jsonOutputURL)
        do {
            return try JSONDecoder().decode(SpeakerMatchResult.self, from: data)
        } catch {
            throw PythonSpeakerIdentifierError.invalidJSONOutput(String(data: data, encoding: .utf8) ?? "")
        }
    }

    private static func runExtract(arguments: [String], workingDirectory: URL?, timeout: TimeInterval?) throws -> SpeakerExtractionResult {
        guard let python = PythonTranscriber.locatePython3(preferredRuntimeRoot: workingDirectory) else {
            throw PythonSpeakerIdentifierError.python3NotFound
        }

        let jsonOutputURL = temporaryJSONURL(prefix: "stt-speaker-extract-")
        defer { try? FileManager.default.removeItem(at: jsonOutputURL) }

        let fullArguments = arguments + ["--json", jsonOutputURL.path]

        let result: ProcessResult
        do {
            result = try ProcessRunner.run(
                executablePath: python,
                arguments: fullArguments,
                currentDirectory: workingDirectory,
                timeout: timeout
            )
        } catch ProcessRunnerError.timedOut {
            throw PythonSpeakerIdentifierError.timedOut(seconds: timeout ?? 0)
        }

        // `extract` exits non-zero (but still writes structured JSON) for the
        // expected `too_short` case; only treat this as a hard failure when
        // no JSON output was produced at all.
        guard FileManager.default.fileExists(atPath: jsonOutputURL.path) else {
            throw PythonSpeakerIdentifierError.processFailed(exitCode: result.exitCode, stderr: result.standardError)
        }

        let data = try Data(contentsOf: jsonOutputURL)
        do {
            return try JSONDecoder().decode(SpeakerExtractionResult.self, from: data)
        } catch {
            throw PythonSpeakerIdentifierError.invalidJSONOutput(String(data: data, encoding: .utf8) ?? "")
        }
    }

    private static func temporaryJSONURL(prefix: String) -> URL {
        FileManager.default.temporaryDirectory.appendingPathComponent("\(prefix)\(UUID().uuidString).json")
    }

    // MARK: - Flattened match input helpers

    /// Writes the flattened profile list Python's `match` command expects,
    /// per the "Flattened match input contract" in
    /// `SPEAKER_IDENTIFICATION_PLAN.md`. Swift owns the profile directory
    /// layout; Python only ever sees this flattened, temporary payload.
    public static func writeFlattenedProfiles(_ profiles: [SpeakerProfile], to url: URL) throws {
        let flattened = profiles.map {
            FlattenedSpeakerProfile(
                id: $0.id.uuidString,
                displayName: $0.displayName,
                embeddingProvider: $0.embeddingProvider,
                embeddingModel: $0.embeddingModel,
                embedding: $0.embedding
            )
        }
        let payload = FlattenedSpeakerProfiles(profiles: flattened)
        let data = try JSONEncoder().encode(payload)
        try data.write(to: url, options: .atomic)
    }

    /// Writes a candidate embedding (as produced by `extractWholeAudio`/
    /// `extractSpeakerSegments`) to a temporary JSON file for `match` to read.
    public static func writeCandidate(_ extraction: SpeakerExtractionResult, to url: URL) throws {
        let data = try JSONEncoder().encode(extraction)
        try data.write(to: url, options: .atomic)
    }
}

/// One profile entry in the flattened match-input payload sent to Python.
/// See "Flattened match input contract" in `SPEAKER_IDENTIFICATION_PLAN.md`.
public struct FlattenedSpeakerProfile: Codable, Equatable {
    public let id: String
    public let displayName: String
    public let embeddingProvider: String
    public let embeddingModel: String
    public let embedding: [Double]

    public init(id: String, displayName: String, embeddingProvider: String, embeddingModel: String, embedding: [Double]) {
        self.id = id
        self.displayName = displayName
        self.embeddingProvider = embeddingProvider
        self.embeddingModel = embeddingModel
        self.embedding = embedding
    }
}

public struct FlattenedSpeakerProfiles: Codable, Equatable {
    public let profiles: [FlattenedSpeakerProfile]

    public init(profiles: [FlattenedSpeakerProfile]) {
        self.profiles = profiles
    }
}
