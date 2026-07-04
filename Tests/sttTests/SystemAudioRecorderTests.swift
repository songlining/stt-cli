import Testing
@testable import sttCore

@Suite("SystemAudioRecorder")
struct SystemAudioRecorderTests {

    @Test func noFallbackDeviceGuidanceIsActionable() {
        let guidance = SystemAudioRecorderError.fallbackConfigurationGuidance()

        #expect(guidance.contains("stt devices"))
        #expect(guidance.contains("stt record --mode system --input-device"))
        #expect(guidance.contains("--fail-if-empty"))
        #expect(guidance.contains("STT_SYSTEM_DEVICE=\"BlackHole 2ch\" ./scripts/validate.sh"))
    }

    @Test func nativeTapAvailabilityRequiresOSAndSymbols() {
        let available = NativeTapAvailability(
            osVersionSupported: true,
            createProcessTapSymbolAvailable: true,
            createAggregateDeviceSymbolAvailable: true
        )
        #expect(available.isPotentiallyAvailable)
        #expect(available.summary.contains("symbols appear available"))

        let missingTap = NativeTapAvailability(
            osVersionSupported: true,
            createProcessTapSymbolAvailable: false,
            createAggregateDeviceSymbolAvailable: true
        )
        #expect(missingTap.isPotentiallyAvailable == false)
        #expect(missingTap.summary.contains("AudioHardwareCreateProcessTap"))

        let missingOS = NativeTapAvailability(
            osVersionSupported: false,
            createProcessTapSymbolAvailable: true,
            createAggregateDeviceSymbolAvailable: true
        )
        #expect(missingOS.isPotentiallyAvailable == false)
        #expect(missingOS.summary.contains("macOS 14.4+"))
    }

    @Test func runtimeNativeTapProbeReturnsConsistentSummary() {
        let availability = SystemAudioRecorder.probeNativeTapAvailability()
        #expect(availability.summary.isEmpty == false)
        #expect(availability.isPotentiallyAvailable == (
            availability.osVersionSupported &&
            availability.createProcessTapSymbolAvailable &&
            availability.destroyProcessTapSymbolAvailable &&
            availability.createAggregateDeviceSymbolAvailable
        ))
    }

    @Test func nativeTapDiagnosticSummariesAreActionable() {
        let unavailable = NativeTapDiagnostic(
            availability: NativeTapAvailability(
                osVersionSupported: false,
                createProcessTapSymbolAvailable: true,
                destroyProcessTapSymbolAvailable: true,
                createAggregateDeviceSymbolAvailable: true
            ),
            createDestroyAttempted: false
        )
        #expect(unavailable.summary.contains("macOS 14.4+"))

        let notAttempted = NativeTapDiagnostic(
            availability: NativeTapAvailability(
                osVersionSupported: true,
                createProcessTapSymbolAvailable: true,
                destroyProcessTapSymbolAvailable: true,
                createAggregateDeviceSymbolAvailable: true
            ),
            createDestroyAttempted: false
        )
        #expect(notAttempted.summary.contains("STT_NATIVE_TAP_DIAGNOSTIC=1"))

        let failed = NativeTapDiagnostic(
            availability: NativeTapAvailability(
                osVersionSupported: true,
                createProcessTapSymbolAvailable: true,
                destroyProcessTapSymbolAvailable: true,
                createAggregateDeviceSymbolAvailable: true
            ),
            createDestroyAttempted: true,
            createDestroySucceeded: false,
            createOSStatus: -50,
            destroyOSStatus: nil
        )
        #expect(failed.summary.contains("create OSStatus: -50"))
    }
}
