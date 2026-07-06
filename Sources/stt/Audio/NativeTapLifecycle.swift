import Foundation
import AVFoundation
import AVFAudio
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
    var createIOProc: (_ deviceID: AudioObjectID, _ queue: DispatchQueue?, _ block: @escaping AudioDeviceIOBlock, _ ioProcID: inout AudioDeviceIOProcID?) -> OSStatus
    var destroyIOProc: (AudioObjectID, AudioDeviceIOProcID) -> OSStatus
    var startAggregateDevice: (AudioObjectID, AudioDeviceIOProcID?) -> OSStatus
    var stopAggregateDevice: (AudioObjectID, AudioDeviceIOProcID?) -> OSStatus

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
            createIOProc: { deviceID, queue, block, ioProcID in
                AudioDeviceCreateIOProcIDWithBlock(&ioProcID, deviceID, queue, block)
            },
            destroyIOProc: { deviceID, ioProcID in AudioDeviceDestroyIOProcID(deviceID, ioProcID) },
            startAggregateDevice: { deviceID, ioProcID in AudioDeviceStart(deviceID, ioProcID) },
            stopAggregateDevice: { deviceID, ioProcID in AudioDeviceStop(deviceID, ioProcID) }
        )
    }
}

struct NativeTapCleanupResult: Equatable {
    var stopStatus: OSStatus?
    var destroyIOProcStatus: OSStatus?
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
    case createIOProcFailed(OSStatus, cleanup: NativeTapCleanupResult)
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
        case .createIOProcFailed(let status, let cleanup):
            return "AudioDeviceCreateIOProcIDWithBlock failed with OSStatus \(status); cleanup succeeded: \(cleanup.succeeded)"
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

/// Thread-safe bridge from CoreAudio IOProc buffers to `StreamingWAVWriter`.
///
/// The IOProc path copies/normalizes input buffers into Int16 interleaved PCM
/// and appends on a dedicated serial queue. `finish()` drains that queue before
/// patching the WAV header, so late queued writes cannot race header finalization.
struct NativeTapPayloadSnapshot: Equatable {
    let nonZeroSamples: Int
    let peakAbsSample: Int16
}

final class NativeTapWAVBridge: @unchecked Sendable {
    private let writer: StreamingWAVWriter
    private let queue = DispatchQueue(label: "com.larrysong.stt.native-tap.wav-writer")
    private let lock = NSLock()
    private var acceptingBuffers = true
    private var appendError: Error?
    private var nonZeroSampleCount = 0
    private var peakAbsSample: Int16 = 0

    let sampleRate: UInt32
    let channels: UInt16

    init(outputURL: URL, sampleRate: UInt32 = 48_000, channels: UInt16 = 2) throws {
        self.sampleRate = sampleRate
        self.channels = channels
        self.writer = try StreamingWAVWriter(url: outputURL, sampleRate: sampleRate, channels: channels, bitDepth: 16)
    }

    func consume(_ inputData: UnsafePointer<AudioBufferList>?) {
        guard let pcm = Self.int16PCM(from: inputData, targetChannels: Int(channels)), !pcm.isEmpty else { return }

        lock.lock()
        let shouldAccept = acceptingBuffers
        lock.unlock()
        guard shouldAccept else { return }

        queue.async { [weak self] in
            guard let self else { return }
            self.lock.lock()
            let shouldWrite = self.acceptingBuffers
            self.lock.unlock()
            guard shouldWrite else { return }
            self.updatePayloadStats(with: pcm)
            do {
                try self.writer.append(pcm)
            } catch {
                self.lock.lock()
                if self.appendError == nil { self.appendError = error }
                self.lock.unlock()
            }
        }
    }

    @discardableResult
    func finish() throws -> URL {
        lock.lock()
        acceptingBuffers = false
        lock.unlock()

        queue.sync {}

        lock.lock()
        let capturedError = appendError
        lock.unlock()
        if let capturedError { throw capturedError }
        return try writer.finish()
    }

    var durationSeconds: Double { writer.durationSeconds }

    func payloadSnapshot() -> NativeTapPayloadSnapshot {
        queue.sync {}
        lock.lock()
        defer { lock.unlock() }
        return NativeTapPayloadSnapshot(nonZeroSamples: nonZeroSampleCount, peakAbsSample: peakAbsSample)
    }

    private func updatePayloadStats(with pcm: Data) {
        // Track what CoreAudio delivered even if a later file append fails;
        // diagnostics should distinguish payload/TCC problems from writer errors.
        let stats = Self.payloadStats(for: pcm)
        lock.lock()
        nonZeroSampleCount += stats.nonZeroSamples
        peakAbsSample = max(peakAbsSample, stats.peakAbsSample)
        lock.unlock()
    }

    static func payloadStats(for pcm: Data) -> NativeTapPayloadSnapshot {
        // NativeTapWAVBridge only passes complete Int16 PCM buffers here.
        var nonZero = 0
        var peak: Int16 = 0
        pcm.withUnsafeBytes { rawBuffer in
            for sample in rawBuffer.bindMemory(to: Int16.self) {
                let value = Int16(littleEndian: sample)
                if value != 0 { nonZero += 1 }
                peak = max(peak, absClamped(value))
            }
        }
        return NativeTapPayloadSnapshot(nonZeroSamples: nonZero, peakAbsSample: peak)
    }

