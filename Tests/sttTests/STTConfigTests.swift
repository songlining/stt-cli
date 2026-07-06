import Foundation
import Testing
@testable import sttCore

@Suite("STTConfig")
struct STTConfigTests {

    private func tempDirectory() -> URL {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    private func write(_ json: String, to url: URL) throws {
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try json.data(using: .utf8)!.write(to: url)
    }

    // MARK: - Defaults

    @Test func defaultsWhenNoConfigExists() throws {
        let home = tempDirectory()
        defer { try? FileManager.default.removeItem(at: home) }

        let config = try STTConfigLoader.load(explicitPath: nil, environment: ["HOME": home.path])

        #expect(config == STTConfig.default)
        #expect(config.speakerProfilesDir == nil)
        #expect(config.speakerIdentification.enabled == false)
        #expect(config.speakerIdentification.provider == "speechbrain")
        #expect(config.speakerIdentification.matchThreshold == 0.78)
        #expect(config.speakerIdentification.matchMargin == 0.05)
        #expect(config.speakerIdentification.minimumSpeechSeconds == 8.0)
        #expect(config.artifactExport.enabled == false)
        #expect(config.artifactExport.includeAudio == false)
        #expect(config.artifactExport.overwrite == false)
        #expect(config.artifactExport.hookTimeoutSeconds == 30)
    }

    // MARK: - Discovery order

    @Test func stConfigEnvironmentOverrideIsUsedWhenNoExplicitPath() throws {
        let home = tempDirectory()
        defer { try? FileManager.default.removeItem(at: home) }

        let envConfigPath = home.appendingPathComponent("env-config.json")
        try write("""
        { "speakerProfilesDir": "/tmp/from-env-config" }
        """, to: envConfigPath)

        let config = try STTConfigLoader.load(
            explicitPath: nil,
            environment: ["HOME": home.path, "STT_CONFIG": envConfigPath.path]
        )

        #expect(config.speakerProfilesDir == "/tmp/from-env-config")
    }

    @Test func explicitPathBeatsEnvironmentAndDefault() throws {
        let home = tempDirectory()
        defer { try? FileManager.default.removeItem(at: home) }

        let envConfigPath = home.appendingPathComponent("env-config.json")
        try write("""
        { "speakerProfilesDir": "/tmp/from-env-config" }
        """, to: envConfigPath)

        let explicitConfigPath = home.appendingPathComponent("explicit-config.json")
        try write("""
        { "speakerProfilesDir": "/tmp/from-explicit-config" }
        """, to: explicitConfigPath)

        let config = try STTConfigLoader.load(
            explicitPath: explicitConfigPath.path,
            environment: ["HOME": home.path, "STT_CONFIG": envConfigPath.path]
        )

        #expect(config.speakerProfilesDir == "/tmp/from-explicit-config")
    }

    @Test func builtInDefaultLocationIsUsedWhenNoOverrides() throws {
        let home = tempDirectory()
        defer { try? FileManager.default.removeItem(at: home) }

        let defaultConfigPath = home.appendingPathComponent(".config/stt/config.json")
        try write("""
        { "speakerProfilesDir": "/tmp/from-default-config" }
        """, to: defaultConfigPath)

        let config = try STTConfigLoader.load(explicitPath: nil, environment: ["HOME": home.path])

        #expect(config.speakerProfilesDir == "/tmp/from-default-config")
    }

    @Test func missingBuiltInDefaultConfigIsNonFatal() throws {
        let home = tempDirectory()
        defer { try? FileManager.default.removeItem(at: home) }

        // No file at ~/.config/stt/config.json.
        let config = try STTConfigLoader.load(explicitPath: nil, environment: ["HOME": home.path])
        #expect(config == STTConfig.default)
    }

    @Test func missingExplicitPathIsAnError() {
        let home = tempDirectory()
        defer { try? FileManager.default.removeItem(at: home) }

        let missingPath = home.appendingPathComponent("does-not-exist.json").path

        #expect(throws: STTConfigError.self) {
            try STTConfigLoader.load(explicitPath: missingPath, environment: ["HOME": home.path])
        }
    }

