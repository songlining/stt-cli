import Foundation

/// Backward-compatible provenance metadata for an enrolled speaker sample.
///
/// Records *how* a profile's enrollment sample was produced so the profile
/// can be traced back to its source session, transcript, diarized speaker,
/// and the confirmed time ranges it was enrolled from. Every field is
/// optional so that:
///
/// - Pre-existing profile JSON (written before provenance existed) decodes
///   with a ``nil`` ``provenance`` block -- no migration required.
/// - Range-limited enrollment (``enroll-ranges``) populates the fields it
///   knows (source session/transcript/track, diarized speaker id, selected
///   ranges, confirmation mode, timestamp); whole-audio enrollment simply
///   leaves ``provenance`` unset.
///
/// Speaker profiles are local biometric-like data; provenance is stored
/// locally alongside the profile and is never uploaded.
public struct SpeakerProfileProvenance: Codable, Equatable, Sendable {
    /// Path of the recording session directory the sample was enrolled from
    /// (e.g. the session containing ``mic.wav``/``system.wav`` and the
    /// transcript). ``nil`` for whole-audio enrollment with no session.
    public var sourceSession: String?
    /// Path of the diarized transcript JSON the sample was sourced from.
    public var sourceTranscript: String?
    /// Audio source track the sample was taken from (``"mic"`` or
    /// ``"system"``).
    public var sourceTrack: String?
    /// Diarized speaker id (cluster label) the enrolled ranges belonged to.
    public var diarizedSpeakerId: String?
    /// Confirmed ``(start, end)`` time ranges (seconds) the sample was built
    /// from, as ``[[start, end], ...]``.
    public var selectedRanges: [[Double]]?
    /// Relative path (under ``speakerProfilesDir``) of the enrolled sample
    /// WAV, e.g. ``samples/<id>/20260706-120000.wav``. Mirrors the entry
    /// appended to ``SpeakerProfile.samplePaths`` for this enrollment.
    public var samplePath: String?
    /// How the enrollment was confirmed/sourced. ``"range-limited"`` for
    /// ``enroll-ranges`` (confirmed ranges); whole-audio enrollment leaves
    /// this unset.
    public var confirmationMode: String?
    /// When the provenance was recorded (profile/sample creation time).
    public var timestamp: Date?

    public init(sourceSession: String? = nil,
                sourceTranscript: String? = nil,
                sourceTrack: String? = nil,
                diarizedSpeakerId: String? = nil,
                selectedRanges: [[Double]]? = nil,
                samplePath: String? = nil,
                confirmationMode: String? = nil,
                timestamp: Date? = nil) {
        self.sourceSession = sourceSession
        self.sourceTranscript = sourceTranscript
        self.sourceTrack = sourceTrack
        self.diarizedSpeakerId = diarizedSpeakerId
        self.selectedRanges = selectedRanges
        self.samplePath = samplePath
        self.confirmationMode = confirmationMode
        self.timestamp = timestamp
    }

    private enum CodingKeys: String, CodingKey {
        case sourceSession
        case sourceTranscript
        case sourceTrack
        case diarizedSpeakerId
        case selectedRanges
        case samplePath
        case confirmationMode
        case timestamp
    }
}

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
    /// Backward-compatible provenance metadata for the enrollment sample.
    /// ``nil`` for profiles enrolled before provenance existed and for
    /// whole-audio enrollment that has no source session/ranges to record.
    /// Optional so existing profile JSON loads without migration.
    public var provenance: SpeakerProfileProvenance?

    public init(id: UUID = UUID(),
                displayName: String,
                createdAt: Date = Date(),
                updatedAt: Date = Date(),
                embeddingProvider: String,
                embeddingModel: String,
                embedding: [Double],
                samplePaths: [String] = [],
                sampleDurationSeconds: Double = 0,
                notes: String? = nil,
                provenance: SpeakerProfileProvenance? = nil) {
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
        self.provenance = provenance
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
        case provenance
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
