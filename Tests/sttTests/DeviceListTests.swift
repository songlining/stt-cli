import Testing
@testable import sttCore

@Suite("DeviceList")
struct DeviceListTests {

    @Test func exactMatchWinsOverEarlierSubstringMatch() throws {
        let devices = [
            device(id: 1, name: "External BlackHole Monitor"),
            device(id: 2, name: "BlackHole 2ch")
        ]

        let selected = try DeviceList.selectInputDevice(named: "BlackHole 2ch", from: devices)

        #expect(selected.id == 2)
    }

    @Test func substringMatchIsCaseInsensitive() throws {
        let devices = [
            device(id: 1, name: "MacBook Pro Microphone"),
            device(id: 2, name: "BlackHole 2ch")
        ]

        let selected = try DeviceList.selectInputDevice(named: "blackhole", from: devices)

        #expect(selected.id == 2)
    }

    @Test func ambiguousSubstringMatchUsesFirstDeviceOrder() throws {
        let devices = [
            device(id: 1, name: "BlackHole 2ch"),
            device(id: 2, name: "BlackHole 16ch")
        ]

        let selected = try DeviceList.selectInputDevice(named: "BlackHole", from: devices)

        #expect(selected.id == 1)
    }

    @Test func noMatchThrowsWithRequestedName() {
        do {
            _ = try DeviceList.selectInputDevice(named: "Missing Device", from: [device(id: 1, name: "Built-in Mic")])
            Issue.record("Expected missing device error")
        } catch DeviceListError.deviceNotFound(let name) {
            #expect(name == "Missing Device")
        } catch {
            Issue.record("Unexpected error: \(error)")
        }
    }

    @Test func emptyDeviceListThrowsNotFound() {
        do {
            _ = try DeviceList.selectInputDevice(named: "Anything", from: [])
            Issue.record("Expected missing device error")
        } catch DeviceListError.deviceNotFound(let name) {
            #expect(name == "Anything")
        } catch {
            Issue.record("Unexpected error: \(error)")
        }
    }

    private func device(id: UInt32, name: String) -> AudioDeviceInfo {
        AudioDeviceInfo(id: id, name: name, uid: "uid-\(id)", inputChannelCount: 2, isDefaultInput: false)
    }
}
