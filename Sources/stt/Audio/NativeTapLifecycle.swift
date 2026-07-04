import Foundation
import CoreAudio

/// Low-level CoreAudio operations used by `NativeTapLifecycle`.
///
/// The defaults call the real CoreAudio APIs. Tests inject closures so lifecycle
/// ordering, cleanup, and error behavior can be verified without creating real
/// process taps, aggregate devices, IOProcs, or TCC prompts.
struct NativeTapCoreAudioOperations {
    var createProcessTap: (CATapDescription, inout AudioObjectID) -> OSStatus
    var destroyProcessTap: (AudioObjectID) -> OSStatus
    var createAggregateDevice: (CFDictionary, inout AudioObjectID) -> OSStatus
    var destroyAggregateDevice: (AudioObjectID) -> OSStatus
    var startAggregateDevice: (AudioObjectID) -> OSStatus
    var stopAggregateDevice: (AudioObjectID) -> OSStatus

    static var live: NativeTapCoreAudioOperations {
        NativeTapCoreAudioOperations(
            createProcessTap: { description, tapID in
                if #available(macOS 14.2, *) {
                    return AudioHardwareCreateProcessTap(description, &tapID)
                }
                return OSStatus(kAudioHardwareUnsupportedOperationError)
            },
            destroyProcessTap: { tapID in
                if #available(macOS 14.2, *) {
                    return AudioHardwareDestroyProcessTap(tapID)
                }
                return OSStatus(kAudioHardwareUnsupportedOperationError)
            },
            createAggregateDevice: { description, deviceID in AudioHardwareCreateAggregateDevice(description, &deviceID) },
            destroyAggregateDevice: { deviceID in AudioHardwareDestroyAggregateDevice(deviceID) },
            startAggregateDevice: { deviceID in AudioDeviceStart(deviceID, nil) },
            stopAggregateDevice: { deviceID in AudioDeviceStop(deviceID, nil) }
        )
    }
}

struct NativeTapCleanupResult: Equatable {
    var stopStatus: OSStatus?
    var destroyAggregateStatus: OSStatus?
    var destroyTapStatus: OSStatus?

    var succeeded: Bool {
        [stopStatus, destroyAggregateStatus, destroyTapStatus].allSatisfy { status in
            guard let status else { return true }
            return status == noErr
        }
    }
}

enum NativeTapLifecycleError: Error, LocalizedError, Equatable {
    case unsupported(String)
    case createProcessTapFailed(OSStatus)
    case createAggregateDeviceFailed(OSStatus, cleanup: NativeTapCleanupResult)
    case startAggregateDeviceFailed(OSStatus, cleanup: NativeTapCleanupResult)
    case stopFailed(NativeTapCleanupResult)

    var errorDescription: String? {
        switch self {
        case .unsupported(let reason):
            return "Native CoreAudio tap lifecycle unsupported: \(reason)"
        case .createProcessTapFailed(let status):
            return "AudioHardwareCreateProcessTap failed with OSStatus \(status)"
        case .createAggregateDeviceFailed(let status, let cleanup):
            return "AudioHardwareCreateAggregateDevice failed with OSStatus \(status); cleanup succeeded: \(cleanup.succeeded)"
        case .startAggregateDeviceFailed(let status, let cleanup):
            return "AudioDeviceStart failed with OSStatus \(status); cleanup succeeded: \(cleanup.succeeded)"
        case .stopFailed(let cleanup):
            return "Native CoreAudio tap lifecycle cleanup failed; cleanup result: \(cleanup)"
        }
    }
}

/// Owns a created CoreAudio process tap and destroys it at most once.
final class NativeProcessTapResource {
    let tapID: AudioObjectID
    let tapUUID: UUID
    private var destroyProcessTap: ((AudioObjectID) -> OSStatus)?

    var isValid: Bool { destroyProcessTap != nil }

    init(description: CATapDescription, operations: NativeTapCoreAudioOperations = .live) throws {
        var tapID = AudioObjectID(kAudioObjectUnknown)
        let status = operations.createProcessTap(description, &tapID)
        guard status == noErr else {
            throw NativeTapLifecycleError.createProcessTapFailed(status)
        }
        self.tapID = tapID
        self.tapUUID = description.uuid
        self.destroyProcessTap = operations.destroyProcessTap
    }

    @discardableResult
    func invalidate() -> OSStatus? {
        guard let destroyProcessTap else { return nil }
        self.destroyProcessTap = nil
        return destroyProcessTap(tapID)
    }

    deinit {
        _ = invalidate()
    }
}

