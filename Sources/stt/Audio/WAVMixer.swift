import Foundation

public enum WAVMixerError: Error, LocalizedError {
    case invalidHeader(String)
    case unsupportedFormat(String)
    // Retained for source compatibility with earlier callers. `WAVMixer.mix`
    // now resamples differing sample rates to the higher rate instead of
    // throwing this error.
    case sampleRateMismatch(UInt32, UInt32)

    public var errorDescription: String? {
        switch self {
        case .invalidHeader(let reason):
            return "Invalid WAV file: \(reason)"
        case .unsupportedFormat(let reason):
            return "Unsupported WAV format for mixing: \(reason)"
        case .sampleRateMismatch(let lhs, let rhs):
            return "Cannot mix WAV files with different sample rates: \(lhs) vs \(rhs)"
        }
    }
}

public struct WAVPCMFile: Equatable {
    public let sampleRate: UInt32
    public let channels: UInt16
    public let bitDepth: UInt16
    public let samples: [Int16] // mono samples

    public init(sampleRate: UInt32, channels: UInt16 = 1, bitDepth: UInt16 = 16, samples: [Int16]) {
        self.sampleRate = sampleRate
        self.channels = channels
        self.bitDepth = bitDepth
        self.samples = samples
    }

    public static func parse(_ data: Data) throws -> WAVPCMFile {
        guard data.count >= 44 else { throw WAVMixerError.invalidHeader("file is shorter than the canonical 44-byte header") }
        guard data.subdata(in: 0..<4) == Data("RIFF".utf8) else { throw WAVMixerError.invalidHeader("missing RIFF marker") }
        guard data.subdata(in: 8..<12) == Data("WAVE".utf8) else { throw WAVMixerError.invalidHeader("missing WAVE marker") }

        var cursor = 12
        var audioFormat: UInt16?
        var channels: UInt16?
        var sampleRate: UInt32?
        var bitDepth: UInt16?
        var pcmData: Data?

        while cursor + 8 <= data.count {
            let chunkID = data.subdata(in: cursor..<(cursor + 4))
            let chunkSize = Int(wavReadUInt32LE(data, at: cursor + 4))
            let chunkStart = cursor + 8
            let chunkEnd = chunkStart + chunkSize
            guard chunkSize >= 0, chunkEnd <= data.count else {
                throw WAVMixerError.invalidHeader("chunk extends beyond end of file")
            }

            if chunkID == Data("fmt ".utf8) {
                guard chunkSize >= 16 else { throw WAVMixerError.invalidHeader("fmt chunk is too short") }
                audioFormat = wavReadUInt16LE(data, at: chunkStart)
                channels = wavReadUInt16LE(data, at: chunkStart + 2)
                sampleRate = wavReadUInt32LE(data, at: chunkStart + 4)
                bitDepth = wavReadUInt16LE(data, at: chunkStart + 14)
            } else if chunkID == Data("data".utf8) {
                pcmData = data.subdata(in: chunkStart..<chunkEnd)
            }

            // Chunks are word-aligned.
            cursor = chunkEnd + (chunkSize % 2)
        }

        guard let audioFormat, let channels, let sampleRate, let bitDepth, let pcmData else {
            throw WAVMixerError.invalidHeader("missing required fmt/data chunk")
        }
        guard audioFormat == 1 else { throw WAVMixerError.unsupportedFormat("only integer PCM format 1 is supported") }
        guard bitDepth == 16 else { throw WAVMixerError.unsupportedFormat("only 16-bit PCM is supported") }
        guard channels >= 1 else { throw WAVMixerError.unsupportedFormat("channel count must be at least 1") }
        guard sampleRate > 0 else { throw WAVMixerError.unsupportedFormat("sample rate must be greater than 0") }
        let bytesPerFrame = Int(channels) * 2
        guard pcmData.count % bytesPerFrame == 0 else { throw WAVMixerError.invalidHeader("data chunk is not aligned to complete frames") }

        let frameCount = pcmData.count / bytesPerFrame
        var monoSamples: [Int16] = []
        monoSamples.reserveCapacity(frameCount)
        for frame in 0..<frameCount {
            var sum = 0
            for channel in 0..<Int(channels) {
                let offset = (frame * bytesPerFrame) + (channel * 2)
                sum += Int(wavReadInt16LE(pcmData, at: offset))
            }
            monoSamples.append(Int16(clamping: sum / Int(channels)))
        }

        return WAVPCMFile(sampleRate: sampleRate, channels: 1, bitDepth: bitDepth, samples: monoSamples)
    }

    public func encodedData() -> Data {
        var pcm = Data()
        pcm.reserveCapacity(samples.count * 2)
        for sample in samples {
            pcm.appendInt16LE(sample)
        }
        return WAVWriter.header(sampleRate: sampleRate, channels: 1, bitDepth: 16, dataSize: UInt32(pcm.count)) + pcm
    }
}

