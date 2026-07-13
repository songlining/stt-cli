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
    /// recording plus its transcript JSON (the relabeling path). When
    /// `ranges` is provided, only speech within those time ranges is used
    /// for extraction (range-aware enrollment).
    public static func extractSpeakerSegments(audioPath: String,
                                              segmentsJSONPath: String,
                                              speakerID: String,
                                              provider: String,
                                              minimumSpeechSeconds: Double,
                                              workingDirectory: URL?,
                                              ranges: [String]? = nil,
                                              timeout: TimeInterval? = nil) throws -> SpeakerExtractionResult {
        try runExtract(
            arguments: buildExtractArguments(
                audioPath: audioPath,
                segmentsJSONPath: segmentsJSONPath,
                speakerID: speakerID,
                provider: provider,
                minimumSpeechSeconds: minimumSpeechSeconds,
                ranges: ranges
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
                                   ranges: [String]? = nil,
                                   bestSegments: Bool = true,
                                   timeout: TimeInterval? = nil) throws -> SpeakerConcatenateResult {
        guard let python = PythonTranscriber.locatePython3(preferredRuntimeRoot: workingDirectory) else {
            throw PythonSpeakerIdentifierError.python3NotFound
        }

        let jsonOutputURL = temporaryJSONURL(prefix: "stt-speaker-concatenate-")
        defer { try? FileManager.default.removeItem(at: jsonOutputURL) }

        let arguments = buildConcatenateArguments(
            audioPath: audioPath,
            segmentsJSONPath: segmentsJSONPath,
            speakerID: speakerID,
            outPath: outPath,
            jsonOutputPath: jsonOutputURL.path,
            maxSeconds: maxSeconds,
            normalize: normalize,
            targetLoudness: targetLoudness,
            ranges: ranges,
            bestSegments: bestSegments
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

    /// Builds argument list for `python -m stt_vibevoice.speaker_id concatenate`.
    /// Extracted from `concatenate` so argument construction can be unit-tested
    /// without invoking Python.
    public static func buildConcatenateArguments(audioPath: String,
                                                 segmentsJSONPath: String,
                                                 speakerID: String,
                                                 outPath: String,
                                                 jsonOutputPath: String,
                                                 maxSeconds: Double? = nil,
                                                 normalize: Bool = false,
                                                 targetLoudness: Double = -19.0,
                                                 ranges: [String]? = nil,
                                                 bestSegments: Bool = true) -> [String] {
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
        if bestSegments {
            // --best-segments is the default in the backend; emit it
            // explicitly for clarity (it's a no-op but documents intent).
            arguments += ["--best-segments"]
        } else {
            arguments += ["--no-best-segments"]
        }
        if normalize {
            arguments += ["--normalize", "--target-loudness", String(format: "%.1f", targetLoudness)]
        }
        if let ranges, !ranges.isEmpty {
            for range in ranges {
                arguments += ["--range", range]
            }
        }
        arguments += ["--json", jsonOutputPath]
        return arguments
    }

    public static func buildExtractArguments(audioPath: String,
                                             segmentsJSONPath: String?,
                                             speakerID: String?,
                                             provider: String,
                                             minimumSpeechSeconds: Double,
                                             ranges: [String]? = nil) -> [String] {
        var arguments = ["-m", "stt_vibevoice.speaker_id", "extract", "--audio", audioPath]
        if let segmentsJSONPath {
            arguments += ["--segments", segmentsJSONPath]
        }
        if let speakerID {
            arguments += ["--speaker-id", speakerID]
        }
        arguments += ["--provider", provider, "--minimum-speech-seconds", String(minimumSpeechSeconds)]
        if let ranges, !ranges.isEmpty {
            for range in ranges {
                arguments += ["--range", range]
            }
        }
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

    // MARK: - suggest-labels (stable primitive: speaker_id.py)

    /// Builds argument list for `python -m stt_vibevoice.speaker_id suggest-labels`.
    /// `audioSources` is a list of `(source, path)` pairs that become repeated
    /// `--audio source=path` arguments. Extracted for unit testing.
    public static func buildSuggestLabelsArguments(transcript: String,
                                                   audioSources: [(source: String, path: String)],
                                                   profilesPath: String?,
                                                   provider: String,
                                                   threshold: Double,
                                                   margin: Double,
                                                   minimumSpeechSeconds: Double,
                                                   session: String?,
                                                   noWindows: Bool,
                                                   nWindows: Int,
                                                   jsonOutputPath: String?) -> [String] {
        var arguments = [
            "-m", "stt_vibevoice.speaker_id", "suggest-labels",
            "--transcript", transcript
        ]
        for source in audioSources {
            arguments += ["--audio", "\(source.source)=\(source.path)"]
        }
        if let profilesPath {
            arguments += ["--profiles", profilesPath]
        }
        arguments += [
            "--provider", provider,
            "--threshold", String(threshold),
            "--margin", String(margin),
            "--minimum-speech-seconds", String(minimumSpeechSeconds)
        ]
        if let session {
            arguments += ["--session", session]
        }
        if noWindows {
            arguments += ["--no-windows"]
        } else {
            arguments += ["--n-windows", String(nWindows)]
        }
        if let jsonOutputPath {
            arguments += ["--json", jsonOutputPath]
        }
        return arguments
    }

    /// Runs `python -m stt_vibevoice.speaker_id suggest-labels` and returns
    /// the raw JSON output as `Data`. The caller (CLI command) decides how to
    /// present it. This is a non-mutating read-only operation: it never writes
    /// to the transcript or profile files.
    ///
    /// When `profilesPath` is nil, an empty profile list is used (the
    /// `no_profiles` state). When `jsonOutputPath` is nil, the result is
    /// captured from stdout.
    public static func suggestLabels(transcript: String,
                                     audioSources: [(source: String, path: String)],
                                     profilesPath: String?,
                                     provider: String,
                                     threshold: Double,
                                     margin: Double,
                                     minimumSpeechSeconds: Double,
                                     session: String?,
                                     noWindows: Bool,
                                     nWindows: Int,
                                     jsonOutputPath: String?,
                                     workingDirectory: URL?,
                                     timeout: TimeInterval? = nil) throws -> SpeakerSuggestionResult {
        guard let python = PythonTranscriber.locatePython3(preferredRuntimeRoot: workingDirectory) else {
            throw PythonSpeakerIdentifierError.python3NotFound
        }

        let jsonOutputURL = temporaryJSONURL(prefix: "stt-speaker-suggest-")
        defer { try? FileManager.default.removeItem(at: jsonOutputURL) }

        let arguments = buildSuggestLabelsArguments(
            transcript: transcript,
            audioSources: audioSources,
            profilesPath: profilesPath,
            provider: provider,
            threshold: threshold,
            margin: margin,
            minimumSpeechSeconds: minimumSpeechSeconds,
            session: session,
            noWindows: noWindows,
            nWindows: nWindows,
            jsonOutputPath: jsonOutputURL.path
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
            throw PythonSpeakerIdentifierError.timedOut(seconds: timeout ?? 0)
        }

        guard FileManager.default.fileExists(atPath: jsonOutputURL.path) else {
            throw PythonSpeakerIdentifierError.processFailed(exitCode: result.exitCode, stderr: result.standardError)
        }

        let data = try Data(contentsOf: jsonOutputURL)
        do {
            return try JSONDecoder().decode(SpeakerSuggestionResult.self, from: data)
        } catch {
            throw PythonSpeakerIdentifierError.invalidJSONOutput(String(data: data, encoding: .utf8) ?? "")
        }
    }

    // MARK: - Helper script runner (audit / purity-preview / enroll-ranges)

    /// Default helper script search path. The helper script
    /// (``name_one_speaker.py``) lives outside this repo (in the Pi skills
    /// directory). Override with ``STT_HELPER_SCRIPTS`` env var or the
    /// ``--helper-script`` CLI option.
    public static func defaultHelperScriptsDirectory(environment: [String: String] = ProcessInfo.processInfo.environment) -> String? {
        if let envValue = environment["STT_HELPER_SCRIPTS"], !envValue.isEmpty {
            return envValue
        }
        // Known default on the development machine.
        return "\(NSHomeDirectory())/.pi/agent/skills/stt-meeting-recordings/scripts"
    }

    /// Resolves the helper script path. Returns the full path to
    /// ``name_one_speaker.py`` or nil if it cannot be found.
    public static func resolveHelperScriptPath(explicitOverride: String?,
                                                environment: [String: String] = ProcessInfo.processInfo.environment,
                                                fileManager: FileManager = .default) -> String? {
        let scriptsDir = explicitOverride.flatMap { $0.isEmpty ? nil : $0 } ?? defaultHelperScriptsDirectory(environment: environment)
        guard let scriptsDir else { return nil }
        let scriptPath = URL(fileURLWithPath: scriptsDir).appendingPathComponent("name_one_speaker.py").path
        return fileManager.fileExists(atPath: scriptPath) ? scriptPath : nil
    }

    /// Builds argument list for invoking the helper script with a given
    /// subcommand. The first element is the script path itself; the Python
    /// executable is prepended by `runHelperScript`.
    ///
    /// Each entry in `rangeArguments` produces a `--range <value>` pair.
    public static func buildHelperScriptArguments(subcommand: String,
                                                  scriptPath: String,
                                                  rangeArguments: [String],
                                                  keywordArguments: [(flag: String, value: String?)]) -> [String] {
        var arguments = [scriptPath, subcommand]
        for range in rangeArguments {
            arguments += ["--range", range]
        }
        for (flag, value) in keywordArguments {
            if let value {
                arguments += [flag, value]
            } else {
                arguments += [flag]
            }
        }
        return arguments
    }

    /// Runs the helper script (``name_one_speaker.py``) with the given
    /// arguments. The helper script is invoked via the Python 3.11 runtime
    /// venv (``runtime/.venv``) when available, falling back to system
    /// python3. Returns the raw stdout (the helper prints JSON to stdout).
    ///
    /// The helper script is the agent-friendly wrapper that orchestrates
    /// audit, purity-preview, and enroll-ranges by calling the same
    /// ``stt_vibevoice.speaker_id`` backend primitives this bridge uses.
    public static func runHelperScript(scriptPath: String,
                                       arguments: [String],
                                       workingDirectory: URL?,
                                       timeout: TimeInterval? = nil) throws -> HelperScriptResult {
        guard let python = PythonTranscriber.locatePython3(preferredRuntimeRoot: workingDirectory) else {
            throw PythonSpeakerIdentifierError.python3NotFound
        }

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

        return HelperScriptResult(
            exitCode: result.exitCode,
            standardOutput: result.standardOutput,
            standardError: result.standardError
        )
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

// MARK: - suggest-labels result models

/// Result of `python -m stt_vibevoice.speaker_id suggest-labels`. This is a
/// non-mutating, read-only operation: the only file written is the optional
/// `--json` output path. Mirrors the real schema produced by
/// `build_label_suggestions` (see `speaker_id.py`): `schemaVersion` 1,
/// `status` (`"ok"` / `"no_profiles"`), `config`, `profilesConsidered`,
/// `clusters`, `duplicateClusterGroups`, `mixedClusterWarnings`, `summary`.
/// All JSON keys are already camelCase so default `Codable` synthesis is
/// used throughout (no custom `CodingKeys`).
public struct SpeakerSuggestionResult: Codable, Equatable {
    public let schemaVersion: Int?
    public let status: String?
    public let session: String?
    public let generatedAt: String?
    public let config: SpeakerSuggestionConfig?
    public let profilesConsidered: SpeakerSuggestionProfilesConsidered?
    public let clusters: [SpeakerSuggestionCluster]?
    public let duplicateClusterGroups: [SpeakerSuggestionDuplicateGroup]?
    public let mixedClusterWarnings: [SpeakerSuggestionMixedWarning]?
    public let summary: SpeakerSuggestionSummary?
    /// Only present in the `no_profiles` state.
    public let recommendation: String?

    public init(schemaVersion: Int?,
                status: String?,
                session: String?,
                generatedAt: String?,
                config: SpeakerSuggestionConfig?,
                profilesConsidered: SpeakerSuggestionProfilesConsidered?,
                clusters: [SpeakerSuggestionCluster]?,
                duplicateClusterGroups: [SpeakerSuggestionDuplicateGroup]?,
                mixedClusterWarnings: [SpeakerSuggestionMixedWarning]?,
                summary: SpeakerSuggestionSummary?,
                recommendation: String?) {
        self.schemaVersion = schemaVersion
        self.status = status
        self.session = session
        self.generatedAt = generatedAt
        self.config = config
        self.profilesConsidered = profilesConsidered
        self.clusters = clusters
        self.duplicateClusterGroups = duplicateClusterGroups
        self.mixedClusterWarnings = mixedClusterWarnings
        self.summary = summary
        self.recommendation = recommendation
    }
}

public struct SpeakerSuggestionConfig: Codable, Equatable {
    public let threshold: Double?
    public let margin: Double?
    public let provider: String?
    public let model: String?

    public init(threshold: Double?, margin: Double?, provider: String?, model: String?) {
        self.threshold = threshold
        self.margin = margin
        self.provider = provider
        self.model = model
    }
}

public struct SpeakerSuggestionProfilesConsidered: Codable, Equatable {
    public let count: Int?
    public let profileIds: [String]?

    public init(count: Int?, profileIds: [String]?) {
        self.count = count
        self.profileIds = profileIds
    }
}

public struct SpeakerSuggestionCluster: Codable, Equatable {
    public let speakerId: String?
    public let source: String?
    public let durationSeconds: Double?
    public let segmentCount: Int?
    public let selectedRanges: [[Double]]?
    public let speechSeconds: Double?
    /// Reuses `SpeakerMatchBestMatch` (the exact `bestMatch` shape emitted by
    /// `match_candidate`) rather than duplicating a second, divergent model.
    public let bestMatch: SpeakerMatchBestMatch?
    public let recommendation: String?
    public let recommendationDetail: String?

    public init(speakerId: String?,
                source: String?,
                durationSeconds: Double?,
                segmentCount: Int?,
                selectedRanges: [[Double]]?,
                speechSeconds: Double?,
                bestMatch: SpeakerMatchBestMatch?,
                recommendation: String?,
                recommendationDetail: String?) {
        self.speakerId = speakerId
        self.source = source
        self.durationSeconds = durationSeconds
        self.segmentCount = segmentCount
        self.selectedRanges = selectedRanges
        self.speechSeconds = speechSeconds
        self.bestMatch = bestMatch
        self.recommendation = recommendation
        self.recommendationDetail = recommendationDetail
    }
}

public struct SpeakerSuggestionDuplicateGroup: Codable, Equatable {
    public let profileId: String?
    public let nameHint: String?
    public let displayName: String?
    public let clusters: [SpeakerSuggestionDuplicateMember]?
    public let recommendation: String?
    public let recommendationDetail: String?

    public init(profileId: String?,
                nameHint: String?,
                displayName: String?,
                clusters: [SpeakerSuggestionDuplicateMember]?,
                recommendation: String?,
                recommendationDetail: String?) {
        self.profileId = profileId
        self.nameHint = nameHint
        self.displayName = displayName
        self.clusters = clusters
        self.recommendation = recommendation
        self.recommendationDetail = recommendationDetail
    }
}

public struct SpeakerSuggestionDuplicateMember: Codable, Equatable {
    public let speakerId: String?
    public let confidence: Double?
    public let selectedRanges: [[Double]]?

    public init(speakerId: String?, confidence: Double?, selectedRanges: [[Double]]?) {
        self.speakerId = speakerId
        self.confidence = confidence
        self.selectedRanges = selectedRanges
    }
}

public struct SpeakerSuggestionMixedWarning: Codable, Equatable {
    public let speakerId: String?
    public let windows: [SpeakerSuggestionWindowEvidence]?
    public let conflictingProfileIds: [String]?
    public let conflictingDisplayNames: [String]?
    public let recommendation: String?
    public let recommendationDetail: String?

    public init(speakerId: String?,
                windows: [SpeakerSuggestionWindowEvidence]?,
                conflictingProfileIds: [String]?,
                conflictingDisplayNames: [String]?,
                recommendation: String?,
                recommendationDetail: String?) {
        self.speakerId = speakerId
        self.windows = windows
        self.conflictingProfileIds = conflictingProfileIds
        self.conflictingDisplayNames = conflictingDisplayNames
        self.recommendation = recommendation
        self.recommendationDetail = recommendationDetail
    }
}

public struct SpeakerSuggestionWindowEvidence: Codable, Equatable {
    public let label: String?
    public let range: [Double]?
    public let bestMatch: SpeakerMatchBestMatch?
    public let matchedProfileId: String?

    public init(label: String?, range: [Double]?, bestMatch: SpeakerMatchBestMatch?, matchedProfileId: String?) {
        self.label = label
        self.range = range
        self.bestMatch = bestMatch
        self.matchedProfileId = matchedProfileId
    }
}

public struct SpeakerSuggestionSummary: Codable, Equatable {
    public let clusterCount: Int?
    public let matchedCount: Int?
    public let duplicateGroupCount: Int?
    public let mixedClusterCount: Int?
    public let unmatchedCount: Int?

    public init(clusterCount: Int?, matchedCount: Int?, duplicateGroupCount: Int?, mixedClusterCount: Int?, unmatchedCount: Int?) {
        self.clusterCount = clusterCount
        self.matchedCount = matchedCount
        self.duplicateGroupCount = duplicateGroupCount
        self.mixedClusterCount = mixedClusterCount
        self.unmatchedCount = unmatchedCount
    }
}

// MARK: - helper script result

/// Raw result of invoking the ``name_one_speaker.py`` helper script. The
/// helper prints structured JSON to stdout; callers parse it as needed.
public struct HelperScriptResult: Equatable {
    public let exitCode: Int32
    public let standardOutput: String
    public let standardError: String

    public var succeeded: Bool { exitCode == 0 }

    public init(exitCode: Int32, standardOutput: String, standardError: String) {
        self.exitCode = exitCode
        self.standardOutput = standardOutput
        self.standardError = standardError
    }
}
