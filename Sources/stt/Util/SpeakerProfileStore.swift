import Foundation

/// Manages speaker profile storage on disk.
///
/// Layout under `directory` (typically `Paths.speakerProfilesDirectory(...)`):
///
/// ```text
/// <directory>/
///   profiles/
///     index.json
///     <profile-id>.json
///   samples/
///     <profile-id>/
///       20260706-120000.wav
/// ```
///
/// This store owns profile/index CRUD and path helpers only. Copying
/// enrollment sample audio into `samples/<id>/...` is Phase 5's
/// responsibility (the enrollment command); this store only exposes the
/// directory a caller should copy samples into.
public struct SpeakerProfileStore {
    public let directory: URL
    private let fileManager: FileManager

    public init(directory: URL, fileManager: FileManager = .default) {
        self.directory = directory
        self.fileManager = fileManager
    }

    // MARK: - Paths

    public var profilesDirectory: URL {
        directory.appendingPathComponent("profiles", isDirectory: true)
    }

    public var samplesDirectory: URL {
        directory.appendingPathComponent("samples", isDirectory: true)
    }

    public var indexFileURL: URL {
        profilesDirectory.appendingPathComponent("index.json")
    }

    public func profileFileURL(id: UUID) -> URL {
        profilesDirectory.appendingPathComponent("\(id.uuidString).json")
    }

    /// Directory a caller should copy enrollment sample audio into for the
    /// given profile id. Relative sample paths recorded on the profile
    /// should be relative to `directory` (e.g.
    /// `samples/<id>/20260706-120000.wav`), not to this directory directly.
    public func sampleDirectory(forProfileID id: UUID) -> URL {
        samplesDirectory.appendingPathComponent(id.uuidString, isDirectory: true)
    }

    /// Resolves a profile's relative sample path (as stored in
    /// `samplePaths`) to an absolute URL under `directory`.
    public func absoluteSamplePath(_ relativePath: String) -> URL {
        directory.appendingPathComponent(relativePath)
    }

    // MARK: - Coding

    private func encoder() -> JSONEncoder {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        encoder.dateEncodingStrategy = .iso8601
        return encoder
    }

    private func decoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return decoder
    }

    // MARK: - Index

    /// Loads `profiles/index.json`. Returns an empty index if the file
    /// doesn't exist yet (e.g. no profiles have been saved).
    public func loadIndex() throws -> SpeakerProfileIndex {
        guard fileManager.fileExists(atPath: indexFileURL.path) else {
            return SpeakerProfileIndex()
        }
        let data = try Data(contentsOf: indexFileURL)
        return try decoder().decode(SpeakerProfileIndex.self, from: data)
    }

    private func writeIndex(_ index: SpeakerProfileIndex) throws {
        try Paths.ensureDirectoryExists(profilesDirectory, fileManager: fileManager)
        let data = try encoder().encode(index)
        try data.write(to: indexFileURL, options: .atomic)
    }

    // MARK: - CRUD

    /// Saves a profile: writes `profiles/<id>.json` and updates (inserting
    /// or replacing) its entry in `profiles/index.json`.
    @discardableResult
    public func save(_ profile: SpeakerProfile) throws -> SpeakerProfile {
        try Paths.ensureDirectoryExists(profilesDirectory, fileManager: fileManager)

        let data = try encoder().encode(profile)
        try data.write(to: profileFileURL(id: profile.id), options: .atomic)

        var index = try loadIndex()
        let summary = SpeakerProfileSummary(profile: profile)
        if let existingIndex = index.profiles.firstIndex(where: { $0.id == profile.id }) {
            index.profiles[existingIndex] = summary
        } else {
            index.profiles.append(summary)
        }
        try writeIndex(index)

        return profile
    }

    /// Loads a full profile (including its embedding) by id.
    public func load(id: UUID) throws -> SpeakerProfile {
        let url = profileFileURL(id: id)
        guard fileManager.fileExists(atPath: url.path) else {
            throw SpeakerProfileError.notFound(id: id)
        }
        let data = try Data(contentsOf: url)
        return try decoder().decode(SpeakerProfile.self, from: data)
    }

    /// Lists profile summaries from the index, sorted by display name (then
    /// id) for stable, predictable output.
    public func listSummaries() throws -> [SpeakerProfileSummary] {
        try loadIndex().profiles.sorted { lhs, rhs in
            if lhs.displayName != rhs.displayName {
                return lhs.displayName.localizedStandardCompare(rhs.displayName) == .orderedAscending
            }
            return lhs.id.uuidString < rhs.id.uuidString
        }
    }

    /// Loads every full profile, sorted the same way as `listSummaries()`.
    public func listProfiles() throws -> [SpeakerProfile] {
        try listSummaries().map { try load(id: $0.id) }
    }

    /// Renames a profile's mutable `displayName`, bumping `updatedAt`, and
    /// persists the change to both the profile JSON and the index.
    @discardableResult
    public func rename(id: UUID, to newDisplayName: String) throws -> SpeakerProfile {
        var profile = try load(id: id)
        profile.displayName = newDisplayName
        profile.updatedAt = Date()
        return try save(profile)
    }

    /// Deletes a profile: removes `profiles/<id>.json`, removes its index
    /// entry, and removes any copied samples under `samples/<id>/`.
    public func delete(id: UUID) throws {
        guard fileManager.fileExists(atPath: profileFileURL(id: id).path) else {
            throw SpeakerProfileError.notFound(id: id)
        }

        try? fileManager.removeItem(at: profileFileURL(id: id))

        let sampleDir = sampleDirectory(forProfileID: id)
        if fileManager.fileExists(atPath: sampleDir.path) {
            try? fileManager.removeItem(at: sampleDir)
        }

        var index = try loadIndex()
        index.profiles.removeAll { $0.id == id }
        try writeIndex(index)
    }

    /// Looks up a profile by case-insensitive `displayName` match. Throws
    /// `SpeakerProfileError.nameNotFound` when no profile matches, and
    /// `SpeakerProfileError.ambiguousName` when more than one profile matches
    /// that display name case-insensitively (duplicate names are allowed for
    /// storage, but name-based lookup must not silently pick one).
    public func findByName(_ displayName: String) throws -> SpeakerProfile {
        let normalizedDisplayName = normalizeDisplayName(displayName)
        let matches = try loadIndex().profiles.filter { normalizeDisplayName($0.displayName) == normalizedDisplayName }
        switch matches.count {
        case 0:
            throw SpeakerProfileError.nameNotFound(displayName)
        case 1:
            return try load(id: matches[0].id)
        default:
            throw SpeakerProfileError.ambiguousName(displayName, matchingIDs: matches.map(\.id))
        }
    }

    private func normalizeDisplayName(_ displayName: String) -> String {
        displayName.trimmingCharacters(in: .whitespacesAndNewlines).localizedLowercase
    }
}
