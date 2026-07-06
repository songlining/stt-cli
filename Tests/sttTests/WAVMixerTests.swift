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

        // Existing raw-summation assertion, kept intentionally on `.raw` mode
        // since `.balanced` is now the default and would rescale these levels.
        let mixed = try WAVMixer.mix(lhs, rhs, mode: .raw)

        #expect(mixed.sampleRate == 16_000)
        #expect(mixed.samples == [20_000, 32_767, -32_768, 123])
    }

    @Test func rawModeMatchesLegacyRawSummationExactly() throws {
        // Reproduces the pre-balancing behavior byte-for-byte: raw PCM
        // summation with only Int16 clamping, no loudness normalization.
        let lhs = WAVPCMFile(sampleRate: 16_000, samples: [1_000, -2_000, 30_000])
        let rhs = WAVPCMFile(sampleRate: 16_000, samples: [500, 500, 500])

        let mixed = try WAVMixer.mix(lhs, rhs, mode: .raw)

        #expect(mixed.samples == [1_500, -1_500, 32_767])
    }

    @Test func balancedModeLiftsQuietTrackToParityWithLoudTrack() throws {
        // Loud track: full-scale-ish tone. Quiet track: same shape but at
        // roughly 1/16th amplitude (about -24 dB quieter), similar to the
        // real-world mic-vs-system imbalance this fix addresses.
        let loudSamples: [Int16] = (0..<200).map { i in
            Int16(clamping: Int(20_000.0 * sin(Double(i) * 0.3)))
        }
        let quietSamples: [Int16] = loudSamples.map { Int16(clamping: Int($0) / 16) }

        let loud = WAVPCMFile(sampleRate: 16_000, samples: loudSamples)
        let quiet = WAVPCMFile(sampleRate: 16_000, samples: quietSamples)

        let rawMixed = try WAVMixer.mix(loud, quiet, mode: .raw)
        let balancedMixed = try WAVMixer.mix(loud, quiet, mode: .balanced)

        // Sanity: raw mixing is dominated by the loud track's RMS (its
        // contribution to the raw mix should track the loud track closely).
        let rawRMS = WAVMixer.rms(rawMixed.samples)
        let loudRMS = WAVMixer.rms(loudSamples)
        let quietRMS = WAVMixer.rms(quietSamples)
        #expect(quietRMS < loudRMS * 0.2)
        #expect(abs(rawRMS - loudRMS) < loudRMS * 0.3)

        // After balancing, the two tracks should be lifted to comparable
        // (parity) loudness before summation, so the balanced mix should be
        // meaningfully louder than the raw mix (the quiet track no longer
        // gets buried) while never having reduced the loud track's level.
        let balancedRMS = WAVMixer.rms(balancedMixed.samples)
        #expect(balancedRMS > rawRMS)

        // Directly verify the gain policy: scaling the quiet track up by
        // (loudRMS / quietRMS) should bring it to parity with the loud track.
        let impliedGain = loudRMS / quietRMS
        let scaledQuiet = WAVMixer.scale(quietSamples, by: impliedGain)
        let scaledQuietRMS = WAVMixer.rms(scaledQuiet)
        #expect(abs(scaledQuietRMS - loudRMS) < loudRMS * 0.05)
    }

    @Test func balancedModeDoesNotBlanketAttenuateBurstyMeetingAudio() throws {
        // Regression fixture for real meeting captures where tracks have very
        // different duty cycles: a low-duty-cycle mic plus a more continuous
        // system track. A fixed post-sum attenuation factor can erase the RMS
        // lift from balancing and make the mixed file sound too quiet.
        let sampleCount = 400
        let micSamples: [Int16] = (0..<sampleCount).map { index in
            guard index % 8 == 0 else { return 0 }
            return (index / 8).isMultiple(of: 2) ? 60 : -60
        }
        let systemSamples: [Int16] = (0..<sampleCount).map { index in
            guard index % 2 == 0 else { return 0 }
            return (index / 2).isMultiple(of: 2) ? 100 : -100
        }

        let mic = WAVPCMFile(sampleRate: 16_000, samples: micSamples)
        let system = WAVPCMFile(sampleRate: 16_000, samples: systemSamples)

        let rawMixed = try WAVMixer.mix(mic, system, mode: .raw)
        let balancedMixed = try WAVMixer.mix(mic, system, mode: .balanced)

        #expect(WAVMixer.rms(balancedMixed.samples) > WAVMixer.rms(rawMixed.samples))
        #expect(peakMagnitude(balancedMixed.samples) > peakMagnitude(rawMixed.samples))
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

        let mixed = try WAVMixer.mix(lhs, rhs, mode: .raw)

        #expect(mixed.sampleRate == 20)
        #expect(mixed.samples == [0, 50, 100, 100])
    }

    @Test func mixUpsamplesCommonMeetingSampleRateMismatch() throws {
        let lhs = WAVPCMFile(sampleRate: 44_100, samples: Array(repeating: Int16(1_000), count: 441))
        let rhs = WAVPCMFile(sampleRate: 48_000, samples: Array(repeating: Int16(1_000), count: 480))

        let mixed = try WAVMixer.mix(lhs, rhs, mode: .raw)

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

        let result = try WAVMixer.mixFiles(lhsURL, rhsURL, outputURL: outURL, mode: .raw)
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

        let result = try WAVMixer.mixFiles(lhsURL, rhsURL, outputURL: outURL, mode: .raw)
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

    private func peakMagnitude(_ samples: [Int16]) -> Int {
        samples.map { abs(Int($0)) }.max() ?? 0
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