    @Test func missingSTTConfigEnvPathIsAnError() {
        let home = tempDirectory()
        defer { try? FileManager.default.removeItem(at: home) }

        let missingPath = home.appendingPathComponent("does-not-exist.json").path

        #expect(throws: STTConfigError.self) {
            try STTConfigLoader.load(explicitPath: nil, environment: ["HOME": home.path, "STT_CONFIG": missingPath])
        }
    }

    // MARK: - Malformed JSON

    @Test func malformedJSONThrowsError() throws {
        let home = tempDirectory()
        defer { try? FileManager.default.removeItem(at: home) }

        let configPath = home.appendingPathComponent("config.json")
        try write("{ not valid json ", to: configPath)

        #expect(throws: STTConfigError.self) {
            try STTConfigLoader.load(explicitPath: configPath.path, environment: ["HOME": home.path])
        }
    }

    // MARK: - Validation

    @Test func invalidMatchThresholdAboveOneThrows() throws {
        let home = tempDirectory()
        defer { try? FileManager.default.removeItem(at: home) }

        let configPath = home.appendingPathComponent("config.json")
        try write("""
        { "speakerIdentification": { "matchThreshold": 1.5 } }
        """, to: configPath)

        #expect(throws: STTConfigError.self) {
            try STTConfigLoader.load(explicitPath: configPath.path, environment: ["HOME": home.path])
        }
    }

    @Test func invalidMatchThresholdBelowZeroThrows() throws {
        let home = tempDirectory()
        defer { try? FileManager.default.removeItem(at: home) }

        let configPath = home.appendingPathComponent("config.json")
        try write("""
        { "speakerIdentification": { "matchThreshold": -0.1 } }
        """, to: configPath)

        #expect(throws: STTConfigError.self) {
            try STTConfigLoader.load(explicitPath: configPath.path, environment: ["HOME": home.path])
        }
    }

    @Test func invalidMatchMarginThrows() throws {
        let home = tempDirectory()
        defer { try? FileManager.default.removeItem(at: home) }

        let configPath = home.appendingPathComponent("config.json")
        try write("""
        { "speakerIdentification": { "matchMargin": -0.01 } }
        """, to: configPath)

        #expect(throws: STTConfigError.self) {
            try STTConfigLoader.load(explicitPath: configPath.path, environment: ["HOME": home.path])
        }
    }

    @Test func invalidMinimumSpeechSecondsThrows() throws {
        let home = tempDirectory()
        defer { try? FileManager.default.removeItem(at: home) }

        let configPath = home.appendingPathComponent("config.json")
        try write("""
        { "speakerIdentification": { "minimumSpeechSeconds": 0 } }
        """, to: configPath)

        #expect(throws: STTConfigError.self) {
            try STTConfigLoader.load(explicitPath: configPath.path, environment: ["HOME": home.path])
        }
    }

    @Test func emptySpeakerProfilesDirThrows() throws {
        let home = tempDirectory()
        defer { try? FileManager.default.removeItem(at: home) }

        let configPath = home.appendingPathComponent("config.json")
        try write("""
        { "speakerProfilesDir": "" }
        """, to: configPath)

        #expect(throws: STTConfigError.self) {
            try STTConfigLoader.load(explicitPath: configPath.path, environment: ["HOME": home.path])
        }
    }

    @Test func emptyArtifactExportTargetDirThrows() throws {
        let home = tempDirectory()
        defer { try? FileManager.default.removeItem(at: home) }

        let configPath = home.appendingPathComponent("config.json")
        try write("""
        { "artifactExport": { "targetDir": "" } }
        """, to: configPath)

        #expect(throws: STTConfigError.self) {
            try STTConfigLoader.load(explicitPath: configPath.path, environment: ["HOME": home.path])
        }
    }

    @Test func enabledArtifactExportWithoutTargetDirThrows() throws {
        let home = tempDirectory()
        defer { try? FileManager.default.removeItem(at: home) }

        let configPath = home.appendingPathComponent("config.json")
        try write("""
        { "artifactExport": { "enabled": true } }
        """, to: configPath)

        #expect(throws: STTConfigError.self) {
            try STTConfigLoader.load(explicitPath: configPath.path, environment: ["HOME": home.path])
        }
    }

    @Test func enabledArtifactExportWithTargetDirSucceeds() throws {
        let home = tempDirectory()
        defer { try? FileManager.default.removeItem(at: home) }

        let configPath = home.appendingPathComponent("config.json")
        try write("""
        { "artifactExport": { "enabled": true, "targetDir": "/tmp/meetings" } }
        """, to: configPath)

        let config = try STTConfigLoader.load(explicitPath: configPath.path, environment: ["HOME": home.path])
        #expect(config.artifactExport.enabled == true)
        #expect(config.artifactExport.targetDir == "/tmp/meetings")
    }

    @Test func invalidHookTimeoutThrows() throws {
        let home = tempDirectory()
        defer { try? FileManager.default.removeItem(at: home) }

        let configPath = home.appendingPathComponent("config.json")
        try write("""
        { "artifactExport": { "hookTimeoutSeconds": 0 } }
        """, to: configPath)

        #expect(throws: STTConfigError.self) {
            try STTConfigLoader.load(explicitPath: configPath.path, environment: ["HOME": home.path])
        }
    }

    @Test func emptyPostPipelineCommandExecutableThrows() throws {
        let home = tempDirectory()
        defer { try? FileManager.default.removeItem(at: home) }

        let configPath = home.appendingPathComponent("config.json")
        try write("""
        {
          "artifactExport": {
            "postPipelineCommand": { "executable": "", "arguments": [] }
          }
        }
        """, to: configPath)

        #expect(throws: STTConfigError.self) {
            try STTConfigLoader.load(explicitPath: configPath.path, environment: ["HOME": home.path])
        }
    }

    @Test func fullConfigParsesStructuredPostPipelineCommand() throws {
        let home = tempDirectory()
        defer { try? FileManager.default.removeItem(at: home) }

        let configPath = home.appendingPathComponent("config.json")
        try write("""
        {
          "speakerProfilesDir": "/Users/me/Documents/stt-speakers",
          "speakerIdentification": {
            "enabled": true,
            "provider": "speechbrain",
            "matchThreshold": 0.78,
            "matchMargin": 0.05,
            "minimumSpeechSeconds": 8.0
          },
          "artifactExport": {
            "enabled": true,
            "targetDir": "/Users/me/Documents/Meetings",
            "includeAudio": false,
            "overwrite": false,
            "postPipelineCommand": {
              "executable": "/Users/me/bin/file-meeting-note",
              "arguments": ["--transcript", "{transcriptPath}", "--target", "{targetDir}"]
            }
          }
        }
        """, to: configPath)

        let config = try STTConfigLoader.load(explicitPath: configPath.path, environment: ["HOME": home.path])

        #expect(config.speakerProfilesDir == "/Users/me/Documents/stt-speakers")
        #expect(config.speakerIdentification.enabled == true)
        #expect(config.artifactExport.postPipelineCommand?.executable == "/Users/me/bin/file-meeting-note")
        #expect(config.artifactExport.postPipelineCommand?.arguments == [
            "--transcript", "{transcriptPath}", "--target", "{targetDir}"
        ])
    }

    // MARK: - Paths.speakerProfilesDirectory

    @Test func speakerProfilesDirectoryRespectsConfigOverride() {
        var config = STTConfig()
        config.speakerProfilesDir = "/tmp/my-speaker-profiles"

        let dir = Paths.speakerProfilesDirectory(config: config, environment: [:])
        #expect(dir.path == "/tmp/my-speaker-profiles")
    }

    @Test func speakerProfilesDirectoryFallsBackToSTTHomeScopedDefault() {
        let config = STTConfig()
        let env = ["STT_HOME": "/tmp/stt-test-home-speakers"]

        let dir = Paths.speakerProfilesDirectory(config: config, environment: env)
        #expect(dir.path == "/tmp/stt-test-home-speakers/speakers")
    }
}
