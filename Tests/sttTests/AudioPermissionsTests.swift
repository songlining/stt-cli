import Testing
@testable import sttCore

@Suite("Audio permission guidance")
struct AudioPermissionsTests {

    @Test func tccAttributionGuidanceIncludesBundleAndSmokeCommands() {
        let guidance = AudioPermissions.tccAttributionGuidance(bundleID: "com.example.stt")

        #expect(guidance.contains("./scripts/build-app-bundle.sh"))
        #expect(guidance.contains("./dist/stt.app/Contents/MacOS/stt doctor"))
        #expect(guidance.contains("STT_RESET_TCC=1 ./scripts/manual-tcc-smoke.sh"))
        #expect(guidance.contains("com.example.stt"))
    }

    @Test func microphoneResetGuidanceIncludesBundleIDAndSettingsPath() {
        let guidance = AudioPermissions.microphoneResetGuidance(bundleID: "com.example.stt")

        #expect(guidance.contains("tccutil reset Microphone com.example.stt"))
        #expect(guidance.contains("Privacy & Security > Microphone"))
        #expect(guidance.contains("STT_RESET_TCC=1 ./scripts/manual-tcc-smoke.sh"))
    }

    @Test func screenRecordingResetGuidanceIncludesBundleIDAndSettingsPath() {
        let guidance = AudioPermissions.screenRecordingResetGuidance(bundleID: "com.example.stt")

        #expect(guidance.contains("tccutil reset ScreenCapture com.example.stt"))
        #expect(guidance.contains("Privacy & Security > Screen Recording"))
    }

    @Test func audioCaptureResetGuidanceIncludesBundleID() {
        let guidance = AudioPermissions.audioCaptureResetGuidance(bundleID: "com.example.stt")

        #expect(guidance.contains("tccutil reset AudioCapture com.example.stt"))
        #expect(guidance.contains("native system-audio capture"))
    }
}
