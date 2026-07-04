import Foundation
import Testing
@testable import sttCore

@Suite("WAVMixer")
struct WAVMixerTests {

    @Test func parsesCanonicalMono16BitPCM() throws {
        let file = WAVPCMFile(sampleRate: 16_000, samples: [100, -100, 200])
        let parsed = try WAVPCMFile.parse(file.encodedData())

        #expect(parsed.sampleRate == 16_000)
        #expect(parsed.channels == 1)
        #expect(parsed.bitDepth == 16)
        #expect(parsed.samples == [100, -100, 200])
    }

    @Test func parsesStereo16BitPCMByDownmixingToMono() throws {
        var pcm = Data()
        appendInt16LE(100, to: &pcm)
        appendInt16LE(300, to: &pcm)
        appendInt16LE(-200, to: &pcm)
        appendInt16LE(100, to: &pcm)
        let data = WAVWriter.header(sampleRate: 44_100, channels: 2, bitDepth: 16, dataSize: UInt32(pcm.count)) + pcm

        let parsed = try WAVPCMFile.parse(data)

        #expect(parsed.sampleRate == 44_100)
        #expect(parsed.channels == 1)
        #expect(parsed.samples == [200, -50])
    }

    @Test func mixesWithClippingAndPadsShorterInput() throws {
        let lhs = WAVPCMFile(sampleRate: 16_000, samples: [10_000, 30_000, -30_000, 123])
        let rhs = WAVPCMFile(sampleRate: 16_000, samples: [10_000, 10_000, -10_000])

        let mixed = try WAVMixer.mix(lhs, rhs)

        #expect(mixed.sampleRate == 16_000)
        #expect(mixed.samples == [20_000, 32_767, -32_768, 123])
    }

