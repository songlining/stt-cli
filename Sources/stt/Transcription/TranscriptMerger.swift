import Foundation

public struct TranscriptJSON: Codable, Equatable {
    public var audioFile: String?
    public var backend: String?
    public var device: String?
    public var modelPath: String?
    public var durationSeconds: Double?
    public var text: String?
    public var diarisedText: String?
    public var segments: [TranscriptSegment]

    public init(audioFile: String? = nil,
                backend: String? = nil,
                device: String? = nil,
                modelPath: String? = nil,
                durationSeconds: Double? = nil,
                text: String? = nil,
                diarisedText: String? = nil,
                segments: [TranscriptSegment] = []) {
        self.audioFile = audioFile
        self.backend = backend
        self.device = device
        self.modelPath = modelPath
        self.durationSeconds = durationSeconds
        self.text = text
        self.diarisedText = diarisedText
        self.segments = segments
    }

    enum CodingKeys: String, CodingKey {
        case audioFile = "audio_file"
        case backend
        case device
        case modelPath = "model_path"
        case durationSeconds = "duration_seconds"
        case text
        case diarisedText = "diarised_text"
        case segments
    }
}

public struct TranscriptSegment: Codable, Equatable {
    public var text: String
    public var startTime: Double
    public var endTime: Double
    public var duration: Double?
    public var speakerID: String?
    public var source: String?
    /// Resolved display name for this speaker (e.g. "Larry"), set
    /// programmatically by speaker identification. `nil` keeps the default
    /// "Speaker N" rendering. Not decoded from ASR JSON (CodingKeys
    /// excludes it).
    public var speakerName: String?

    public init(text: String,
                startTime: Double,
                endTime: Double,
                duration: Double? = nil,
                speakerID: String? = nil,
                source: String? = nil,
                speakerName: String? = nil) {
        self.text = text
        self.startTime = startTime
        self.endTime = endTime
        self.duration = duration
        self.speakerID = speakerID
        self.source = source
        self.speakerName = speakerName
    }

    enum CodingKeys: String, CodingKey {
        case text
        case startTime = "start_time"
        case endTime = "end_time"
        case duration
        case speakerID = "speaker_id"
        case source
    }

    // Tolerates `speaker_id` arriving as either a string ("0", diarisation
    // output) or a number (0, raw VibeVoice ASR output). Always stored as a
    // String so downstream code (rendering, SpeakerLabelResolver) has one type.
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        text = try c.decode(String.self, forKey: .text)
        startTime = try c.decode(Double.self, forKey: .startTime)
        endTime = try c.decode(Double.self, forKey: .endTime)
        duration = try c.decodeIfPresent(Double.self, forKey: .duration)
        source = try c.decodeIfPresent(String.self, forKey: .source)
        if let string = try? c.decodeIfPresent(String.self, forKey: .speakerID) {
            speakerID = string
        } else if let number = try? c.decodeIfPresent(Int.self, forKey: .speakerID) {
            speakerID = String(number)
        } else {
            speakerID = nil
        }
    }
}

public struct MeetingTranscriptMergeResult: Equatable {
    public let text: String
    public let jsonData: Data
    public let segmentCount: Int
    public let sources: [String]
}

public enum TranscriptMergerError: Error, LocalizedError {
    case noUsableSegments

    public var errorDescription: String? {
        switch self {
        case .noUsableSegments:
            return "No usable transcript segments were found in any source transcript."
        }
    }
}

/// Error raised when applying diarized speaker ids back onto a transcript.
public enum DiarizedSpeakerIDError: Error, LocalizedError, Equatable {
    case segmentCountMismatch(transcript: Int, diarized: Int)

    public var errorDescription: String? {
        switch self {
        case .segmentCountMismatch(let transcript, let diarized):
            return "Diarization returned \(diarized) segments but the transcript has \(transcript); cannot match by index."
        }
    }
}

public enum TranscriptMerger {
    /// Copies each diarized segment's `speaker_id` onto the corresponding
    /// transcript segment by index, preserving all other top-level fields.
    /// diarize.py preserves input segment order, so index-matching is valid.
    /// Throws when the segment counts differ.
    public static func applyDiarizedSpeakerIDs(_ transcript: TranscriptJSON,
                                                result: DiarizationResult) throws -> TranscriptJSON {
        guard transcript.segments.count == result.segments.count else {
            throw DiarizedSpeakerIDError.segmentCountMismatch(
                transcript: transcript.segments.count,
                diarized: result.segments.count
            )
        }
        var updated = transcript
        updated.segments = zip(transcript.segments, result.segments).map { original, diarized in
            var seg = original
            seg.speakerID = diarized.speakerID
            return seg
        }
        return updated
    }

