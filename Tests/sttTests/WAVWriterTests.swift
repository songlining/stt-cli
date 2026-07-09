import Foundation
import Testing
@testable import sttCore

@Suite("WAVWriter")
struct WAVWriterTests {

    @Test func headerByteExact16kHzMono16bit() {
        let sampleRate: UInt32 = 16000
        let channels: UInt16 = 1
        let bitDepth: UInt16 = 16
        let dataSize: UInt32 = 32000 // 1 second of audio at 16kHz/mono/16-bit

        let header = WAVWriter.header(sampleRate: sampleRate, channels: channels, bitDepth: bitDepth, dataSize: dataSize)

        #expect(header.count == 44)

        var expected = Data()
        expected.append(contentsOf: Array("RIFF".utf8))
        expected.append(contentsOf: withUnsafeBytes(of: UInt32(36 + dataSize).littleEndian) { Array($0) })
        expected.append(contentsOf: Array("WAVE".utf8))
        expected.append(contentsOf: Array("fmt ".utf8))
        expected.append(contentsOf: withUnsafeBytes(of: UInt32(16).littleEndian) { Array($0) })
        expected.append(contentsOf: withUnsafeBytes(of: UInt16(1).littleEndian) { Array($0) }) // PCM
        expected.append(contentsOf: withUnsafeBytes(of: channels.littleEndian) { Array($0) })
        expected.append(contentsOf: withUnsafeBytes(of: sampleRate.littleEndian) { Array($0) })
        let byteRate = sampleRate * UInt32(channels) * UInt32(bitDepth / 8)
        expected.append(contentsOf: withUnsafeBytes(of: byteRate.littleEndian) { Array($0) })
        let blockAlign = channels * (bitDepth / 8)
        expected.append(contentsOf: withUnsafeBytes(of: blockAlign.littleEndian) { Array($0) })
        expected.append(contentsOf: withUnsafeBytes(of: bitDepth.littleEndian) { Array($0) })
        expected.append(contentsOf: Array("data".utf8))
        expected.append(contentsOf: withUnsafeBytes(of: dataSize.littleEndian) { Array($0) })

        #expect(header == expected)
    }

    @Test func headerFieldsExplicit44100Stereo16bit() {
        let header = WAVWriter.header(sampleRate: 44100, channels: 2, bitDepth: 16, dataSize: 1000)

        // RIFF chunk descriptor
        #expect(header.subdata(in: 0..<4) == Data("RIFF".utf8))
        let chunkSize = header.subdata(in: 4..<8).withUnsafeBytes { $0.load(as: UInt32.self) }
        #expect(UInt32(littleEndian: chunkSize) == 1036)
        #expect(header.subdata(in: 8..<12) == Data("WAVE".utf8))

        // fmt subchunk
        #expect(header.subdata(in: 12..<16) == Data("fmt ".utf8))
        let subchunk1Size = header.subdata(in: 16..<20).withUnsafeBytes { UInt32(littleEndian: $0.load(as: UInt32.self)) }
        #expect(subchunk1Size == 16)
        let audioFormat = header.subdata(in: 20..<22).withUnsafeBytes { UInt16(littleEndian: $0.load(as: UInt16.self)) }
        #expect(audioFormat == 1)
        let numChannels = header.subdata(in: 22..<24).withUnsafeBytes { UInt16(littleEndian: $0.load(as: UInt16.self)) }
        #expect(numChannels == 2)
        let sampleRateField = header.subdata(in: 24..<28).withUnsafeBytes { UInt32(littleEndian: $0.load(as: UInt32.self)) }
        #expect(sampleRateField == 44100)
        let byteRate = header.subdata(in: 28..<32).withUnsafeBytes { UInt32(littleEndian: $0.load(as: UInt32.self)) }
        #expect(byteRate == 44100 * 2 * 2)
        let blockAlign = header.subdata(in: 32..<34).withUnsafeBytes { UInt16(littleEndian: $0.load(as: UInt16.self)) }
        #expect(blockAlign == 4)
        let bitsPerSample = header.subdata(in: 34..<36).withUnsafeBytes { UInt16(littleEndian: $0.load(as: UInt16.self)) }
        #expect(bitsPerSample == 16)

        // data subchunk
        #expect(header.subdata(in: 36..<40) == Data("data".utf8))
        let subchunk2Size = header.subdata(in: 40..<44).withUnsafeBytes { UInt32(littleEndian: $0.load(as: UInt32.self)) }
        #expect(subchunk2Size == 1000)
    }

    @Test func totalFileSize() {
        #expect(WAVWriter.totalFileSize(dataSize: 1000) == 1044)
        #expect(WAVWriter.totalFileSize(dataSize: 0) == 44)
    }

    @Test func streamingWAVWriterProducesValidHeaderAfterFinish() throws {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let fileURL = tmpDir.appendingPathComponent("test.wav")
        let writer = try StreamingWAVWriter(url: fileURL, sampleRate: 16000, channels: 1, bitDepth: 16)

        let sampleData = Data(repeating: 0, count: 3200) // 100ms at 16kHz mono 16-bit
        try writer.append(sampleData)
        try writer.append(sampleData)

        #expect(writer.durationSeconds == 0.2)

        _ = try writer.finish()

        let fileData = try Data(contentsOf: fileURL)
        #expect(fileData.count == 44 + 6400)

        let dataSizeField = fileData.subdata(in: 40..<44).withUnsafeBytes { UInt32(littleEndian: $0.load(as: UInt32.self)) }
        #expect(dataSizeField == 6400)
    }

    @Test func streamingWAVWriterIsCrashSafeBeforeFinish() throws {
        // Regression test for SIGKILL/crash safety: the WAV header on disk must
        // track appended data periodically (not only on finish()), so that a
        // force-killed recorder never leaves a header-only or stale-size WAV
        // that some strict decoders would read as empty/truncated.
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let fileURL = tmpDir.appendingPathComponent("crashsafe.wav")
        let writer = try StreamingWAVWriter(url: fileURL, sampleRate: 48000, channels: 2, bitDepth: 16)

        // Append well past the 1 MiB header-patch threshold so the header is
        // rewritten in place at least once without finish() being called.
        let chunk = Data(repeating: 0xAB, count: 1 << 16) // 64 KiB
        var total = 0
        for _ in 0..<40 { // 2.5 MiB total
            try writer.append(chunk)
            total += chunk.count
        }
        // Deliberately do NOT call finish(): simulate an abrupt termination.

        let fileData = try Data(contentsOf: fileURL)
        #expect(fileData.count == 44 + total)

        // The header's data-size field must reflect (approximately) the bytes
        // appended so far -- not the initial 0. It is patched on each MiB
        // boundary, so it should be at least the last whole MiB written.
        let dataSizeField = fileData.subdata(in: 40..<44).withUnsafeBytes { UInt32(littleEndian: $0.load(as: UInt32.self)) }
        #expect(dataSizeField >= 1 << 20)
        #expect(dataSizeField <= UInt32(total))

        // The RIFF chunk size must be consistent with the patched data size.
        let riffSize = fileData.subdata(in: 4..<8).withUnsafeBytes { UInt32(littleEndian: $0.load(as: UInt32.self)) }
        #expect(riffSize == 36 + dataSizeField)

        // The tail of the file must still contain the most recent samples
        // (i.e. patching the header did not truncate the data stream).
        #expect(fileData.suffix(chunk.count) == chunk)
    }
}
