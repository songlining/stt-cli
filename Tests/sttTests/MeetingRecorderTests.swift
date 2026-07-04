import Foundation
import Testing
@testable import sttCore

@Suite("MeetingRecorder")
struct MeetingRecorderTests {

    @Test func micStartFailedDescriptionWrapsUnderlyingError() {
        let error = DeviceListError.deviceNotFound("Unit Test Mic")
        let description = MeetingRecorderError.micStartFailed(error).errorDescription ?? ""

        #expect(description.contains("Failed to start mic capture:"))
        #expect(description.contains("No input device found matching \"Unit Test Mic\""))
    }

    @Test func systemStartFailedDescriptionWrapsUnderlyingFallbackGuidance() {
        let description = MeetingRecorderError.systemStartFailed(SystemAudioRecorderError.noFallbackDeviceConfigured).errorDescription ?? ""

        #expect(description.contains("Failed to start system-audio capture:"))
        #expect(description.contains("No fallback input device configured or found for system-audio capture."))
        #expect(description.contains("stt devices"))
        #expect(description.contains("BlackHole"))
        #expect(description.contains("STT_SYSTEM_DEVICE=\"BlackHole 2ch\" ./scripts/validate.sh"))
    }

    @Test func systemStartFailedDescriptionWrapsMissingExplicitDevice() {
        let error = DeviceListError.deviceNotFound("definitely-missing-stt-meeting-device")
        let description = MeetingRecorderError.systemStartFailed(error).errorDescription ?? ""

        #expect(description.contains("Failed to start system-audio capture:"))
        #expect(description.contains("No input device found matching \"definitely-missing-stt-meeting-device\""))
    }
}
