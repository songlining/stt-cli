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
    public var speakerID: Int?
    public var source: String?

    public init(text: String,
                startTime: Double,
                endTime: Double,
                duration: Double? = nil,
                speakerID: Int? = nil,
                source: String? = nil) {
        self.text = text
        self.startTime = startTime
        self.endTime = endTime
        self.duration = duration
        self.speakerID = speakerID
        self.source = source
    }

    enum CodingKeys: String, CodingKey {
        case text
        case startTime = "start_time"
        case endTime = "end_time"
        case duration
        case speakerID = "speaker_id"
        case source
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

public enum TranscriptMerger {
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
            let speaker = segment.speakerID.map { " Speaker \($0)" } ?? ""
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
