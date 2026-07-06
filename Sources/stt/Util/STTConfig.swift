import Foundation

/// A structured command invocation, deliberately not a raw shell string, so
/// `stt` never has to interpret shell metacharacters when running a
/// user-configured post-pipeline hook.
public struct PostPipelineCommand: Codable, Equatable, Sendable {
    public var executable: String
    public var arguments: [String]

    public init(executable: String, arguments: [String] = []) {
        self.executable = executable
        self.arguments = arguments
    }

    private enum CodingKeys: String, CodingKey {
        case executable
        case arguments
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        executable = try container.decode(String.self, forKey: .executable)
        arguments = try container.decodeIfPresent([String].self, forKey: .arguments) ?? []
    }
}

/// Speaker identification tuning knobs. Identification stays disabled by
/// default; enabling it requires an explicit opt-in in the user's config.
public struct SpeakerIdentificationConfig: Codable, Equatable, Sendable {
    public var enabled: Bool
    public var provider: String
    public var matchThreshold: Double
    public var matchMargin: Double
    public var minimumSpeechSeconds: Double

    public init(enabled: Bool = false,
                provider: String = "speechbrain",
                matchThreshold: Double = 0.78,
                matchMargin: Double = 0.05,
                minimumSpeechSeconds: Double = 8.0) {
        self.enabled = enabled
        self.provider = provider
        self.matchThreshold = matchThreshold
        self.matchMargin = matchMargin
        self.minimumSpeechSeconds = minimumSpeechSeconds
    }

    private enum CodingKeys: String, CodingKey {
        case enabled
        case provider
        case matchThreshold
        case matchMargin
        case minimumSpeechSeconds
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let defaults = SpeakerIdentificationConfig()
        enabled = try container.decodeIfPresent(Bool.self, forKey: .enabled) ?? defaults.enabled
        provider = try container.decodeIfPresent(String.self, forKey: .provider) ?? defaults.provider
        matchThreshold = try container.decodeIfPresent(Double.self, forKey: .matchThreshold) ?? defaults.matchThreshold
        matchMargin = try container.decodeIfPresent(Double.self, forKey: .matchMargin) ?? defaults.matchMargin
        minimumSpeechSeconds = try container.decodeIfPresent(Double.self, forKey: .minimumSpeechSeconds) ?? defaults.minimumSpeechSeconds
    }
}

/// Generic (non-Obsidian-specific) artifact export configuration. Core `stt`
/// only knows how to copy artifacts into a configured directory and,
/// optionally, invoke a structured post-pipeline hook command; any
/// Obsidian-specific filing behavior belongs in that hook script, not here.
public struct ArtifactExportConfig: Codable, Equatable, Sendable {
    public var enabled: Bool
    public var targetDir: String?
    public var includeAudio: Bool
    public var overwrite: Bool
    public var postPipelineCommand: PostPipelineCommand?
    public var hookTimeoutSeconds: Double

    public init(enabled: Bool = false,
                targetDir: String? = nil,
                includeAudio: Bool = false,
                overwrite: Bool = false,
                postPipelineCommand: PostPipelineCommand? = nil,
                hookTimeoutSeconds: Double = 30) {
        self.enabled = enabled
        self.targetDir = targetDir
        self.includeAudio = includeAudio
        self.overwrite = overwrite
        self.postPipelineCommand = postPipelineCommand
        self.hookTimeoutSeconds = hookTimeoutSeconds
    }

    private enum CodingKeys: String, CodingKey {
        case enabled
        case targetDir
        case includeAudio
        case overwrite
        case postPipelineCommand
        case hookTimeoutSeconds
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let defaults = ArtifactExportConfig()
        enabled = try container.decodeIfPresent(Bool.self, forKey: .enabled) ?? defaults.enabled
        targetDir = try container.decodeIfPresent(String.self, forKey: .targetDir)
        includeAudio = try container.decodeIfPresent(Bool.self, forKey: .includeAudio) ?? defaults.includeAudio
        overwrite = try container.decodeIfPresent(Bool.self, forKey: .overwrite) ?? defaults.overwrite
        postPipelineCommand = try container.decodeIfPresent(PostPipelineCommand.self, forKey: .postPipelineCommand)
        hookTimeoutSeconds = try container.decodeIfPresent(Double.self, forKey: .hookTimeoutSeconds) ?? defaults.hookTimeoutSeconds
    }
}

