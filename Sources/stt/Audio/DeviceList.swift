import Foundation
import CoreAudio
#if canImport(AudioToolbox)
import AudioToolbox
#endif

/// Describes a CoreAudio device relevant for input (recording) selection.
public struct AudioDeviceInfo: Equatable, Codable {
    public let id: UInt32
    public let name: String
    public let uid: String?
    public let inputChannelCount: Int
    public let isDefaultInput: Bool

    public init(id: UInt32, name: String, uid: String?, inputChannelCount: Int, isDefaultInput: Bool) {
        self.id = id
        self.name = name
        self.uid = uid
        self.inputChannelCount = inputChannelCount
        self.isDefaultInput = isDefaultInput
    }
}

public enum DeviceListError: Error, LocalizedError {
    case coreAudioError(OSStatus, String)
    case deviceNotFound(String)

    public var errorDescription: String? {
        switch self {
        case .coreAudioError(let status, let context):
            return "CoreAudio error \(status) while \(context)"
        case .deviceNotFound(let name):
            return "No input device found matching \"\(name)\""
        }
    }
}

/// CoreAudio-backed enumeration of audio devices, used by `stt devices` and
/// for resolving `--input-device <name>` to a concrete device ID.
public enum DeviceList {

    /// Lists all audio devices on the system that expose at least one input
    /// channel, annotated with whether each is the current default input.
    public static func inputDevices() throws -> [AudioDeviceInfo] {
        let deviceIDs = try allDeviceIDs()
        let defaultInputID = try? defaultInputDeviceID()

        var results: [AudioDeviceInfo] = []
        for deviceID in deviceIDs {
            let inputChannels = inputChannelCount(for: deviceID)
            guard inputChannels > 0 else { continue }
            let name = deviceName(for: deviceID) ?? "Unknown Device \(deviceID)"
            let uid = deviceUID(for: deviceID)
            results.append(AudioDeviceInfo(
                id: deviceID,
                name: name,
                uid: uid,
                inputChannelCount: inputChannels,
                isDefaultInput: deviceID == defaultInputID
            ))
        }
        return results
    }

    /// Resolves a device by exact or case-insensitive substring name match.
    public static func resolveInputDevice(named name: String) throws -> AudioDeviceInfo {
        try selectInputDevice(named: name, from: inputDevices())
    }

    /// Pure selection helper used by `resolveInputDevice(named:)` after the
    /// CoreAudio-backed device list has been collected. Kept separate so the
    /// matching behavior is deterministic and unit-testable without hardware.
    public static func selectInputDevice(named name: String, from devices: [AudioDeviceInfo]) throws -> AudioDeviceInfo {
        if let exact = devices.first(where: { $0.name == name }) {
            return exact
        }
        let lowered = name.lowercased()
        if let partial = devices.first(where: { $0.name.lowercased().contains(lowered) }) {
            return partial
        }
        throw DeviceListError.deviceNotFound(name)
    }

    public static func defaultInputDevice() throws -> AudioDeviceInfo? {
        guard let id = try? defaultInputDeviceID() else { return nil }
        return try inputDevices().first(where: { $0.id == id })
    }

    // MARK: - Low-level CoreAudio helpers

    private static func allDeviceIDs() throws -> [UInt32] {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDevices,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )

        var dataSize: UInt32 = 0
        var status = AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &dataSize)
        guard status == noErr else {
            throw DeviceListError.coreAudioError(status, "getting device list size")
        }

        let count = Int(dataSize) / MemoryLayout<AudioDeviceID>.size
        var deviceIDs = [AudioDeviceID](repeating: 0, count: count)
        status = AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &dataSize, &deviceIDs)
        guard status == noErr else {
            throw DeviceListError.coreAudioError(status, "getting device list")
        }
        return deviceIDs
    }

    private static func defaultInputDeviceID() throws -> UInt32 {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultInputDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var deviceID: AudioDeviceID = 0
        var dataSize = UInt32(MemoryLayout<AudioDeviceID>.size)
        let status = AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &dataSize, &deviceID)
        guard status == noErr else {
            throw DeviceListError.coreAudioError(status, "getting default input device")
        }
        return deviceID
    }

    private static func deviceName(for deviceID: UInt32) -> String? {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioObjectPropertyName,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var name: CFString = "" as CFString
        var dataSize = UInt32(MemoryLayout<CFString>.size)
        let status = withUnsafeMutablePointer(to: &name) { ptr -> OSStatus in
            AudioObjectGetPropertyData(deviceID, &address, 0, nil, &dataSize, ptr)
        }
        guard status == noErr else { return nil }
        return name as String
    }

    private static func deviceUID(for deviceID: UInt32) -> String? {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyDeviceUID,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var uid: CFString = "" as CFString
        var dataSize = UInt32(MemoryLayout<CFString>.size)
        let status = withUnsafeMutablePointer(to: &uid) { ptr -> OSStatus in
            AudioObjectGetPropertyData(deviceID, &address, 0, nil, &dataSize, ptr)
        }
        guard status == noErr else { return nil }
        return uid as String
    }

    /// Number of input channels exposed by a device, derived from its
    /// input-scope stream configuration.
    private static func inputChannelCount(for deviceID: UInt32) -> Int {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyStreamConfiguration,
            mScope: kAudioDevicePropertyScopeInput,
            mElement: kAudioObjectPropertyElementMain
        )

        var dataSize: UInt32 = 0
        var status = AudioObjectGetPropertyDataSize(deviceID, &address, 0, nil, &dataSize)
        guard status == noErr, dataSize > 0 else { return 0 }

        let bufferListPointer = UnsafeMutableRawPointer.allocate(byteCount: Int(dataSize), alignment: MemoryLayout<AudioBufferList>.alignment)
        defer { bufferListPointer.deallocate() }

        status = AudioObjectGetPropertyData(deviceID, &address, 0, nil, &dataSize, bufferListPointer)
        guard status == noErr else { return 0 }

        let bufferList = bufferListPointer.assumingMemoryBound(to: AudioBufferList.self)
        let buffers = UnsafeMutableAudioBufferListPointer(bufferList)
        var channels = 0
        for buffer in buffers {
            channels += Int(buffer.mNumberChannels)
        }
        return channels
    }
}
