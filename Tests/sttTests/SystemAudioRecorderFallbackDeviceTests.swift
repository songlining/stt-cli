import Testing
@testable import sttCore

@Suite("SystemAudioRecorder fallback device selection")
struct SystemAudioRecorderFallbackDeviceTests {

    @Test func explicitNameSelectsMatchingDevice() throws {
        let devices = [
            device(id: 1, name: "Built-in Mic"),
            device(id: 2, name: "BlackHole 2ch")
        ]

        let selected = try SystemAudioRecorder.selectFallbackDevice(named: "BlackHole", from: devices)

        #expect(selected.id == 2)
    }

    @Test func explicitMissingNamePreservesDeviceListError() {
        do {
            _ = try SystemAudioRecorder.selectFallbackDevice(named: "Missing Device", from: [device(id: 1, name: "Built-in Mic")])
            Issue.record("Expected missing explicit device error")
        } catch DeviceListError.deviceNotFound(let name) {
            #expect(name == "Missing Device")
        } catch {
            Issue.record("Unexpected error: \(error)")
        }
    }

    @Test func defaultSelectionPrefersFirstCandidateName() throws {
        let devices = [
            device(id: 1, name: "Aggregate Device"),
            device(id: 2, name: "BlackHole 2ch")
        ]

        let selected = try SystemAudioRecorder.selectFallbackDevice(named: nil, from: devices)

        #expect(selected.id == 2)
    }

    @Test func defaultSelectionFallsThroughToLaterCandidate() throws {
        let devices = [
            device(id: 1, name: "Built-in Mic"),
            device(id: 2, name: "Aggregate Device")
        ]

        let selected = try SystemAudioRecorder.selectFallbackDevice(named: nil, from: devices)

        #expect(selected.id == 2)
    }

    @Test func defaultSelectionThrowsWhenNoCandidateMatches() {
        do {
            _ = try SystemAudioRecorder.selectFallbackDevice(named: nil, from: [device(id: 1, name: "Built-in Mic")])
            Issue.record("Expected no fallback device error")
        } catch SystemAudioRecorderError.noFallbackDeviceConfigured {
            // expected
        } catch {
            Issue.record("Unexpected error: \(error)")
        }
    }

    @Test func emptyDeviceListThrowsNoFallbackConfigured() {
        do {
            _ = try SystemAudioRecorder.selectFallbackDevice(named: nil, from: [])
            Issue.record("Expected no fallback device error")
        } catch SystemAudioRecorderError.noFallbackDeviceConfigured {
            // expected
        } catch {
            Issue.record("Unexpected error: \(error)")
        }
    }

    private func device(id: UInt32, name: String) -> AudioDeviceInfo {
        AudioDeviceInfo(id: id, name: name, uid: "uid-\(id)", inputChannelCount: 2, isDefaultInput: false)
    }
}
