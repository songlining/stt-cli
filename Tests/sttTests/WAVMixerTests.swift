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

    @Test func rejectsSampleRateMismatch() {
        let lhs = WAVPCMFile(sampleRate: 16_000, samples: [1])
        let rhs = WAVPCMFile(sampleRate: 44_100, samples: [1])

        #expect(throws: WAVMixerError.self) {
            try WAVMixer.mix(lhs, rhs)
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

    private func appendInt16LE(_ value: Int16, to data: inout Data) {
        let unsigned = UInt16(bitPattern: value)
        data.append(UInt8(unsigned & 0xff))
        data.append(UInt8((unsigned >> 8) & 0xff))
    }
}
