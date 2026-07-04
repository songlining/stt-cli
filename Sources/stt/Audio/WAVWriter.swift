import Foundation

/// A small, dependency-free WAV (RIFF/PCM) writer usable independently of
/// AVAudioFile. Provides pure, unit-testable header construction plus a
/// thin streaming writer for incremental capture.
public enum WAVWriter {

    /// Builds a 44-byte canonical WAV header for 16-bit (or other bit-depth)
    /// integer PCM audio.
    ///
    /// - Parameters:
    ///   - sampleRate: samples per second, e.g. 44100 or 16000.
    ///   - channels: number of interleaved channels.
    ///   - bitDepth: bits per sample, e.g. 16.
    ///   - dataSize: number of bytes of PCM sample data that will follow the header.
    public static func header(sampleRate: UInt32, channels: UInt16, bitDepth: UInt16, dataSize: UInt32) -> Data {
        var data = Data()

        let byteRate = sampleRate * UInt32(channels) * UInt32(bitDepth / 8)
        let blockAlign = channels * (bitDepth / 8)
        let subchunk2Size = dataSize
        let chunkSize = 36 + subchunk2Size

        data.append(ascii("RIFF"))
        data.append(le(chunkSize))
        data.append(ascii("WAVE"))

        data.append(ascii("fmt "))
        data.append(le(UInt32(16)))               // Subchunk1Size (16 for PCM)
        data.append(le(UInt16(1)))                 // AudioFormat: 1 = PCM
        data.append(le(channels))
        data.append(le(sampleRate))
        data.append(le(byteRate))
        data.append(le(blockAlign))
        data.append(le(bitDepth))

        data.append(ascii("data"))
        data.append(le(subchunk2Size))

        return data
    }

    /// Total byte size of a WAV file given a canonical 44-byte header plus data.
    public static func totalFileSize(dataSize: UInt32) -> UInt32 {
        44 + dataSize
    }

    private static func ascii(_ string: String) -> Data {
        Data(string.utf8)
    }

    private static func le(_ value: UInt32) -> Data {
        withUnsafeBytes(of: value.littleEndian) { Data($0) }
    }

    private static func le(_ value: UInt16) -> Data {
        withUnsafeBytes(of: value.littleEndian) { Data($0) }
    }
}

/// Errors surfaced while streaming PCM data to a WAV file on disk.
public enum StreamingWAVWriterError: Error, LocalizedError {
    case fileCreationFailed(String)
    case notOpen

    public var errorDescription: String? {
        switch self {
        case .fileCreationFailed(let path):
            return "Could not create file at \(path)"
        case .notOpen:
            return "WAV writer is not open"
        }
    }
}

/// Streams raw PCM samples to a WAV file, writing a placeholder header first
/// and patching it with the correct sizes on `finish()`. Useful for
/// low-level / manual capture paths that don't go through AVAudioFile.
public final class StreamingWAVWriter {
    private let sampleRate: UInt32
    private let channels: UInt16
    private let bitDepth: UInt16
    private var fileHandle: FileHandle?
    private var bytesWritten: UInt32 = 0
    public let url: URL

    public init(url: URL, sampleRate: UInt32, channels: UInt16, bitDepth: UInt16 = 16) throws {
        self.url = url
        self.sampleRate = sampleRate
        self.channels = channels
        self.bitDepth = bitDepth

        let fileManager = FileManager.default
        if !fileManager.createFile(atPath: url.path, contents: nil) {
            throw StreamingWAVWriterError.fileCreationFailed(url.path)
        }
        let handle = try FileHandle(forWritingTo: url)
        // Reserve space for the header; patched in on finish().
        handle.write(WAVWriter.header(sampleRate: sampleRate, channels: channels, bitDepth: bitDepth, dataSize: 0))
        self.fileHandle = handle
    }

    /// Appends raw PCM bytes (already in the target bit depth/channel layout).
    public func append(_ data: Data) throws {
        guard let fileHandle else { throw StreamingWAVWriterError.notOpen }
        fileHandle.write(data)
        bytesWritten += UInt32(data.count)
    }

    /// Finalizes the WAV file by rewriting the header with the correct sizes,
    /// then closes the file handle. Safe to call once.
    @discardableResult
    public func finish() throws -> URL {
        guard let fileHandle else { throw StreamingWAVWriterError.notOpen }
        let header = WAVWriter.header(sampleRate: sampleRate, channels: channels, bitDepth: bitDepth, dataSize: bytesWritten)
        try fileHandle.seek(toOffset: 0)
        fileHandle.write(header)
        try fileHandle.close()
        self.fileHandle = nil
        return url
    }

    /// Duration, in seconds, of the audio written so far.
    public var durationSeconds: Double {
        let bytesPerSample = Double(bitDepth / 8)
        let totalSamples = Double(bytesWritten) / (bytesPerSample * Double(channels))
        return totalSamples / Double(sampleRate)
    }
}