    static func int16PCM(from inputData: UnsafePointer<AudioBufferList>?, targetChannels: Int = 2) -> Data? {
        guard let inputData, targetChannels > 0 else { return nil }
        let bufferCount = Int(inputData.pointee.mNumberBuffers)
        guard bufferCount > 0 else { return nil }

        return withUnsafePointer(to: inputData.pointee.mBuffers) { firstBuffer in
            let buffers = UnsafeBufferPointer(start: firstBuffer, count: bufferCount)

            if bufferCount == 1 {
                let buffer = buffers[0]
                guard let data = buffer.mData, buffer.mDataByteSize > 0 else { return nil }
                let sampleCount = Int(buffer.mDataByteSize) / MemoryLayout<Float32>.size
                guard sampleCount > 0 else { return nil }
                let samples = data.assumingMemoryBound(to: Float32.self)
                var out = Data(capacity: sampleCount * MemoryLayout<Int16>.size)
                for index in 0..<sampleCount {
                    out.append(Self.int16Sample(from: samples[index]))
                }
                return out
            }

            let usableBuffers = min(bufferCount, targetChannels)
            guard usableBuffers > 0 else { return nil }
            let frameCount = (0..<usableBuffers)
                .compactMap { index -> Int? in
                    let buffer = buffers[index]
                    guard buffer.mData != nil, buffer.mDataByteSize > 0 else { return nil }
                    return Int(buffer.mDataByteSize) / MemoryLayout<Float32>.size
                }
                .min() ?? 0
            guard frameCount > 0 else { return nil }

            var out = Data(capacity: frameCount * targetChannels * MemoryLayout<Int16>.size)
            for frame in 0..<frameCount {
                for channel in 0..<targetChannels {
                    let sourceChannel = min(channel, usableBuffers - 1)
                    let buffer = buffers[sourceChannel]
                    let samples = buffer.mData!.assumingMemoryBound(to: Float32.self)
                    out.append(Self.int16Sample(from: samples[frame]))
                }
            }
            return out
        }
    }

    private static func int16Sample(from sample: Float32) -> Int16 {
        let clamped = max(-1.0, min(1.0, sample))
        let scaled = clamped < 0 ? clamped * 32768.0 : clamped * 32767.0
        return Int16(scaled).littleEndian
    }

    private static func absClamped(_ sample: Int16) -> Int16 {
        if sample == Int16.min { return Int16.max }
        return abs(sample)
    }
}

private extension Data {
    mutating func append(_ value: Int16) {
        var mutable = value
        Swift.withUnsafeBytes(of: &mutable) { append(contentsOf: $0) }
    }
}

/// Owns the native CoreAudio process-tap lifecycle:
///
/// 1. create a process tap;
/// 2. create a private aggregate device containing that tap;
/// 3. (when an `audioBridge` is provided) create an IOProc that streams tap
///    buffers into the bridge's WAV writer;
/// 4. start/stop the aggregate device;
/// 5. destroy resources in reverse order.
///
/// When constructed with an `audioBridge`, this streams real system audio into
/// `record --mode system`/`--mode meeting`. Without a bridge it performs only
/// the tap/aggregate create+destroy lifecycle, used by opt-in diagnostics/tests.
final class NativeTapLifecycle {
    private let operations: NativeTapCoreAudioOperations
    private let availabilityProvider: () -> NativeTapAvailability
    private let tapDescription: CATapDescription
    private let aggregateName: String
    private let aggregateUID: String
    private let audioBridge: NativeTapWAVBridge?
    private let ioQueue = DispatchQueue(label: "com.larrysong.stt.native-tap.ioproc")

    private var tapResource: NativeProcessTapResource?
    private var aggregateDeviceID: AudioObjectID?
    private var ioProcID: AudioDeviceIOProcID?
    private var started = false

    var tapID: AudioObjectID? { tapResource?.tapID }
    var tapUUID: UUID? { tapResource?.tapUUID }
    var aggregateID: AudioObjectID? { aggregateDeviceID }
    var hasIOProc: Bool { ioProcID != nil }
    var isStarted: Bool { started }

    init(tapDescription: CATapDescription,
         aggregateName: String = "stt native tap aggregate",
         aggregateUID: String = "com.larrysong.stt.native-tap.aggregate.\(UUID().uuidString)",
         audioBridge: NativeTapWAVBridge? = nil,
         operations: NativeTapCoreAudioOperations = .live,
         availabilityProvider: @escaping () -> NativeTapAvailability = SystemAudioRecorder.probeNativeTapAvailability) {
        self.tapDescription = tapDescription
        self.aggregateName = aggregateName
        self.aggregateUID = aggregateUID
        self.audioBridge = audioBridge
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

            if let audioBridge {
                var ioProcID: AudioDeviceIOProcID?
                let block: AudioDeviceIOBlock = { _, inputData, _, _, _ in
                    audioBridge.consume(inputData)
                }
                let ioProcStatus = operations.createIOProc(aggregateDeviceID, ioQueue, block, &ioProcID)
                guard ioProcStatus == noErr, let ioProcID else {
                    let cleanup = cleanupAfterFailedStart(includeStop: false)
                    throw NativeTapLifecycleError.createIOProcFailed(ioProcStatus, cleanup: cleanup)
                }
                self.ioProcID = ioProcID
            }

            let startStatus = operations.startAggregateDevice(aggregateDeviceID, ioProcID)
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
            result.stopStatus = operations.stopAggregateDevice(aggregateDeviceID, ioProcID)
        }
        started = false

        if let aggregateDeviceID, let ioProcID {
            result.destroyIOProcStatus = operations.destroyIOProc(aggregateDeviceID, ioProcID)
            self.ioProcID = nil
        }

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