    @Test func mixRejectsNonSixteenBitDepthConstructedDirectly() {
        let lhs = WAVPCMFile(sampleRate: 16_000, bitDepth: 8, samples: [1])
        let rhs = WAVPCMFile(sampleRate: 16_000, samples: [1])

        #expect(throws: WAVMixerError.self) {
            try WAVMixer.mix(lhs, rhs)
        }
    }

    @Test func mixResamplesDifferingSampleRatesToHigherRate() throws {
        let lhs = WAVPCMFile(sampleRate: 10, samples: [0, 100])
        let rhs = WAVPCMFile(sampleRate: 20, samples: [0, 0, 0, 0])

        let mixed = try WAVMixer.mix(lhs, rhs)

        #expect(mixed.sampleRate == 20)
        #expect(mixed.samples == [0, 50, 100, 100])
    }

    @Test func mixUpsamplesCommonMeetingSampleRateMismatch() throws {
        let lhs = WAVPCMFile(sampleRate: 44_100, samples: Array(repeating: Int16(1_000), count: 441))
        let rhs = WAVPCMFile(sampleRate: 48_000, samples: Array(repeating: Int16(1_000), count: 480))

        let mixed = try WAVMixer.mix(lhs, rhs)

        #expect(mixed.sampleRate == 48_000)
        #expect(mixed.samples.count == 480)
        #expect(mixed.samples.allSatisfy { $0 == 2_000 })
    }

    @Test func parseRejectsTooShortData() {
        #expect(throws: WAVMixerError.self) {
            try WAVPCMFile.parse(Data("not a wav".utf8))
        }
    }

    @Test func parseRejectsMissingRIFFMarker() {
        var data = WAVPCMFile(sampleRate: 8_000, samples: [1]).encodedData()
        data.replaceSubrange(0..<4, with: Data("NOPE".utf8))

        #expect(throws: WAVMixerError.self) {
            try WAVPCMFile.parse(data)
        }
    }

    @Test func parseRejectsMissingWAVEMarker() {
        var data = WAVPCMFile(sampleRate: 8_000, samples: [1]).encodedData()
        data.replaceSubrange(8..<12, with: Data("NOPE".utf8))

        #expect(throws: WAVMixerError.self) {
            try WAVPCMFile.parse(data)
        }
    }

    @Test func parseRejectsShortFmtChunk() {
        var data = Data()
        data.append(Data("RIFF".utf8))
        appendUInt32LE(36, to: &data)
        data.append(Data("WAVE".utf8))
        data.append(Data("fmt ".utf8))
        appendUInt32LE(8, to: &data)
        data.append(Data(repeating: 0, count: 24))

        #expect(throws: WAVMixerError.self) {
            try WAVPCMFile.parse(data)
        }
    }

    @Test func parseRejectsMissingFmtOrDataChunk() {
        var data = Data(repeating: 0, count: 44)
        data.replaceSubrange(0..<4, with: Data("RIFF".utf8))
        data.replaceSubrange(8..<12, with: Data("WAVE".utf8))

        #expect(throws: WAVMixerError.self) {
            try WAVPCMFile.parse(data)
        }
    }

    @Test func parseRejectsNonPCMAudioFormat() {
        var data = WAVPCMFile(sampleRate: 8_000, samples: [1]).encodedData()
        replaceUInt16LE(3, in: &data, at: 20)

        #expect(throws: WAVMixerError.self) {
            try WAVPCMFile.parse(data)
        }
    }

    @Test func parseRejectsNon16BitDepth() {
        var data = WAVPCMFile(sampleRate: 8_000, samples: [1]).encodedData()
        replaceUInt16LE(8, in: &data, at: 34)

        #expect(throws: WAVMixerError.self) {
            try WAVPCMFile.parse(data)
        }
    }

    @Test func parseRejectsZeroChannels() {
        var data = WAVPCMFile(sampleRate: 8_000, samples: [1]).encodedData()
        replaceUInt16LE(0, in: &data, at: 22)

        #expect(throws: WAVMixerError.self) {
            try WAVPCMFile.parse(data)
        }
    }

    @Test func parseRejectsZeroSampleRate() {
        var data = WAVPCMFile(sampleRate: 8_000, samples: [1]).encodedData()
        replaceUInt32LE(0, in: &data, at: 24)

        #expect(throws: WAVMixerError.self) {
            try WAVPCMFile.parse(data)
        }
    }

    @Test func parseRejectsMisalignedDataChunk() {
        let data = WAVWriter.header(sampleRate: 8_000, channels: 2, bitDepth: 16, dataSize: 2) + Data([0, 0])

        #expect(throws: WAVMixerError.self) {
            try WAVPCMFile.parse(data)
        }
    }

    @Test func mixFilesWritesCanonicalMonoWAV() throws {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let lhsURL = tmpDir.appendingPathComponent("lhs.wav")
        let rhsURL = tmpDir.appendingPathComponent("rhs.wav")
        let outURL = tmpDir.appendingPathComponent("mixed.wav")
        try WAVPCMFile(sampleRate: 8_000, samples: [1_000, 2_000]).encodedData().write(to: lhsURL)
        try WAVPCMFile(sampleRate: 8_000, samples: [3_000]).encodedData().write(to: rhsURL)

        let result = try WAVMixer.mixFiles(lhsURL, rhsURL, outputURL: outURL)
        let parsed = try WAVPCMFile.parse(Data(contentsOf: outURL))

        #expect(result.outputURL == outURL)
        #expect(result.durationSeconds == 0.00025)
        #expect(result.fileSizeBytes == UInt64(44 + 4))
        #expect(parsed.samples == [4_000, 2_000])
    }

    @Test func mixFilesSucceedsWithDifferingCommonSampleRates() throws {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let lhsURL = tmpDir.appendingPathComponent("mic.wav")
        let rhsURL = tmpDir.appendingPathComponent("system.wav")
        let outURL = tmpDir.appendingPathComponent("mixed.wav")
        try WAVPCMFile(sampleRate: 44_100, samples: Array(repeating: Int16(1_000), count: 441)).encodedData().write(to: lhsURL)
        try WAVPCMFile(sampleRate: 48_000, samples: Array(repeating: Int16(1_000), count: 480)).encodedData().write(to: rhsURL)

        let result = try WAVMixer.mixFiles(lhsURL, rhsURL, outputURL: outURL)
        let parsed = try WAVPCMFile.parse(Data(contentsOf: outURL))

        #expect(result.outputURL == outURL)
        #expect(result.durationSeconds == 0.01)
        #expect(parsed.sampleRate == 48_000)
        #expect(parsed.samples.count == 480)
        #expect(parsed.samples.allSatisfy { $0 == 2_000 })
    }

    @Test func driftWarningReturnsNilWhenDurationsAreClose() throws {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let lhsURL = tmpDir.appendingPathComponent("lhs.wav")
        let rhsURL = tmpDir.appendingPathComponent("rhs.wav")
        try WAVPCMFile(sampleRate: 10, samples: Array(repeating: 1, count: 10)).encodedData().write(to: lhsURL)
        try WAVPCMFile(sampleRate: 10, samples: Array(repeating: 1, count: 11)).encodedData().write(to: rhsURL)

        #expect(WAVMixer.driftWarning(lhsURL: lhsURL, rhsURL: rhsURL, thresholdSeconds: 0.25) == nil)
    }

    @Test func driftWarningReportsLargeDurationMismatch() throws {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let lhsURL = tmpDir.appendingPathComponent("mic.wav")
        let rhsURL = tmpDir.appendingPathComponent("system.wav")
        try WAVPCMFile(sampleRate: 10, samples: Array(repeating: 1, count: 10)).encodedData().write(to: lhsURL)
        try WAVPCMFile(sampleRate: 10, samples: Array(repeating: 1, count: 4)).encodedData().write(to: rhsURL)

        let warning = WAVMixer.driftWarning(lhsURL: lhsURL, rhsURL: rhsURL, thresholdSeconds: 0.25)

        #expect(warning?.contains("duration drift detected: 0.60s") == true)
        #expect(warning?.contains("mic: 1.00s") == true)
        #expect(warning?.contains("system: 0.40s") == true)
        #expect(warning?.contains("--separate-tracks") == true)
    }

    @Test func driftWarningReturnsNilForUnparseableInput() throws {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let lhsURL = tmpDir.appendingPathComponent("lhs.wav")
        let rhsURL = tmpDir.appendingPathComponent("rhs.wav")
        try Data("not a wav".utf8).write(to: lhsURL)
        try WAVPCMFile(sampleRate: 10, samples: Array(repeating: 1, count: 10)).encodedData().write(to: rhsURL)

        #expect(WAVMixer.driftWarning(lhsURL: lhsURL, rhsURL: rhsURL) == nil)
    }

    private func appendInt16LE(_ value: Int16, to data: inout Data) {
        let unsigned = UInt16(bitPattern: value)
        data.append(UInt8(unsigned & 0xff))
        data.append(UInt8((unsigned >> 8) & 0xff))
    }

    private func appendUInt32LE(_ value: UInt32, to data: inout Data) {
        data.append(UInt8(value & 0xff))
        data.append(UInt8((value >> 8) & 0xff))
        data.append(UInt8((value >> 16) & 0xff))
        data.append(UInt8((value >> 24) & 0xff))
    }

    private func replaceUInt16LE(_ value: UInt16, in data: inout Data, at offset: Int) {
        data[offset] = UInt8(value & 0xff)
        data[offset + 1] = UInt8((value >> 8) & 0xff)
    }

    private func replaceUInt32LE(_ value: UInt32, in data: inout Data, at offset: Int) {
        data[offset] = UInt8(value & 0xff)
        data[offset + 1] = UInt8((value >> 8) & 0xff)
        data[offset + 2] = UInt8((value >> 16) & 0xff)
        data[offset + 3] = UInt8((value >> 24) & 0xff)
    }
}