public enum MixMode {
    /// Raw PCM summation, exactly matching legacy behavior. No per-track
    /// loudness normalization is applied. Useful as an escape hatch and for
    /// test/back-compat purposes.
    case raw
    /// Loudness-balanced mix (default). Boosts whichever track is quieter
    /// (by RMS) up to the louder track's level before summing, so a soft
    /// system-audio capture is no longer buried under a louder mic capture.
    case balanced
}

public enum WAVMixer {
    /// Heuristic threshold for warning that separately captured mic/system
    /// tracks may be misaligned after mix-down. Real-world drift depends on
    /// devices, routing, and startup latency; this warning is intentionally
    /// non-fatal so the original tracks remain usable for manual inspection.
    public static let defaultDriftWarningThresholdSeconds: Double = 0.25

    /// Samples quieter than this (in absolute Int16 magnitude) are treated as
    /// silence and excluded from the RMS calculation, so a silent lead-in/tail
    /// doesn't drag down a track's measured loudness.
    private static let silenceGateThreshold: Double = 32.0

    /// Maximum 16-bit PCM magnitude used when computing adaptive headroom for
    /// balanced mixes. Balanced mode should only attenuate when the boosted sum
    /// would clip; otherwise it preserves the captured track levels and lets
    /// the quieter track's lift be audible.
    private static let int16PeakMagnitude = Double(Int16.max)

    /// Balanced mixes are normalized upward when the combined capture is very
    /// quiet. Peak-based normalization avoids clipping; the gain cap prevents
    /// near-silent noise floors from being amplified into loud artifacts.
    private static let balancedMixTargetPeakRatio = 0.8
    private static let balancedMixMaxOutputGain = 16.0

    public static func mix(_ lhs: WAVPCMFile, _ rhs: WAVPCMFile, mode: MixMode = .balanced) throws -> WAVPCMFile {
        guard lhs.bitDepth == 16, rhs.bitDepth == 16 else {
            throw WAVMixerError.unsupportedFormat("only 16-bit PCM is supported")
        }
        guard lhs.sampleRate > 0, rhs.sampleRate > 0 else {
            throw WAVMixerError.unsupportedFormat("sample rate must be greater than 0")
        }

        let targetRate = max(lhs.sampleRate, rhs.sampleRate)
        var lhsSamples = resample(lhs.samples, from: lhs.sampleRate, to: targetRate)
        var rhsSamples = resample(rhs.samples, from: rhs.sampleRate, to: targetRate)

        var headroomFactor = 1.0
        if mode == .balanced {
            let lhsRMS = rms(lhsSamples)
            let rhsRMS = rms(rhsSamples)
            // Policy: target the louder track's RMS and boost the quieter
            // track up to parity. Neither track is ever attenuated below its
            // captured level, so a quiet recording is never made quieter --
            // only the imbalance between the two tracks is corrected.
            if lhsRMS > 0, rhsRMS > 0 {
                if lhsRMS > rhsRMS {
                    let gain = lhsRMS / rhsRMS
                    rhsSamples = scale(rhsSamples, by: gain)
                } else if rhsRMS > lhsRMS {
                    let gain = rhsRMS / lhsRMS
                    lhsSamples = scale(lhsSamples, by: gain)
                }
            }
        }

        let count = max(lhsSamples.count, rhsSamples.count)
        if mode == .balanced {
            headroomFactor = adaptiveHeadroomFactor(lhsSamples, rhsSamples, count: count)
        }

        var mixed: [Int16] = []
        mixed.reserveCapacity(count)
        for index in 0..<count {
            let a = index < lhsSamples.count ? Int(lhsSamples[index]) : 0
            let b = index < rhsSamples.count ? Int(rhsSamples[index]) : 0
            if mode == .balanced {
                let summed = (Double(a) + Double(b)) * headroomFactor
                mixed.append(Int16(clamping: Int(summed.rounded())))
            } else {
                mixed.append(Int16(clamping: a + b))
            }
        }
        if mode == .balanced {
            mixed = normalizePeakUp(
                mixed,
                targetPeak: int16PeakMagnitude * balancedMixTargetPeakRatio,
                maxGain: balancedMixMaxOutputGain
            )
        }
        return WAVPCMFile(sampleRate: targetRate, samples: mixed)
    }

    /// Computes the root-mean-square level of `samples`, gating out
    /// near-silent samples (see `silenceGateThreshold`) so a silent lead-in
    /// or tail doesn't skew the measured loudness of an otherwise louder
    /// track. Returns 0 if there are no non-silent samples.
    static func rms(_ samples: [Int16]) -> Double {
        var sumSquares = 0.0
        var count = 0
        for sample in samples {
            let magnitude = abs(Double(sample))
            guard magnitude >= silenceGateThreshold else { continue }
            sumSquares += magnitude * magnitude
            count += 1
        }
        guard count > 0 else { return 0 }
        return (sumSquares / Double(count)).squareRoot()
    }

