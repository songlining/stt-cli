import Foundation
import Testing
@testable import sttCore

@Suite("SpeakerProfileStore")
struct SpeakerProfileStoreTests {

    private func tempDirectory() -> URL {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    private func makeProfile(displayName: String,
                              samplePaths: [String] = [],
                              embedding: [Double] = [0.1, 0.2, 0.3]) -> SpeakerProfile {
        SpeakerProfile(displayName: displayName,
                        embeddingProvider: "speechbrain",
                        embeddingModel: "speechbrain/spkrec-ecapa-voxceleb",
                        embedding: embedding,
                        samplePaths: samplePaths,
                        sampleDurationSeconds: 15.0)
    }

    // MARK: - Save/load round trip

    @Test func saveAndLoadRoundTrip() throws {
        let dir = tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = SpeakerProfileStore(directory: dir)

        let profile = makeProfile(displayName: "Larry Song",
                                   samplePaths: ["samples/\(UUID().uuidString)/sample.wav"])
        try store.save(profile)

        let loaded = try store.load(id: profile.id)
        #expect(loaded == profile)
        #expect(loaded.samplePaths == profile.samplePaths)
        #expect(loaded.embedding == profile.embedding)
    }

    @Test func profileJSONFileIsWrittenAtExpectedPath() throws {
        let dir = tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = SpeakerProfileStore(directory: dir)

        let profile = makeProfile(displayName: "Larry Song")
        try store.save(profile)

        let expectedPath = dir.appendingPathComponent("profiles/\(profile.id.uuidString).json")
        #expect(FileManager.default.fileExists(atPath: expectedPath.path))
    }

    // MARK: - Index consistency

    @Test func indexReflectsSavedProfiles() throws {
        let dir = tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = SpeakerProfileStore(directory: dir)

        let a = makeProfile(displayName: "Alice")
        let b = makeProfile(displayName: "Bob")
        try store.save(a)
        try store.save(b)

        let index = try store.loadIndex()
        #expect(index.profiles.count == 2)
        #expect(Set(index.profiles.map(\.id)) == Set([a.id, b.id]))

        let summaries = try store.listSummaries()
        #expect(summaries.map(\.displayName) == ["Alice", "Bob"])
        #expect(summaries.first(where: { $0.id == a.id })?.sampleCount == a.samplePaths.count)
    }

    @Test func savingSameIDTwiceUpdatesIndexEntryRatherThanDuplicating() throws {
        let dir = tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = SpeakerProfileStore(directory: dir)

        var profile = makeProfile(displayName: "Larry")
        try store.save(profile)

        profile.embedding = [0.9, 0.9, 0.9]
        profile.updatedAt = Date()
        try store.save(profile)

        let index = try store.loadIndex()
        #expect(index.profiles.count == 1)

        let loaded = try store.load(id: profile.id)
        #expect(loaded.embedding == [0.9, 0.9, 0.9])
    }

    @Test func indexReflectsRename() throws {
        let dir = tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = SpeakerProfileStore(directory: dir)

        let profile = makeProfile(displayName: "Larry")
        try store.save(profile)

        let renamed = try store.rename(id: profile.id, to: "Larry Song")
        #expect(renamed.displayName == "Larry Song")
        #expect(renamed.id == profile.id)

        let index = try store.loadIndex()
        #expect(index.profiles.first(where: { $0.id == profile.id })?.displayName == "Larry Song")

        let loaded = try store.load(id: profile.id)
        #expect(loaded.displayName == "Larry Song")
    }

    @Test func indexReflectsDeletion() throws {
        let dir = tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = SpeakerProfileStore(directory: dir)

        let a = makeProfile(displayName: "Alice")
        let b = makeProfile(displayName: "Bob")
        try store.save(a)
        try store.save(b)

        try store.delete(id: a.id)

        let index = try store.loadIndex()
        #expect(index.profiles.count == 1)
        #expect(index.profiles.first?.id == b.id)

        #expect(throws: SpeakerProfileError.self) {
            try store.load(id: a.id)
        }
    }

    // MARK: - findByName

    @Test func findByNameReturnsUniqueMatch() throws {
        let dir = tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = SpeakerProfileStore(directory: dir)

        let profile = makeProfile(displayName: "Larry Song")
        try store.save(profile)

        let found = try store.findByName("Larry Song")
        #expect(found.id == profile.id)
    }

    @Test func findByNameMatchesCaseInsensitively() throws {
        let dir = tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = SpeakerProfileStore(directory: dir)

        let profile = makeProfile(displayName: "Larry Song")
        try store.save(profile)

        let found = try store.findByName("larry song")
        #expect(found.id == profile.id)
    }

    @Test func findByNameTrimsSurroundingWhitespace() throws {
        let dir = tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = SpeakerProfileStore(directory: dir)

        let profile = makeProfile(displayName: "Larry Song")
        try store.save(profile)

        let found = try store.findByName("  Larry Song\n")
        #expect(found.id == profile.id)
    }

