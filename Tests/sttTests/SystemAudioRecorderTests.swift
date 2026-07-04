import Testing
@testable import sttCore

@Suite("SystemAudioRecorder")
struct SystemAudioRecorderTests {

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
            availability.createAggregateDeviceSymbolAvailable
        ))
    }
}