/// Owns the first safe native CoreAudio tap lifecycle step:
///
/// 1. create a process tap;
/// 2. create a private aggregate device containing that tap;
/// 3. start/stop the aggregate device;
/// 4. destroy resources in reverse order.
///
/// This deliberately does not create an IOProc or stream audio into `record
/// --mode system` yet. It is an internal building block for the next Milestone 2
/// step and is unused by default outside opt-in diagnostics/tests.
final class NativeTapLifecycle {
    private let operations: NativeTapCoreAudioOperations
    private let availabilityProvider: () -> NativeTapAvailability
    private let tapDescription: CATapDescription
    private let aggregateName: String
    private let aggregateUID: String

    private var tapResource: NativeProcessTapResource?
    private var aggregateDeviceID: AudioObjectID?
    private var started = false

    var tapID: AudioObjectID? { tapResource?.tapID }
    var tapUUID: UUID? { tapResource?.tapUUID }
    var aggregateID: AudioObjectID? { aggregateDeviceID }
    var isStarted: Bool { started }

    init(tapDescription: CATapDescription,
         aggregateName: String = "stt native tap aggregate",
         aggregateUID: String = "com.hashicorp.stt.native-tap.aggregate.\(UUID().uuidString)",
         operations: NativeTapCoreAudioOperations = .live,
         availabilityProvider: @escaping () -> NativeTapAvailability = SystemAudioRecorder.probeNativeTapAvailability) {
        self.tapDescription = tapDescription
        self.aggregateName = aggregateName
        self.aggregateUID = aggregateUID
        self.operations = operations
        self.availabilityProvider = availabilityProvider
    }

    static func defaultTapDescription(name: String = "stt native tap") -> CATapDescription {
        let description = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
        description.name = name
        description.isPrivate = true
        description.isMixdown = true
        description.isMono = false
        return description
    }

    static func aggregateDescription(name: String,
                                     uid: String,
                                     tapUUID: UUID,
                                     privateAggregate: Bool = true,
                                     autoStartTap: Bool = false) -> CFDictionary {
        let tap: [String: Any] = [
            "uid": tapUUID.uuidString,
            "drift": true
        ]
        let aggregate: [String: Any] = [
            "name": name,
            "uid": uid,
            "private": privateAggregate,
            "tapautostart": autoStartTap,
            "taps": [tap]
        ]
        return aggregate as CFDictionary
    }

    func start() throws {
        let availability = availabilityProvider()
        guard availability.isPotentiallyAvailable else {
            throw NativeTapLifecycleError.unsupported(availability.summary)
        }

        if #available(macOS 14.2, *) {
            let tapResource = try NativeProcessTapResource(description: tapDescription, operations: operations)
            self.tapResource = tapResource

            var aggregateDeviceID = AudioObjectID(kAudioObjectUnknown)
            let aggregateDescription = Self.aggregateDescription(
                name: aggregateName,
                uid: aggregateUID,
                tapUUID: tapResource.tapUUID
            )
            let aggregateStatus = operations.createAggregateDevice(aggregateDescription, &aggregateDeviceID)
            guard aggregateStatus == noErr else {
                let cleanup = cleanupAfterFailedStart(includeStop: false)
                throw NativeTapLifecycleError.createAggregateDeviceFailed(aggregateStatus, cleanup: cleanup)
            }
            self.aggregateDeviceID = aggregateDeviceID

            let startStatus = operations.startAggregateDevice(aggregateDeviceID)
            guard startStatus == noErr else {
                let cleanup = cleanupAfterFailedStart(includeStop: false)
                throw NativeTapLifecycleError.startAggregateDeviceFailed(startStatus, cleanup: cleanup)
            }

            started = true
            return
        }

        throw NativeTapLifecycleError.unsupported("CoreAudio process tap construction requires a supported macOS runtime.")
    }

    @discardableResult
    func stop() throws -> NativeTapCleanupResult {
        let cleanup = cleanupAfterFailedStart(includeStop: started)
        if cleanup.succeeded { return cleanup }
        throw NativeTapLifecycleError.stopFailed(cleanup)
    }

    private func cleanupAfterFailedStart(includeStop: Bool) -> NativeTapCleanupResult {
        var result = NativeTapCleanupResult()

        if includeStop, let aggregateDeviceID {
            result.stopStatus = operations.stopAggregateDevice(aggregateDeviceID)
        }
        started = false

        if let aggregateDeviceID {
            result.destroyAggregateStatus = operations.destroyAggregateDevice(aggregateDeviceID)
            self.aggregateDeviceID = nil
        }

        if let tapResource {
            result.destroyTapStatus = tapResource.invalidate()
            self.tapResource = nil
        }

        return result
    }

    deinit {
        _ = cleanupAfterFailedStart(includeStop: started)
    }
}