    @Test func findByNameThrowsWhenNoMatch() throws {
        let dir = tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = SpeakerProfileStore(directory: dir)

        try store.save(makeProfile(displayName: "Alice"))

        #expect(throws: SpeakerProfileError.self) {
            try store.findByName("Nobody")
        }
    }

    @Test func findByNameThrowsAmbiguousErrorWhenDuplicateNamesExist() throws {
        let dir = tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = SpeakerProfileStore(directory: dir)

        let a = makeProfile(displayName: "Larry")
        let b = makeProfile(displayName: "Larry")
        try store.save(a)
        try store.save(b)

        do {
            _ = try store.findByName("Larry")
            Issue.record("Expected ambiguousName error to be thrown")
        } catch SpeakerProfileError.ambiguousName(let name, let ids) {
            #expect(name == "Larry")
            #expect(Set(ids) == Set([a.id, b.id]))
        }
    }

    @Test func findByNameThrowsAmbiguousErrorForCaseInsensitiveDuplicates() throws {
        let dir = tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = SpeakerProfileStore(directory: dir)

        let a = makeProfile(displayName: "Larry")
        let b = makeProfile(displayName: "larry")
        try store.save(a)
        try store.save(b)

        do {
            _ = try store.findByName("LARRY")
            Issue.record("Expected ambiguousName error to be thrown")
        } catch SpeakerProfileError.ambiguousName(let name, let ids) {
            #expect(name == "LARRY")
            #expect(Set(ids) == Set([a.id, b.id]))
        }
    }

    // MARK: - Duplicate display names allowed for storage

    @Test func duplicateDisplayNamesAreAllowedInStorage() throws {
        let dir = tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = SpeakerProfileStore(directory: dir)

        let a = makeProfile(displayName: "Larry")
        let b = makeProfile(displayName: "Larry")
        try store.save(a)
        try store.save(b)

        let index = try store.loadIndex()
        #expect(index.profiles.count == 2)
    }

    // MARK: - Relative sample paths

    @Test func samplePathsStayRelativeToProfilesDirectory() throws {
        let dir = tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = SpeakerProfileStore(directory: dir)

        let relative = "samples/some-id/20260706-120000.wav"
        let profile = makeProfile(displayName: "Larry", samplePaths: [relative])
        try store.save(profile)

        let loaded = try store.load(id: profile.id)
        #expect(loaded.samplePaths == [relative])
        #expect(!loaded.samplePaths.contains { $0.hasPrefix("/") })

        let resolved = store.absoluteSamplePath(relative)
        #expect(resolved == dir.appendingPathComponent(relative))
    }

    @Test func sampleDirectoryHelperPointsUnderSamplesDirectory() throws {
        let dir = tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = SpeakerProfileStore(directory: dir)

        let id = UUID()
        let sampleDir = store.sampleDirectory(forProfileID: id)
        #expect(sampleDir == dir.appendingPathComponent("samples/\(id.uuidString)"))
    }

    // MARK: - Deletion removes profile JSON and samples

    @Test func deletionRemovesProfileJSONAndSamplesDirectory() throws {
        let dir = tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = SpeakerProfileStore(directory: dir)

        let profile = makeProfile(displayName: "Larry",
                                   samplePaths: ["samples/placeholder/sample.wav"])
        try store.save(profile)

        let sampleDir = store.sampleDirectory(forProfileID: profile.id)
        try FileManager.default.createDirectory(at: sampleDir, withIntermediateDirectories: true)
        let sampleFile = sampleDir.appendingPathComponent("sample.wav")
        try Data("fake-audio".utf8).write(to: sampleFile)

        #expect(FileManager.default.fileExists(atPath: store.profileFileURL(id: profile.id).path))
        #expect(FileManager.default.fileExists(atPath: sampleFile.path))

        try store.delete(id: profile.id)

        #expect(!FileManager.default.fileExists(atPath: store.profileFileURL(id: profile.id).path))
        #expect(!FileManager.default.fileExists(atPath: sampleDir.path))
    }

    @Test func deletingUnknownIDThrowsNotFound() throws {
        let dir = tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = SpeakerProfileStore(directory: dir)

        #expect(throws: SpeakerProfileError.self) {
            try store.delete(id: UUID())
        }
    }

    // MARK: - listProfiles

    @Test func listProfilesReturnsFullProfilesSortedByDisplayName() throws {
        let dir = tempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = SpeakerProfileStore(directory: dir)

        let z = makeProfile(displayName: "Zed")
        let a = makeProfile(displayName: "Amy")
        try store.save(z)
        try store.save(a)

        let all = try store.listProfiles()
        #expect(all.map(\.displayName) == ["Amy", "Zed"])
        #expect(all.map(\.id) == [a.id, z.id])
    }
}
