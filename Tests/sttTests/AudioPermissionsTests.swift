import Testing
@testable import sttCore

@Suite("Audio permission guidance")
struct AudioPermissionsTests {

    @Test func microphoneResetGuidanceIncludesBundleIDAndSettingsPath() {
        let guidance = AudioPermissions.microphoneResetGuidance(bundleID: "com.example.stt")

        #expect(guidance.contains("tccutil reset Microphone com.example.stt"))
        #expect(guidance.contains("Privacy & Security > Microphone"))
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