    /// Computes the smallest attenuation needed to keep a balanced summed mix
    /// inside the Int16 range. Returns 1.0 unless the boosted sum would clip.
    static func adaptiveHeadroomFactor(_ lhsSamples: [Int16], _ rhsSamples: [Int16], count: Int) -> Double {
        var peak = 0.0
        for index in 0..<count {
            let a = index < lhsSamples.count ? Double(lhsSamples[index]) : 0
            let b = index < rhsSamples.count ? Double(rhsSamples[index]) : 0
            peak = max(peak, abs(a + b))
        }
        guard peak > int16PeakMagnitude else { return 1.0 }
        return int16PeakMagnitude / peak
    }

    /// Normalizes a mix upward to `targetPeak` without ever attenuating an
    /// already-loud result. `maxGain` limits amplification of near-silence.
    static func normalizePeakUp(_ samples: [Int16], targetPeak: Double, maxGain: Double) -> [Int16] {
        let peak = samples.map { abs(Double($0)) }.max() ?? 0
        guard peak > 0, peak < targetPeak, maxGain > 1 else { return samples }
        let gain = min(maxGain, targetPeak / peak)
        return scale(samples, by: gain)
    }

    /// Scales `samples` by a floating-point `gain`, clamping each result to
    /// the Int16 range to avoid overflow.
    static func scale(_ samples: [Int16], by gain: Double) -> [Int16] {
        guard gain != 1.0 else { return samples }
        return samples.map { sample in
            let scaled = Double(sample) * gain
            return Int16(clamping: Int(scaled.rounded()))
        }
    }

    private static func resample(_ samples: [Int16], from sourceRate: UInt32, to targetRate: UInt32) -> [Int16] {
        guard sourceRate != targetRate, sourceRate > 0, targetRate > 0, !samples.isEmpty else {
            return samples
        }

        let ratio = Double(targetRate) / Double(sourceRate)
        let outputCount = Int((Double(samples.count) * ratio).rounded())
        guard outputCount > 0 else { return [] }

        let lastIndex = samples.count - 1
        var output: [Int16] = []
        output.reserveCapacity(outputCount)
        for index in 0..<outputCount {
            let sourcePosition = Double(index) / ratio
            let sourceIndex = min(Int(sourcePosition), lastIndex)
            let fraction = sourcePosition - Double(sourceIndex)
            let first = Double(samples[sourceIndex])
            let second = Double(samples[min(sourceIndex + 1, lastIndex)])
            let interpolated = first + ((second - first) * fraction)
            output.append(Int16(clamping: Int(interpolated.rounded())))
        }
        return output
    }

    public static func driftWarning(lhsURL: URL,
                                    rhsURL: URL,
                                    thresholdSeconds: Double = defaultDriftWarningThresholdSeconds) -> String? {
        guard let lhs = try? WAVPCMFile.parse(Data(contentsOf: lhsURL)),
              let rhs = try? WAVPCMFile.parse(Data(contentsOf: rhsURL)) else {
            return nil
        }

        let lhsDuration = Double(lhs.samples.count) / Double(lhs.sampleRate)
        let rhsDuration = Double(rhs.samples.count) / Double(rhs.sampleRate)
        let drift = abs(lhsDuration - rhsDuration)
        guard drift > thresholdSeconds else { return nil }

        return String(
            format: "Mic/system track duration drift detected: %.2fs (mic: %.2fs, system: %.2fs). Mixed audio may have timing misalignment; use --separate-tracks for downstream diarization or manual alignment.",
            drift,
            lhsDuration,
            rhsDuration
        )
    }

    @discardableResult
    public static func mixFiles(_ lhsURL: URL, _ rhsURL: URL, outputURL: URL, mode: MixMode = .balanced) throws -> RecordingResult {
        let lhs = try WAVPCMFile.parse(Data(contentsOf: lhsURL))
        let rhs = try WAVPCMFile.parse(Data(contentsOf: rhsURL))
        let mixed = try mix(lhs, rhs, mode: mode)
        try Paths.ensureDirectoryExists(outputURL.deletingLastPathComponent())
        try mixed.encodedData().write(to: outputURL, options: .atomic)
        let duration = Double(mixed.samples.count) / Double(mixed.sampleRate)
        let size = (try? FileManager.default.attributesOfItem(atPath: outputURL.path)[.size] as? NSNumber)?.uint64Value
        return RecordingResult(outputURL: outputURL, durationSeconds: duration, fileSizeBytes: size)
    }

}

private func wavReadUInt16LE(_ data: Data, at offset: Int) -> UInt16 {
    UInt16(data[offset]) | (UInt16(data[offset + 1]) << 8)
}

private func wavReadUInt32LE(_ data: Data, at offset: Int) -> UInt32 {
    UInt32(data[offset]) |
    (UInt32(data[offset + 1]) << 8) |
    (UInt32(data[offset + 2]) << 16) |
    (UInt32(data[offset + 3]) << 24)
}

private func wavReadInt16LE(_ data: Data, at offset: Int) -> Int16 {
    Int16(bitPattern: wavReadUInt16LE(data, at: offset))
}

private extension Data {
    mutating func appendInt16LE(_ value: Int16) {
        let unsigned = UInt16(bitPattern: value)
        append(UInt8(unsigned & 0xff))
        append(UInt8((unsigned >> 8) & 0xff))
    }
}
