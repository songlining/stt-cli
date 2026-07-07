import Foundation

/// Which capture mode produced a recording.
public enum RecordingMode: String, Codable, CaseIterable {
    case mic
    case system
    case meeting
}

/// Metadata describing a single mic/system/meeting recording + transcription run.
/// Serialized to `runs/<runID>/metadata.json`.
public struct SessionState: Codable, Equatable {
    public var runID: String
    public var name: String
    public var mode: RecordingMode
    public var startedAt: Date
    public var finishedAt: Date?
    public var durationSeconds: Double?
    public var outputPaths: [String]
    public var separateTracks: Bool
    /// Audio file path selected for transcription. On early pipeline failures,
    /// this is the intended path and may not exist yet. Meeting runs may use
    /// multiple source tracks; see `transcribedAudioPaths` for the full list.
    public var transcribedAudioPath: String?
    public var transcribedAudioPaths: [String]?
    public var transcriptTextPath: String?
    public var transcriptJSONPath: String?
    public var backend: String?
    public var notes: String?

    public init(runID: String,
                name: String,
                mode: RecordingMode,
                startedAt: Date = Date(),
                finishedAt: Date? = nil,
                durationSeconds: Double? = nil,
                outputPaths: [String] = [],
                separateTracks: Bool = false,
                transcribedAudioPath: String? = nil,
                transcribedAudioPaths: [String]? = nil,
                transcriptTextPath: String? = nil,
                transcriptJSONPath: String? = nil,
                backend: String? = nil,
                notes: String? = nil) {
        self.runID = runID
        self.name = name
        self.mode = mode
        self.startedAt = startedAt
        self.finishedAt = finishedAt
        self.durationSeconds = durationSeconds
        self.outputPaths = outputPaths
        self.separateTracks = separateTracks
        self.transcribedAudioPath = transcribedAudioPath
        self.transcribedAudioPaths = transcribedAudioPaths
        self.transcriptTextPath = transcriptTextPath
        self.transcriptJSONPath = transcriptJSONPath
        self.backend = backend
        self.notes = notes
    }
}

public enum SessionStateStore {
    private static func encoder() -> JSONEncoder {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        encoder.dateEncodingStrategy = .iso8601
        return encoder
    }

    private static func decoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return decoder
    }

    /// Writes session metadata to `<runDirectory>/metadata.json`, creating
    /// the run directory if necessary.
    public static func write(_ state: SessionState, toRunDirectory runDirectory: URL, fileManager: FileManager = .default) throws -> URL {
        try Paths.ensureDirectoryExists(runDirectory, fileManager: fileManager)
        let fileURL = runDirectory.appendingPathComponent("metadata.json")
        let data = try encoder().encode(state)
        try data.write(to: fileURL, options: .atomic)
        return fileURL
    }

    /// Reads session metadata from `<runDirectory>/metadata.json`.
    public static func read(fromRunDirectory runDirectory: URL) throws -> SessionState {
        let fileURL = runDirectory.appendingPathComponent("metadata.json")
        let data = try Data(contentsOf: fileURL)
        return try decoder().decode(SessionState.self, from: data)
    }
}