/// Top-level `stt` configuration, loaded from a JSON file. All fields are
/// optional in the on-disk schema; missing sections fall back to their
/// documented defaults.
public struct STTConfig: Codable, Equatable, Sendable {
    public var speakerProfilesDir: String?
    public var speakerIdentification: SpeakerIdentificationConfig
    public var artifactExport: ArtifactExportConfig

    public init(speakerProfilesDir: String? = nil,
                speakerIdentification: SpeakerIdentificationConfig = SpeakerIdentificationConfig(),
                artifactExport: ArtifactExportConfig = ArtifactExportConfig()) {
        self.speakerProfilesDir = speakerProfilesDir
        self.speakerIdentification = speakerIdentification
        self.artifactExport = artifactExport
    }

    private enum CodingKeys: String, CodingKey {
        case speakerProfilesDir
        case speakerIdentification
        case artifactExport
    }

    /// Every top-level section is optional in the on-disk JSON schema, so a
    /// config file that only sets e.g. `speakerProfilesDir` must not fail to
    /// decode just because `speakerIdentification`/`artifactExport` are
    /// absent.
    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        speakerProfilesDir = try container.decodeIfPresent(String.self, forKey: .speakerProfilesDir)
        speakerIdentification = try container.decodeIfPresent(SpeakerIdentificationConfig.self, forKey: .speakerIdentification)
            ?? SpeakerIdentificationConfig()
        artifactExport = try container.decodeIfPresent(ArtifactExportConfig.self, forKey: .artifactExport)
            ?? ArtifactExportConfig()
    }

    /// Config with all defaults, used when no config file is found. Not an
    /// error condition: an absent config file is expected for most users.
    public static let `default` = STTConfig()
}

public enum STTConfigError: Error, LocalizedError, Equatable {
    case malformedJSON(path: String, underlying: String)
    case invalidValue(path: String, message: String)

    public var errorDescription: String? {
        switch self {
        case .malformedJSON(let path, let underlying):
            return "Config file at \(path) is not valid JSON: \(underlying)"
        case .invalidValue(_, let message):
            return message
        }
    }

    public static func == (lhs: STTConfigError, rhs: STTConfigError) -> Bool {
        switch (lhs, rhs) {
        case (.malformedJSON(let lp, let lu), .malformedJSON(let rp, let ru)):
            return lp == rp && lu == ru
        case (.invalidValue(let lp, let lm), .invalidValue(let rp, let rm)):
            return lp == rp && lm == rm
        default:
            return false
        }
    }
}

public enum STTConfigLoader {

    /// Resolves the config file path to use, without reading or validating
    /// its contents.
    ///
    /// Discovery order:
    /// 1. `explicitPath`, if provided (e.g. from a `--config` CLI flag).
    /// 2. `STT_CONFIG` environment variable, if set and non-empty.
    /// 3. `~/.config/stt/config.json` (built-in default location).
    ///
    /// The built-in default location is non-fatal if missing: callers should
    /// treat a missing file at that path as "use defaults", not an error.
    public static func resolvePath(explicitPath: String?,
                                    environment: [String: String],
                                    fileManager: FileManager) -> URL {
        if let explicitPath, !explicitPath.isEmpty {
            return URL(fileURLWithPath: explicitPath)
        }
        if let envPath = environment["STT_CONFIG"], !envPath.isEmpty {
            return URL(fileURLWithPath: envPath)
        }
        let home = homeDirectory(environment: environment, fileManager: fileManager)
        return home.appendingPathComponent(".config/stt/config.json")
    }

