import Foundation
import CoreAudio
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

    @Test func nativeTapAggregateDescriptionContainsPrivateTap() throws {
        let tapUUID = UUID(uuidString: "11111111-2222-3333-4444-555555555555")!
        let description = NativeTapLifecycle.aggregateDescription(
            name: "stt test aggregate",
            uid: "com.hashicorp.stt.test.aggregate",
            tapUUID: tapUUID
        ) as NSDictionary

        #expect(description["name"] as? String == "stt test aggregate")
        #expect(description["uid"] as? String == "com.hashicorp.stt.test.aggregate")
        #expect(description["private"] as? Bool == true)
        #expect(description["tapautostart"] as? Bool == false)

        let taps = try #require(description["taps"] as? [[String: Any]])
        #expect(taps.count == 1)
        #expect(taps[0]["uid"] as? String == tapUUID.uuidString)
        #expect(taps[0]["drift"] as? Bool == true)
    }

    @Test func nativeProcessTapResourceInvalidatesAtMostOnce() throws {
        var destroyed: [AudioObjectID] = []
        let operations = NativeTapCoreAudioOperations.mock(
            createProcessTap: { _, tapID in
                tapID = 42
                return noErr
            },
            destroyProcessTap: { tapID in
                destroyed.append(tapID)
                return noErr
            }
        )

        let resource = try NativeProcessTapResource(
            description: NativeTapLifecycle.defaultTapDescription(name: "stt test tap"),
            operations: operations
        )

        #expect(resource.tapID == 42)
        #expect(resource.isValid)
        #expect(resource.invalidate() == noErr)
        #expect(resource.invalidate() == nil)
        #expect(destroyed == [42])
        #expect(resource.isValid == false)
    }

    @Test func nativeTapLifecycleStartsAndStopsInOrderWithMocks() throws {
        var events: [String] = []
        let operations = NativeTapCoreAudioOperations.mock(
            createProcessTap: { _, tapID in
                events.append("createTap")
                tapID = 101
                return noErr
            },
            destroyProcessTap: { tapID in
                events.append("destroyTap:\(tapID)")
                return noErr
            },
            createAggregateDevice: { description, deviceID in
                let aggregate = description as NSDictionary
                let taps = aggregate["taps"] as? [[String: Any]]
                events.append("createAggregate:\(taps?.count ?? 0)")
                deviceID = 202
                return noErr
            },
            destroyAggregateDevice: { deviceID in
                events.append("destroyAggregate:\(deviceID)")
                return noErr
            },
            startAggregateDevice: { deviceID in
                events.append("startAggregate:\(deviceID)")
                return noErr
            },
            stopAggregateDevice: { deviceID in
                events.append("stopAggregate:\(deviceID)")
                return noErr
            }
        )

        let lifecycle = NativeTapLifecycle(
            tapDescription: NativeTapLifecycle.defaultTapDescription(name: "stt test tap"),
            aggregateName: "stt test aggregate",
            aggregateUID: "com.hashicorp.stt.test.aggregate",
            operations: operations,
            availabilityProvider: Self.availableNativeTap
        )

        try lifecycle.start()
        #expect(lifecycle.isStarted)
        #expect(lifecycle.tapID == 101)
        #expect(lifecycle.aggregateID == 202)
        let cleanup = try lifecycle.stop()
        #expect(cleanup.succeeded)
        #expect(lifecycle.isStarted == false)
        #expect(lifecycle.tapID == nil)
        #expect(lifecycle.aggregateID == nil)
        #expect(events == [
            "createTap",
            "createAggregate:1",
            "startAggregate:202",
            "stopAggregate:202",
            "destroyAggregate:202",
            "destroyTap:101"
        ])
    }

    @Test func nativeTapLifecycleCleansUpTapWhenAggregateCreationFails() throws {
        var events: [String] = []
        let operations = NativeTapCoreAudioOperations.mock(
            createProcessTap: { _, tapID in
                events.append("createTap")
                tapID = 303
                return noErr
            },
            destroyProcessTap: { tapID in
                events.append("destroyTap:\(tapID)")
                return noErr
            },
            createAggregateDevice: { _, _ in
                events.append("createAggregate")
                return -50
            }
        )

        let lifecycle = NativeTapLifecycle(
            tapDescription: NativeTapLifecycle.defaultTapDescription(name: "stt test tap"),
            operations: operations,
            availabilityProvider: Self.availableNativeTap
        )

        do {
            try lifecycle.start()
            Issue.record("Expected aggregate creation failure")
        } catch let error as NativeTapLifecycleError {
            guard case .createAggregateDeviceFailed(let status, let cleanup) = error else {
                Issue.record("Unexpected error: \(error)")
                return
            }
            #expect(status == -50)
            #expect(cleanup.succeeded)
        }
        #expect(lifecycle.isStarted == false)
        #expect(lifecycle.tapID == nil)
        #expect(lifecycle.aggregateID == nil)
        #expect(events == ["createTap", "createAggregate", "destroyTap:303"])
    }

    @Test func nativeTapLifecycleDoesNotCallCoreAudioWhenUnavailable() throws {
        var coreAudioCallCount = 0
        let operations = NativeTapCoreAudioOperations.mock(
            createProcessTap: { _, _ in
                coreAudioCallCount += 1
                return noErr
            }
        )
        let lifecycle = NativeTapLifecycle(
            tapDescription: NativeTapLifecycle.defaultTapDescription(name: "stt test tap"),
            operations: operations,
            availabilityProvider: {
                NativeTapAvailability(
                    osVersionSupported: false,
                    createProcessTapSymbolAvailable: true,
                    destroyProcessTapSymbolAvailable: true,
                    createAggregateDeviceSymbolAvailable: true
                )
            }
        )

        do {
            try lifecycle.start()
            Issue.record("Expected unsupported native tap lifecycle")
        } catch let error as NativeTapLifecycleError {
            guard case .unsupported(let reason) = error else {
                Issue.record("Unexpected error: \(error)")
                return
            }
            #expect(reason.contains("macOS 14.4+"))
        }
        #expect(coreAudioCallCount == 0)
    }

    private static func availableNativeTap() -> NativeTapAvailability {
        NativeTapAvailability(
            osVersionSupported: true,
            createProcessTapSymbolAvailable: true,
            destroyProcessTapSymbolAvailable: true,
            createAggregateDeviceSymbolAvailable: true
        )
    }
}

private extension NativeTapCoreAudioOperations {
    static func mock(
        createProcessTap: @escaping (CATapDescription, inout AudioObjectID) -> OSStatus = { _, _ in noErr },
        destroyProcessTap: @escaping (AudioObjectID) -> OSStatus = { _ in noErr },
        createAggregateDevice: @escaping (CFDictionary, inout AudioObjectID) -> OSStatus = { _, _ in noErr },
        destroyAggregateDevice: @escaping (AudioObjectID) -> OSStatus = { _ in noErr },
        startAggregateDevice: @escaping (AudioObjectID) -> OSStatus = { _ in noErr },
        stopAggregateDevice: @escaping (AudioObjectID) -> OSStatus = { _ in noErr }
    ) -> NativeTapCoreAudioOperations {
        NativeTapCoreAudioOperations(
            createProcessTap: createProcessTap,
            destroyProcessTap: destroyProcessTap,
            createAggregateDevice: createAggregateDevice,
            destroyAggregateDevice: destroyAggregateDevice,
            startAggregateDevice: startAggregateDevice,
            stopAggregateDevice: stopAggregateDevice
        )
    }
}
