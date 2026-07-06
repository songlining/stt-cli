import Foundation

/// Resolves standard application-support directories used by `stt` for
/// recordings, transcripts, and per-run session metadata.
///
/// Layout:
///   ~/Library/Application Support/stt/recordings
///   ~/Library/Application Support/stt/transcripts
///   ~/Library/Application Support/stt/runs/<timestamp>/metadata.json
public enum Paths {

    /// Base "Application Support/stt" directory. Can be overridden via the
    /// `STT_HOME` environment variable (primarily for testing).
    public static func appSupportDirectory(fileManager: FileManager = .default,
                                            environment: [String: String] = ProcessInfo.processInfo.environment) -> URL {
        if let override = environment["STT_HOME"], !override.isEmpty {
            return URL(fileURLWithPath: override, isDirectory: true)
        }
        let base = fileManager.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? fileManager.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support")
        return base.appendingPathComponent("stt", isDirectory: true)
    }

    public static func recordingsDirectory(fileManager: FileManager = .default,
                                            environment: [String: String] = ProcessInfo.processInfo.environment) -> URL {
        appSupportDirectory(fileManager: fileManager, environment: environment).appendingPathComponent("recordings", isDirectory: true)
    }

    public static func transcriptsDirectory(fileManager: FileManager = .default,
                                             environment: [String: String] = ProcessInfo.processInfo.environment) -> URL {
        appSupportDirectory(fileManager: fileManager, environment: environment).appendingPathComponent("transcripts", isDirectory: true)
    }

    public static func runsDirectory(fileManager: FileManager = .default,
                                      environment: [String: String] = ProcessInfo.processInfo.environment) -> URL {
        appSupportDirectory(fileManager: fileManager, environment: environment).appendingPathComponent("runs", isDirectory: true)
    }

    /// Directory used to store speaker enrollment profiles and voice
    /// samples. Resolution order:
    /// 1. `config.speakerProfilesDir`, if set (explicit user override).
    /// 2. Default app-support path, `<appSupportDirectory>/speakers`, which
    ///    is itself scoped by `STT_HOME` when that environment variable is
    ///    set.
    public static func speakerProfilesDirectory(config: STTConfig,
                                                 fileManager: FileManager = .default,
                                                 environment: [String: String] = ProcessInfo.processInfo.environment) -> URL {
        if let override = config.speakerProfilesDir, !override.isEmpty {
            return URL(fileURLWithPath: override, isDirectory: true)
        }
        return appSupportDirectory(fileManager: fileManager, environment: environment).appendingPathComponent("speakers", isDirectory: true)
    }

    /// Directory for a specific run, identified by a timestamp-based token.
    public static func runDirectory(runID: String,
                                     fileManager: FileManager = .default,
                                     environment: [String: String] = ProcessInfo.processInfo.environment) -> URL {
        runsDirectory(fileManager: fileManager, environment: environment).appendingPathComponent(runID, isDirectory: true)
    }

    /// Creates the given directory (and intermediate directories) if needed.
    @discardableResult
    public static func ensureDirectoryExists(_ url: URL, fileManager: FileManager = .default) throws -> URL {
        var isDirectory: ObjCBool = false
        if fileManager.fileExists(atPath: url.path, isDirectory: &isDirectory) {
            if isDirectory.boolValue {
                return url
            }
            throw PathsError.notADirectory(url.path)
        }
        try fileManager.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    /// Resolves and validates that a user-supplied path exists and is a file.
    @discardableResult
    public static func requireExistingFile(_ path: String, fileManager: FileManager = .default) throws -> URL {
        let url = URL(fileURLWithPath: path)
        var isDirectory: ObjCBool = false
        guard fileManager.fileExists(atPath: url.path, isDirectory: &isDirectory) else {
            throw PathsError.fileNotFound(path)
        }
        guard !isDirectory.boolValue else {
            throw PathsError.notAFile(path)
        }
        return url
    }

    /// Resolves and validates that a user-supplied audio file exists and has
    /// at least one byte of content. This stays format-agnostic so transcribe
    /// can still accept non-WAV inputs that the Python backend normalizes.
    @discardableResult
    public static func requireNonEmptyFile(_ path: String, fileManager: FileManager = .default) throws -> URL {
        let url = try requireExistingFile(path, fileManager: fileManager)
        let attributes = try fileManager.attributesOfItem(atPath: url.path)
        let size = (attributes[.size] as? NSNumber)?.uint64Value ?? 0
        guard size > 0 else {
            throw PathsError.emptyFile(path)
        }
        return url
    }

    /// A sortable, filesystem-safe timestamp token e.g. `20260704-143210`.
    public static func timestampToken(date: Date = Date()) -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyyMMdd-HHmmss"
        formatter.timeZone = TimeZone.current
        formatter.locale = Locale(identifier: "en_US_POSIX")
        return formatter.string(from: date)
    }
}

public enum PathsError: Error, LocalizedError {
    case notADirectory(String)
    case fileNotFound(String)
    case notAFile(String)
    case emptyFile(String)

    public var errorDescription: String? {
        switch self {
        case .notADirectory(let path):
            return "Expected a directory at \(path), but a file exists there."
        case .fileNotFound(let path):
            return "Audio file not found: \(path)"
        case .notAFile(let path):
            return "Expected a file at \(path), but a directory exists there."
        case .emptyFile(let path):
            return "Audio file is empty (0 bytes): \(path)"
        }
    }
}