    /// Loads and validates the `stt` configuration.
    ///
    /// - If `explicitPath` is supplied, a missing or malformed file at that
    ///   path is an error (the caller asked for it explicitly).
    /// - If resolution falls through to `STT_CONFIG` or the built-in default
    ///   path and no file exists there, `STTConfig.default` is returned with
    ///   no error.
    /// - A malformed JSON file, at any resolved path once it exists, is
    ///   always an error.
    /// - Values are validated per the documented schema rules; violations
    ///   raise `STTConfigError.invalidValue`.
    public static func load(explicitPath: String? = nil,
                             environment: [String: String] = ProcessInfo.processInfo.environment,
                             fileManager: FileManager = .default) throws -> STTConfig {
        let resolvedPath = resolvePath(explicitPath: explicitPath, environment: environment, fileManager: fileManager)
        let explicitlyRequested = (explicitPath != nil && !(explicitPath ?? "").isEmpty)
            || !(environment["STT_CONFIG"] ?? "").isEmpty

        guard fileManager.fileExists(atPath: resolvedPath.path) else {
            if explicitlyRequested {
                throw STTConfigError.invalidValue(path: resolvedPath.path,
                                                   message: "Config file not found: \(resolvedPath.path)")
            }
            return .default
        }

        let data: Data
        do {
            data = try Data(contentsOf: resolvedPath)
        } catch {
            throw STTConfigError.malformedJSON(path: resolvedPath.path, underlying: error.localizedDescription)
        }

        let config: STTConfig
        do {
            let decoder = JSONDecoder()
            config = try decoder.decode(STTConfig.self, from: data)
        } catch {
            throw STTConfigError.malformedJSON(path: resolvedPath.path, underlying: String(describing: error))
        }

        try validate(config)
        return config
    }

    /// Applies the schema validation rules documented in
    /// `SPEAKER_IDENTIFICATION_PLAN.md`.
    public static func validate(_ config: STTConfig) throws {
        if let profilesDir = config.speakerProfilesDir, profilesDir.isEmpty {
            throw STTConfigError.invalidValue(path: "speakerProfilesDir",
                                               message: "speakerProfilesDir must not be an empty string.")
        }

        let identification = config.speakerIdentification
        guard (0...1).contains(identification.matchThreshold) else {
            throw STTConfigError.invalidValue(path: "speakerIdentification.matchThreshold",
                                               message: "speakerIdentification.matchThreshold must be between 0 and 1, got \(identification.matchThreshold).")
        }
        guard identification.matchMargin >= 0 else {
            throw STTConfigError.invalidValue(path: "speakerIdentification.matchMargin",
                                               message: "speakerIdentification.matchMargin must be >= 0, got \(identification.matchMargin).")
        }
        guard identification.minimumSpeechSeconds > 0 else {
            throw STTConfigError.invalidValue(path: "speakerIdentification.minimumSpeechSeconds",
                                               message: "speakerIdentification.minimumSpeechSeconds must be > 0, got \(identification.minimumSpeechSeconds).")
        }

        let artifactExport = config.artifactExport
        if let targetDir = artifactExport.targetDir, targetDir.isEmpty {
            throw STTConfigError.invalidValue(path: "artifactExport.targetDir",
                                               message: "artifactExport.targetDir must not be an empty string.")
        }
        if artifactExport.enabled {
            guard let targetDir = artifactExport.targetDir, !targetDir.isEmpty else {
                throw STTConfigError.invalidValue(path: "artifactExport.targetDir",
                                                   message: "artifactExport.targetDir is required when artifactExport.enabled is true.")
            }
        }
        if let hook = artifactExport.postPipelineCommand, hook.executable.isEmpty {
            throw STTConfigError.invalidValue(path: "artifactExport.postPipelineCommand.executable",
                                               message: "artifactExport.postPipelineCommand.executable must not be an empty string.")
        }
        guard artifactExport.hookTimeoutSeconds > 0 else {
            throw STTConfigError.invalidValue(path: "artifactExport.hookTimeoutSeconds",
                                               message: "artifactExport.hookTimeoutSeconds must be > 0, got \(artifactExport.hookTimeoutSeconds).")
        }
    }

    /// Resolves the user's home directory, honoring a `HOME` environment
    /// override so tests can point at a temp directory without touching the
    /// real machine.
    static func homeDirectory(environment: [String: String], fileManager: FileManager) -> URL {
        if let home = environment["HOME"], !home.isEmpty {
            return URL(fileURLWithPath: home, isDirectory: true)
        }
        return fileManager.homeDirectoryForCurrentUser
    }
}
