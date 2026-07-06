import Foundation

/// A single enrolled speaker profile.
///
/// Stored as one JSON file per profile under
/// `<speakerProfilesDir>/profiles/<id>.json`. See
/// `SPEAKER_IDENTIFICATION_PLAN.md` for the full data model contract.
public struct SpeakerProfile: Codable, Equatable, Sendable {
    /// Immutable identity for this profile. Never changes after creation;
    /// callers must not attempt to rename/recreate the id.
    public let id: UUID
    /// Human-facing name. Mutable via rename.
    public var displayName: String
    public var createdAt: Date
    public var updatedAt: Date
    public var embeddingProvider: String
    public var embeddingModel: String
    public var embedding: [Double]
    /// Sample audio paths, relative to `speakerProfilesDir` (e.g.
    /// `samples/<id>/20260706-120000.wav`). Never absolute source paths.
    public var samplePaths: [String]
    public var sampleDurationSeconds: Double
    public var notes: String?

    public init(id: UUID = UUID(),
                displayName: String,
                createdAt: Date = Date(),
                updatedAt: Date = Date(),
                embeddingProvider: String,
                embeddingModel: String,
                embedding: [Double],
                samplePaths: [String] = [],
                sampleDurationSeconds: Double = 0,
                notes: String? = nil) {
        self.id = id
        self.displayName = displayName
        self.createdAt = createdAt
        self.updatedAt = updatedAt
        self.embeddingProvider = embeddingProvider
        self.embeddingModel = embeddingModel
        self.embedding = embedding
        self.samplePaths = samplePaths
        self.sampleDurationSeconds = sampleDurationSeconds
        self.notes = notes
    }

    private enum CodingKeys: String, CodingKey {
        case id
        case displayName
        case createdAt
        case updatedAt
        case embeddingProvider
        case embeddingModel
        case embedding
        case samplePaths
        case sampleDurationSeconds
        case notes
    }
}

/// Lightweight per-profile summary stored in `profiles/index.json`, so
/// listing profiles does not require reading every profile JSON file (and,
/// notably, does not require loading every embedding vector).
public struct SpeakerProfileSummary: Codable, Equatable, Sendable {
    public var id: UUID
    public var displayName: String
    public var embeddingProvider: String
    public var embeddingModel: String
    public var updatedAt: Date
    public var sampleCount: Int

    public init(id: UUID,
                displayName: String,
                embeddingProvider: String,
                embeddingModel: String,
                updatedAt: Date,
                sampleCount: Int) {
        self.id = id
        self.displayName = displayName
        self.embeddingProvider = embeddingProvider
        self.embeddingModel = embeddingModel
        self.updatedAt = updatedAt
        self.sampleCount = sampleCount
    }

    public init(profile: SpeakerProfile) {
        self.id = profile.id
        self.displayName = profile.displayName
        self.embeddingProvider = profile.embeddingProvider
        self.embeddingModel = profile.embeddingModel
        self.updatedAt = profile.updatedAt
        self.sampleCount = profile.samplePaths.count
    }
}

/// On-disk schema for `<speakerProfilesDir>/profiles/index.json`.
public struct SpeakerProfileIndex: Codable, Equatable, Sendable {
    public var profiles: [SpeakerProfileSummary]

    public init(profiles: [SpeakerProfileSummary] = []) {
        self.profiles = profiles
    }
}

public enum SpeakerProfileError: Error, LocalizedError, Equatable {
    case notFound(id: UUID)
    case nameNotFound(String)
    case ambiguousName(String, matchingIDs: [UUID])

    public var errorDescription: String? {
        switch self {
        case .notFound(let id):
            return "No speaker profile found with id \(id.uuidString)."
        case .nameNotFound(let name):
            return "No speaker profile found with display name \"\(name)\"."
        case .ambiguousName(let name, let matchingIDs):
            let ids = matchingIDs.map(\.uuidString).joined(separator: ", ")
            return "Display name \"\(name)\" matches multiple speaker profiles (\(ids)); use the profile id instead."
        }
    }
}