    /// Reads the transcript JSON at `url`, writes each diarized segment's
    /// `speaker_id` onto the matching segment by index, and writes it back.
    public static func applyDiarizationToFile(transcriptURL: URL, result: DiarizationResult) throws {
        let data = try Data(contentsOf: transcriptURL)
        let transcript = try JSONDecoder().decode(TranscriptJSON.self, from: data)
        let updated = try applyDiarizedSpeakerIDs(transcript, result: result)
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        let out = try encoder.encode(updated)
        try out.write(to: transcriptURL, options: .atomic)
    }

    /// Reads the merged transcript JSON at `transcriptURL`, stamps each
    /// segment's `speakerName` from `speakerNames` (keyed by speaker_id
    /// string), and re-renders both the JSON and plain-text output. Segments
    /// whose speaker_id is not in the map keep `speakerName = nil`. Writes
    /// the JSON back to `transcriptURL` and the plain text to
    /// `outputTextURL` (if given).
    ///
    /// Top-level fields already present on the merged transcript
    /// (backend, duration_seconds, text, diarised_text, sources) are
    /// preserved exactly — the original JSON object's `sources` array is
    /// carried through untouched, and only segments/text/diarised_text are
    /// rewritten.
    public static func applySpeakerNames(transcriptURL: URL, outputTextURL: URL?, speakerNames: [String: String]) throws {
        let data = try Data(contentsOf: transcriptURL)
        var transcript = try JSONDecoder().decode(TranscriptJSON.self, from: data)
        for i in transcript.segments.indices {
            if let sid = transcript.segments[i].speakerID, let name = speakerNames[sid] {
                transcript.segments[i].speakerName = name
            }
        }

        // Preserve the original `sources` array (per-source audio_file /
        // backend / duration_seconds metadata) that the TranscriptJSON
        // Codable model does not model, so re-rendering is lossless.
        let preservedSources: Any?
        if let rawObject = try JSONSerialization.jsonObject(with: data, options: []) as? [String: Any] {
            preservedSources = rawObject["sources"]
        } else {
            preservedSources = nil
        }

        let plainText = renderPlainText(segments: transcript.segments)
        var jsonData = try renderJSONFromTranscript(transcript, plainText: plainText)

        if let preservedSources {
            // Overwrite the best-effort reconstructed sources with the
            // original, losslessly preserving per-source metadata.
            if var object = (try? JSONSerialization.jsonObject(with: jsonData, options: [])) as? [String: Any] {
                object["sources"] = preservedSources
                jsonData = try JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys])
            }
        }

        try jsonData.write(to: transcriptURL, options: .atomic)
        if let outputTextURL {
            try plainText.write(to: outputTextURL, atomically: true, encoding: .utf8)
        }
    }

    /// Re-renders a merged transcript JSON from a decoded `TranscriptJSON`,
    /// preserving its existing top-level fields (backend,
    /// duration_seconds, text, diarised_text) and re-emitting every segment
    /// — now including `speaker_name` where set. `sources` is reconstructed
    /// best-effort from segment source labels; callers that need to
    /// preserve the original `sources` metadata (see `applySpeakerNames`)
    /// overlay it afterwards.
    private static func renderJSONFromTranscript(_ transcript: TranscriptJSON, plainText: String) throws -> Data {
        let trimmedText = plainText.trimmingCharacters(in: .whitespacesAndNewlines)

        // Group segments by source to reconstruct a sources summary. Each
        // entry mirrors the shape produced by renderJSON: source name plus
        // the per-source duration taken from the max segment end_time.
        var sourceMaxEnd: [String: Double] = [:]
        for segment in transcript.segments {
            let key = segment.source ?? "unknown"
            sourceMaxEnd[key] = max(sourceMaxEnd[key] ?? 0, segment.endTime)
        }
        let sourceObjects: [[String: Any]] = sourceMaxEnd.keys.sorted().map { source in
            var object: [String: Any] = ["source": source]
            if let duration = sourceMaxEnd[source] { object["duration_seconds"] = duration }
            return object
        }

        let segmentObjects: [[String: Any]] = transcript.segments.map { segment in
            var object: [String: Any] = [
                "text": segment.text,
                "start_time": segment.startTime,
                "end_time": segment.endTime,
                "source": segment.source ?? "unknown"
            ]
            if let duration = segment.duration { object["duration"] = duration }
            if let speakerID = segment.speakerID { object["speaker_id"] = speakerID }
            if let speakerName = segment.speakerName { object["speaker_name"] = speakerName }
            return object
        }

        let maxDuration = transcript.segments.map(\.endTime).max() ?? transcript.durationSeconds ?? 0
        let object: [String: Any] = [
            "backend": transcript.backend ?? "merged-separate-tracks",
            "duration_seconds": transcript.durationSeconds ?? maxDuration,
            "text": trimmedText,
            "diarised_text": trimmedText,
            "sources": sourceObjects,
            "segments": segmentObjects
        ]
        return try JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys])
    }

    public static func merge(micJSONURL: URL?,
                             systemJSONURL: URL?,
                             outputTextURL: URL?,
                             outputJSONURL: URL?) throws -> MeetingTranscriptMergeResult {
        var sourcePayloads: [(source: String, payload: TranscriptJSON)] = []
        if let micJSONURL, FileManager.default.fileExists(atPath: micJSONURL.path) {
            sourcePayloads.append(("mic", try decodeTranscript(at: micJSONURL)))
        }
        if let systemJSONURL, FileManager.default.fileExists(atPath: systemJSONURL.path) {
            sourcePayloads.append(("system", try decodeTranscript(at: systemJSONURL)))
        }

        var mergedSegments: [TranscriptSegment] = []
        for (source, payload) in sourcePayloads {
            for var segment in payload.segments {
                segment.source = source
                mergedSegments.append(segment)
            }
        }
        mergedSegments.sort { lhs, rhs in
            if lhs.startTime == rhs.startTime {
                return (lhs.source ?? "") < (rhs.source ?? "")
            }
            return lhs.startTime < rhs.startTime
        }

        if mergedSegments.isEmpty {
            // Some backends may produce text but no segment array. Preserve that
            // text instead of failing hard when at least one source has content.
            for (source, payload) in sourcePayloads {
                let text = (payload.text ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
                guard !text.isEmpty else { continue }
                mergedSegments.append(TranscriptSegment(
                    text: text,
                    startTime: 0,
                    endTime: payload.durationSeconds ?? 0,
                    duration: payload.durationSeconds,
                    speakerID: nil,
                    source: source
                ))
            }
        }

        guard !mergedSegments.isEmpty else { throw TranscriptMergerError.noUsableSegments }

        let plainText = renderPlainText(segments: mergedSegments)
        let jsonData = try renderJSON(sourcePayloads: sourcePayloads, segments: mergedSegments, text: plainText)

        if let outputTextURL {
            try plainText.write(to: outputTextURL, atomically: true, encoding: .utf8)
        }
        if let outputJSONURL {
            try jsonData.write(to: outputJSONURL, options: .atomic)
        }

        return MeetingTranscriptMergeResult(
            text: plainText,
            jsonData: jsonData,
            segmentCount: mergedSegments.count,
            sources: Array(Set(mergedSegments.compactMap(\.source))).sorted()
        )
    }

    private static func decodeTranscript(at url: URL) throws -> TranscriptJSON {
        let data = try Data(contentsOf: url)
        let decoder = JSONDecoder()
        return try decoder.decode(TranscriptJSON.self, from: data)
    }

    private static func renderPlainText(segments: [TranscriptSegment]) -> String {
        segments.map { segment in
            let source = label(for: segment.source)
            let speaker: String
            if let name = segment.speakerName { speaker = " \(name)" }
            else if let id = segment.speakerID { speaker = " Speaker \(id)" }
            else { speaker = "" }
            return "[\(format(segment.startTime)) - \(format(segment.endTime))] \(source)\(speaker): \(segment.text)"
        }
        .joined(separator: "\n") + "\n"
    }

    private static func renderJSON(sourcePayloads: [(source: String, payload: TranscriptJSON)],
                                   segments: [TranscriptSegment],
                                   text: String) throws -> Data {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]

        let sourceObjects: [[String: Any]] = sourcePayloads.map { source, payload in
            var object: [String: Any] = ["source": source]
            if let audioFile = payload.audioFile { object["audio_file"] = audioFile }
            if let backend = payload.backend { object["backend"] = backend }
            if let duration = payload.durationSeconds { object["duration_seconds"] = duration }
            return object
        }

        let segmentObjects: [[String: Any]] = segments.map { segment in
            var object: [String: Any] = [
                "text": segment.text,
                "start_time": segment.startTime,
                "end_time": segment.endTime,
                "source": segment.source ?? "unknown"
            ]
            if let duration = segment.duration { object["duration"] = duration }
            if let speakerID = segment.speakerID { object["speaker_id"] = speakerID }
            if let speakerName = segment.speakerName { object["speaker_name"] = speakerName }
            return object
        }

        let maxDuration = segments.map(\.endTime).max() ?? 0
        let object: [String: Any] = [
            "backend": "merged-separate-tracks",
            "duration_seconds": maxDuration,
            "text": text.trimmingCharacters(in: .whitespacesAndNewlines),
            "diarised_text": text.trimmingCharacters(in: .whitespacesAndNewlines),
            "sources": sourceObjects,
            "segments": segmentObjects
        ]
        return try JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys])
    }

    private static func label(for source: String?) -> String {
        switch source {
        case "mic": return "Mic"
        case "system": return "System"
        case let source?: return source.capitalized
        case nil: return "Unknown"
        }
    }

    private static func format(_ value: Double) -> String {
        String(format: "%.2f", value)
    }
}
