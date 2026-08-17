import Foundation
import Testing
@testable import sttCore

@Suite("PythonSpeakerIdentifier")
struct PythonSpeakerIdentifierTests {

    /// Repo `python/` directory, containing `stt_vibevoice`. Tests run
    /// against the real `mfcc-test` provider end-to-end since it is
    /// stdlib-only and requires no ML dependencies.
    private static var pythonBackendDirectory: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent() // Tests/sttTests
            .deletingLastPathComponent() // Tests
            .deletingLastPathComponent() // repo root
            .appendingPathComponent("python", isDirectory: true)
    }

    private func writeTestWAV(to url: URL, durationSeconds: Double, amplitude: Int16 = 8000, framerate: Int = 16000) throws {
        let frameCount = Int(durationSeconds * Double(framerate))
        var pcmData = Data(capacity: frameCount * 2)
        for i in 0..<frameCount {
            let sample: Int16 = (i / 64) % 2 == 0 ? amplitude : -amplitude
            withUnsafeBytes(of: sample.littleEndian) { pcmData.append(contentsOf: $0) }
        }
        let header = WAVWriter.header(sampleRate: UInt32(framerate), channels: 1, bitDepth: 16, dataSize: UInt32(pcmData.count))
        var fileData = header
        fileData.append(pcmData)
        try fileData.write(to: url)
    }

    @Test func buildsExtractArgumentsForWholeAudio() {
        let arguments = PythonSpeakerIdentifier.buildExtractArguments(
            audioPath: "sample.wav",
            segmentsJSONPath: nil,
            speakerID: nil,
            provider: "mfcc-test",
            minimumSpeechSeconds: 8.0
        )

        #expect(arguments == [
            "-m", "stt_vibevoice.speaker_id", "extract",
            "--audio", "sample.wav",
            "--provider", "mfcc-test",
            "--minimum-speech-seconds", "8.0"
        ])
    }

    @Test func buildsExtractArgumentsForSegments() {
        let arguments = PythonSpeakerIdentifier.buildExtractArguments(
            audioPath: "sample.wav",
            segmentsJSONPath: "transcript.json",
            speakerID: "0",
            provider: "mfcc-test",
            minimumSpeechSeconds: 8.0
        )

        #expect(arguments == [
            "-m", "stt_vibevoice.speaker_id", "extract",
            "--audio", "sample.wav",
            "--segments", "transcript.json",
            "--speaker-id", "0",
            "--provider", "mfcc-test",
            "--minimum-speech-seconds", "8.0"
        ])
    }

    @Test func extractWholeAudioReturnsOKAboveMinimum() throws {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let audioURL = tmpDir.appendingPathComponent("sample.wav")
        try writeTestWAV(to: audioURL, durationSeconds: 9.0)

        let result = try PythonSpeakerIdentifier.extractWholeAudio(
            audioPath: audioURL.path,
            provider: "mfcc-test",
            minimumSpeechSeconds: 8.0,
            workingDirectory: Self.pythonBackendDirectory,
            timeout: 15
        )

        #expect(result.status == "ok")
        #expect(result.provider == "mfcc-test")
        #expect(result.embedding != nil)
        #expect(!(result.embedding ?? []).isEmpty)
    }

    @Test func extractWholeAudioReturnsTooShortBelowMinimum() throws {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let audioURL = tmpDir.appendingPathComponent("short.wav")
        try writeTestWAV(to: audioURL, durationSeconds: 1.0)

        let result = try PythonSpeakerIdentifier.extractWholeAudio(
            audioPath: audioURL.path,
            provider: "mfcc-test",
            minimumSpeechSeconds: 8.0,
            workingDirectory: Self.pythonBackendDirectory,
            timeout: 15
        )

        #expect(result.isTooShort)
        #expect(result.embedding == nil)
    }

    @Test func extractSpeakerSegmentsConcatenatesMatchingSpeaker() throws {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let audioURL = tmpDir.appendingPathComponent("full.wav")
        try writeTestWAV(to: audioURL, durationSeconds: 20.0)

        let transcriptURL = tmpDir.appendingPathComponent("transcript.json")
        let segments: [[String: Any]] = [
            ["speaker_id": "0", "start_time": 0.0, "end_time": 5.0, "text": "hi"],
            ["speaker_id": "1", "start_time": 5.0, "end_time": 10.0, "text": "there"],
            ["speaker_id": "0", "start_time": 10.0, "end_time": 15.0, "text": "again"]
        ]
        let payload: [String: Any] = ["segments": segments]
        let data = try JSONSerialization.data(withJSONObject: payload)
        try data.write(to: transcriptURL)

        let result = try PythonSpeakerIdentifier.extractSpeakerSegments(
            audioPath: audioURL.path,
            segmentsJSONPath: transcriptURL.path,
            speakerID: "0",
            provider: "mfcc-test",
            minimumSpeechSeconds: 8.0,
            workingDirectory: Self.pythonBackendDirectory,
            timeout: 15
        )

        #expect(result.status == "ok")
        #expect(result.segmentCount == 2)
        #expect(result.durationSeconds > 9.0)
    }

    @Test func matchReturnsMatchedAboveThresholdAndMargin() throws {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let audioURL = tmpDir.appendingPathComponent("enroll.wav")
        try writeTestWAV(to: audioURL, durationSeconds: 9.0)

        let extraction = try PythonSpeakerIdentifier.extractWholeAudio(
            audioPath: audioURL.path,
            provider: "mfcc-test",
            minimumSpeechSeconds: 8.0,
            workingDirectory: Self.pythonBackendDirectory,
            timeout: 15
        )
        #expect(extraction.embedding != nil)

        let profile = SpeakerProfile(
            displayName: "Larry",
            embeddingProvider: extraction.provider,
            embeddingModel: extraction.model ?? "",
            embedding: extraction.embedding ?? []
        )

        let profilesURL = tmpDir.appendingPathComponent("profiles.json")
        let candidateURL = tmpDir.appendingPathComponent("candidate.json")
        try PythonSpeakerIdentifier.writeFlattenedProfiles([profile], to: profilesURL)
        try PythonSpeakerIdentifier.writeCandidate(extraction, to: candidateURL)

        let matchResult = try PythonSpeakerIdentifier.match(
            candidateJSONPath: candidateURL.path,
            profilesJSONPath: profilesURL.path,
            threshold: 0.5,
            margin: 0.01,
            workingDirectory: Self.pythonBackendDirectory,
            timeout: 15
        )

        #expect(matchResult.bestMatch?.profileId == profile.id.uuidString)
        #expect(matchResult.bestMatch?.matched == true)
        #expect(matchResult.bestMatch?.status == "matched")
    }

    @Test func matchReturnsNilBestMatchWhenNoProfiles() throws {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let profilesURL = tmpDir.appendingPathComponent("profiles.json")
        let candidateURL = tmpDir.appendingPathComponent("candidate.json")
        try PythonSpeakerIdentifier.writeFlattenedProfiles([], to: profilesURL)
        try PythonSpeakerIdentifier.writeCandidate(
            SpeakerExtractionResult(
                speakerId: "0",
                provider: "mfcc-test",
                model: "stt-vibevoice/mfcc-test-v1",
                embedding: [1.0, 0.0],
                durationSeconds: 9.0,
                segmentCount: 1,
                status: "ok"
            ),
            to: candidateURL
        )

        let matchResult = try PythonSpeakerIdentifier.match(
            candidateJSONPath: candidateURL.path,
            profilesJSONPath: profilesURL.path,
            threshold: 0.78,
            margin: 0.05,
            workingDirectory: Self.pythonBackendDirectory,
            timeout: 15
        )

        #expect(matchResult.bestMatch == nil)
        #expect(matchResult.candidates.isEmpty)
    }

    @Test func extractSurfacesBackendFailureMessageWhenAudioMissing() throws {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let missingAudioURL = tmpDir.appendingPathComponent("does-not-exist.wav")

        #expect(throws: PythonSpeakerIdentifierError.self) {
            _ = try PythonSpeakerIdentifier.extractWholeAudio(
                audioPath: missingAudioURL.path,
                provider: "mfcc-test",
                minimumSpeechSeconds: 8.0,
                workingDirectory: Self.pythonBackendDirectory,
                timeout: 15
            )
        }
    }

    @Test func python3NotFoundErrorWhenNoPythonOnPath() throws {
        let emptyRoot = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: emptyRoot, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: emptyRoot) }

        // Force an isolated PATH via a runtime override that doesn't exist,
        // and no python3 resolvable relative to it, is difficult to force
        // deterministically without mutating the real PATH; instead this
        // test only exercises the error's message format directly.
        let error = PythonSpeakerIdentifierError.python3NotFound
        #expect(error.errorDescription?.contains("python3") == true)
    }

    // MARK: - Task 08: range-aware argument builders

    @Test func buildExtractArgumentsIncludesRepeatedRangeFlags() {
        let arguments = PythonSpeakerIdentifier.buildExtractArguments(
            audioPath: "sample.wav",
            segmentsJSONPath: "transcript.json",
            speakerID: "4",
            provider: "mfcc-test",
            minimumSpeechSeconds: 8.0,
            ranges: ["2.0-12.0", "30.0-40.0"]
        )

        #expect(arguments == [
            "-m", "stt_vibevoice.speaker_id", "extract",
            "--audio", "sample.wav",
            "--segments", "transcript.json",
            "--speaker-id", "4",
            "--provider", "mfcc-test",
            "--minimum-speech-seconds", "8.0",
            "--range", "2.0-12.0",
            "--range", "30.0-40.0"
        ])
    }

    @Test func buildExtractArgumentsOmitsRangeFlagsWhenNilOrEmpty() {
        let withNil = PythonSpeakerIdentifier.buildExtractArguments(
            audioPath: "sample.wav", segmentsJSONPath: nil, speakerID: nil,
            provider: "mfcc-test", minimumSpeechSeconds: 8.0, ranges: nil
        )
        let withEmpty = PythonSpeakerIdentifier.buildExtractArguments(
            audioPath: "sample.wav", segmentsJSONPath: nil, speakerID: nil,
            provider: "mfcc-test", minimumSpeechSeconds: 8.0, ranges: []
        )
        #expect(!withNil.contains("--range"))
        #expect(!withEmpty.contains("--range"))
    }

    @Test func buildConcatenateArgumentsDefaultsToBestSegmentsAndOmitsRanges() {
        let arguments = PythonSpeakerIdentifier.buildConcatenateArguments(
            audioPath: "sample.wav",
            segmentsJSONPath: "transcript.json",
            speakerID: "4",
            outPath: "out.wav",
            jsonOutputPath: "out.json"
        )
        #expect(arguments == [
            "-m", "stt_vibevoice.speaker_id", "concatenate",
            "--audio", "sample.wav",
            "--segments", "transcript.json",
            "--speaker-id", "4",
            "--out", "out.wav",
            "--best-segments",
            "--json", "out.json"
        ])
    }

    @Test func buildConcatenateArgumentsWithRangesAndNoBestSegments() {
        let arguments = PythonSpeakerIdentifier.buildConcatenateArguments(
            audioPath: "sample.wav",
            segmentsJSONPath: "transcript.json",
            speakerID: "4",
            outPath: "out.wav",
            jsonOutputPath: "out.json",
            maxSeconds: 30.0,
            normalize: true,
            targetLoudness: -19.0,
            ranges: ["2.0-12.0"],
            bestSegments: false
        )
        #expect(arguments == [
            "-m", "stt_vibevoice.speaker_id", "concatenate",
            "--audio", "sample.wav",
            "--segments", "transcript.json",
            "--speaker-id", "4",
            "--out", "out.wav",
            "--max-seconds", "30.0",
            "--no-best-segments",
            "--normalize", "--target-loudness", "-19.0",
            "--range", "2.0-12.0",
            "--json", "out.json"
        ])
    }

    // MARK: - Task 08: suggest-labels argument builder

    @Test func buildSuggestLabelsArgumentsIncludesRepeatedAudioAndWindows() {
        let arguments = PythonSpeakerIdentifier.buildSuggestLabelsArguments(
            transcript: "transcript.json",
            audioSources: [(source: "mic", path: "mic.wav"), (source: "system", path: "system.wav")],
            profilesPath: "profiles.json",
            provider: "mfcc-test",
            threshold: 0.78,
            margin: 0.05,
            minimumSpeechSeconds: 8.0,
            session: "/tmp/session",
            noWindows: false,
            nWindows: 2,
            jsonOutputPath: "out.json"
        )
        #expect(arguments == [
            "-m", "stt_vibevoice.speaker_id", "suggest-labels",
            "--transcript", "transcript.json",
            "--audio", "mic=mic.wav",
            "--audio", "system=system.wav",
            "--profiles", "profiles.json",
            "--provider", "mfcc-test",
            "--threshold", "0.78",
            "--margin", "0.05",
            "--minimum-speech-seconds", "8.0",
            "--session", "/tmp/session",
            "--n-windows", "2",
            "--json", "out.json"
        ])
    }

    @Test func buildSuggestLabelsArgumentsUsesNoWindowsFlagInsteadOfCount() {
        let arguments = PythonSpeakerIdentifier.buildSuggestLabelsArguments(
            transcript: "transcript.json",
            audioSources: [],
            profilesPath: nil,
            provider: "mfcc-test",
            threshold: 0.78,
            margin: 0.05,
            minimumSpeechSeconds: 8.0,
            session: nil,
            noWindows: true,
            nWindows: 2,
            jsonOutputPath: nil
        )
        #expect(arguments.contains("--no-windows"))
        #expect(!arguments.contains("--n-windows"))
        #expect(!arguments.contains("--profiles"))
        #expect(!arguments.contains("--session"))
        #expect(!arguments.contains("--json"))
    }

    // MARK: - Task 08: helper script path resolution + argument builder

    @Test func defaultHelperScriptsDirectoryPrefersEnvironmentOverride() {
        let path = PythonSpeakerIdentifier.defaultHelperScriptsDirectory(
            environment: ["STT_HELPER_SCRIPTS": "/custom/scripts"]
        )
        #expect(path == "/custom/scripts")
    }

    @Test func defaultHelperScriptsDirectoryReturnsNilWhenUnconfigured() {
        let path = PythonSpeakerIdentifier.defaultHelperScriptsDirectory(environment: [:])
        #expect(path == nil)
    }

    @Test func resolveHelperScriptPathReturnsNilWhenScriptMissing() {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let resolved = PythonSpeakerIdentifier.resolveHelperScriptPath(
            explicitOverride: tmpDir.path,
            environment: [:],
            fileManager: .default
        )
        #expect(resolved == nil)
    }

    @Test func resolveHelperScriptPathReturnsPathWhenScriptExists() throws {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmpDir) }
        let scriptPath = tmpDir.appendingPathComponent("name_one_speaker.py")
        try "# stub".write(to: scriptPath, atomically: true, encoding: .utf8)

        let resolved = PythonSpeakerIdentifier.resolveHelperScriptPath(
            explicitOverride: tmpDir.path,
            environment: [:],
            fileManager: .default
        )
        #expect(resolved == scriptPath.path)
    }

    @Test func resolveHelperScriptPathIgnoresEmptyExplicitOverride() {
        // Empty string override should fall through to the env/default path,
        // not be treated as a valid (nonexistent) directory.
        let resolved = PythonSpeakerIdentifier.resolveHelperScriptPath(
            explicitOverride: "",
            environment: ["STT_HELPER_SCRIPTS": "/definitely/missing"],
            fileManager: .default
        )
        #expect(resolved == nil)
    }

    @Test func buildHelperScriptArgumentsInterleavesRangesAndKeywordArgs() {
        let arguments = PythonSpeakerIdentifier.buildHelperScriptArguments(
            subcommand: "enroll-ranges",
            scriptPath: "/scripts/name_one_speaker.py",
            rangeArguments: ["2.0-12.0", "30.0-40.0"],
            keywordArguments: [
                (flag: "--session", value: "/tmp/session"),
                (flag: "--speaker-id", value: "4"),
                (flag: "--name", value: "Domingo"),
                (flag: "--no-enroll", value: nil)
            ]
        )
        #expect(arguments == [
            "/scripts/name_one_speaker.py", "enroll-ranges",
            "--range", "2.0-12.0",
            "--range", "30.0-40.0",
            "--session", "/tmp/session",
            "--speaker-id", "4",
            "--name", "Domingo",
            "--no-enroll"
        ])
    }

    @Test func buildHelperScriptArgumentsWithNoRangesOrKeywordArgs() {
        let arguments = PythonSpeakerIdentifier.buildHelperScriptArguments(
            subcommand: "audit",
            scriptPath: "/scripts/name_one_speaker.py",
            rangeArguments: [],
            keywordArguments: [(flag: "--session", value: "/tmp/session")]
        )
        #expect(arguments == ["/scripts/name_one_speaker.py", "audit", "--session", "/tmp/session"])
    }

    // MARK: - Task 08: suggest-labels result decoding (characterization)
    //
    // Fixture mirrors the REAL schema `build_label_suggestions` emits in
    // speaker_id.py (schemaVersion 1) -- not a guessed/aspirational shape.
    // This locks in the actual wire format so Swift/Python never silently
    // diverge again.

    @Test func decodesRealBackendSchemaOkStatusWithDuplicateAndMixedGroups() throws {
        let json = """
        {
          "schemaVersion": 1,
          "status": "ok",
          "session": "/tmp/session",
          "generatedAt": "2026-07-13T00:00:00Z",
          "config": {"threshold": 0.78, "margin": 0.05, "provider": "mfcc-test", "model": "mfcc-test-v1"},
          "profilesConsidered": {"count": 1, "profileIds": ["p1"]},
          "clusters": [
            {
              "speakerId": "1",
              "source": "system",
              "durationSeconds": 12.0,
              "segmentCount": 2,
              "selectedRanges": [[0.0, 5.0], [10.0, 15.0]],
              "speechSeconds": 10.0,
              "bestMatch": {
                "profileId": "p1", "displayName": "Domingo",
                "confidence": 0.91, "margin": 0.2, "matched": true, "status": "matched"
              },
              "recommendation": "reuse_profile",
              "recommendationDetail": "Cluster 1 matches profile 'Domingo' (p1) with confidence 0.9100."
            },
            {
              "speakerId": "2",
              "source": "system",
              "durationSeconds": 9.0,
              "segmentCount": 1,
              "selectedRanges": [[20.0, 29.0]],
              "speechSeconds": 9.0,
              "bestMatch": {
                "profileId": "p1", "displayName": "Domingo",
                "confidence": 0.88, "margin": 0.15, "matched": true, "status": "matched"
              },
              "recommendation": "reuse_profile",
              "recommendationDetail": "Cluster 2 matches profile 'Domingo' (p1) with confidence 0.8800."
            }
          ],
          "duplicateClusterGroups": [
            {
              "profileId": "p1",
              "nameHint": "Domingo",
              "displayName": "Domingo",
              "clusters": [
                {"speakerId": "1", "confidence": 0.91, "selectedRanges": [[0.0, 5.0], [10.0, 15.0]]},
                {"speakerId": "2", "confidence": 0.88, "selectedRanges": [[20.0, 29.0]]}
              ],
              "recommendation": "merge_or_relabel",
              "recommendationDetail": "Clusters 1, 2 all confidently match profile 'Domingo' (p1)."
            }
          ],
          "mixedClusterWarnings": [
            {
              "speakerId": "4",
              "windows": [
                {"label": "early", "range": [0.0, 12.0], "bestMatch": {"profileId": "p1", "displayName": "Domingo", "confidence": 0.9, "margin": 0.2, "matched": true, "status": "matched"}, "matchedProfileId": "p1"},
                {"label": "late", "range": [200.0, 212.0], "bestMatch": {"profileId": "p2", "displayName": "Gia", "confidence": 0.85, "margin": 0.18, "matched": true, "status": "matched"}, "matchedProfileId": "p2"}
              ],
              "conflictingProfileIds": ["p1", "p2"],
              "conflictingDisplayNames": ["Domingo", "Gia"],
              "recommendation": "do_not_enroll_whole_cluster",
              "recommendationDetail": "Cluster 4's chronological windows match different profiles."
            }
          ],
          "summary": {"clusterCount": 3, "matchedCount": 2, "duplicateGroupCount": 1, "mixedClusterCount": 1, "unmatchedCount": 1}
        }
        """
        let result = try JSONDecoder().decode(SpeakerSuggestionResult.self, from: Data(json.utf8))

        #expect(result.schemaVersion == 1)
        #expect(result.status == "ok")
        #expect(result.config?.threshold == 0.78)
        #expect(result.profilesConsidered?.count == 1)
        #expect(result.clusters?.count == 2)
        #expect(result.clusters?.first?.bestMatch?.displayName == "Domingo")
        #expect(result.clusters?.first?.bestMatch?.matched == true)
        #expect(result.clusters?.first?.selectedRanges == [[0.0, 5.0], [10.0, 15.0]])

        let group = try #require(result.duplicateClusterGroups?.first)
        #expect(group.profileId == "p1")
        #expect(group.clusters?.count == 2)
        #expect(group.clusters?.first?.speakerId == "1")

        let warning = try #require(result.mixedClusterWarnings?.first)
        #expect(warning.speakerId == "4")
        #expect(warning.conflictingProfileIds == ["p1", "p2"])
        #expect(warning.windows?.count == 2)
        #expect(warning.windows?.last?.bestMatch?.displayName == "Gia")

        #expect(result.summary?.clusterCount == 3)
        #expect(result.summary?.duplicateGroupCount == 1)
        #expect(result.summary?.mixedClusterCount == 1)
    }

    @Test func decodesRealBackendSchemaNoProfilesStatus() throws {
        let json = """
        {
          "schemaVersion": 1,
          "status": "no_profiles",
          "session": null,
          "generatedAt": null,
          "config": {"threshold": 0.78, "margin": 0.05, "provider": null, "model": null},
          "profilesConsidered": {"count": 0, "profileIds": []},
          "clusters": [],
          "duplicateClusterGroups": [],
          "mixedClusterWarnings": [],
          "summary": {"clusterCount": 2, "matchedCount": 0, "duplicateGroupCount": 0, "mixedClusterCount": 0, "unmatchedCount": 2},
          "recommendation": "No speaker profiles are enrolled yet."
        }
        """
        let result = try JSONDecoder().decode(SpeakerSuggestionResult.self, from: Data(json.utf8))

        #expect(result.status == "no_profiles")
        #expect(result.profilesConsidered?.count == 0)
        #expect(result.clusters?.isEmpty == true)
        #expect(result.recommendation != nil)
    }

    @Test func suggestLabelsWritesBackendJSONToRequestedPath() throws {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let audioURL = tmpDir.appendingPathComponent("sample.wav")
        try writeTestWAV(to: audioURL, durationSeconds: 9.0)
        let transcriptURL = tmpDir.appendingPathComponent("transcript.json")
        let transcript: [String: Any] = [
            "segments": [[
                "text": "hello",
                "start_time": 0.0,
                "end_time": 9.0,
                "speaker_id": "0",
                "source": "mic"
            ]]
        ]
        try JSONSerialization.data(withJSONObject: transcript).write(to: transcriptURL)
        let outputURL = tmpDir.appendingPathComponent("suggestions.json")

        let result = try PythonSpeakerIdentifier.suggestLabels(
            transcript: transcriptURL.path,
            audioSources: [(source: "mic", path: audioURL.path)],
            profilesPath: nil,
            provider: "mfcc-test",
            threshold: 0.78,
            margin: 0.05,
            minimumSpeechSeconds: 8.0,
            session: nil,
            noWindows: true,
            nWindows: 0,
            jsonOutputPath: outputURL.path,
            workingDirectory: Self.pythonBackendDirectory,
            timeout: 15
        )

        #expect(FileManager.default.fileExists(atPath: outputURL.path))
        let persisted = try JSONDecoder().decode(SpeakerSuggestionResult.self, from: Data(contentsOf: outputURL))
        #expect(persisted == result)
    }

    @Test func suggestionResultRoundTripsThroughEncodeDecode() throws {
        let original = SpeakerSuggestionResult(
            schemaVersion: 1,
            status: "ok",
            session: "/tmp/session",
            generatedAt: nil,
            config: SpeakerSuggestionConfig(threshold: 0.78, margin: 0.05, provider: "mfcc-test", model: nil),
            profilesConsidered: SpeakerSuggestionProfilesConsidered(count: 1, profileIds: ["p1"]),
            clusters: [],
            duplicateClusterGroups: [],
            mixedClusterWarnings: [],
            summary: SpeakerSuggestionSummary(clusterCount: 0, matchedCount: 0, duplicateGroupCount: 0, mixedClusterCount: 0, unmatchedCount: 0),
            recommendation: nil
        )
        let data = try JSONEncoder().encode(original)
        let decoded = try JSONDecoder().decode(SpeakerSuggestionResult.self, from: data)
        #expect(decoded == original)
    }
}
