import Foundation
import Testing
@testable import sttCore

@Suite("Transcript merger")
struct TranscriptMergerTests {
    @Test func mergesMicAndSystemSegmentsChronologicallyWithSourceLabels() throws {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let micURL = tmpDir.appendingPathComponent("mic.json")
        let systemURL = tmpDir.appendingPathComponent("system.json")
        let outputTextURL = tmpDir.appendingPathComponent("transcript.md")
        let outputJSONURL = tmpDir.appendingPathComponent("transcript.json")

        try writeTranscript(
            TranscriptJSON(
                audioFile: "mic.wav",
                backend: "fake",
                durationSeconds: 4,
                text: "测试",
                segments: [TranscriptSegment(text: "测试", startTime: 1.0, endTime: 2.0, duration: 1.0, speakerID: 0)]
            ),
            to: micURL
        )
        try writeTranscript(
            TranscriptJSON(
                audioFile: "system.wav",
                backend: "fake",
                durationSeconds: 4,
                text: "hello",
                segments: [TranscriptSegment(text: "hello", startTime: 0.5, endTime: 1.5, duration: 1.0, speakerID: 1)]
            ),
            to: systemURL
        )

        let result = try TranscriptMerger.merge(
            micJSONURL: micURL,
            systemJSONURL: systemURL,
            outputTextURL: outputTextURL,
            outputJSONURL: outputJSONURL
        )

        #expect(result.segmentCount == 2)
        #expect(result.sources == ["mic", "system"])
        let text = try String(contentsOf: outputTextURL, encoding: .utf8)
        #expect(text.contains("[0.50 - 1.50] System Speaker 1: hello"))
        #expect(text.contains("[1.00 - 2.00] Mic Speaker 0: 测试"))
        #expect(text.range(of: "System")!.lowerBound < text.range(of: "Mic")!.lowerBound)

        let json = try JSONSerialization.jsonObject(with: Data(contentsOf: outputJSONURL)) as? [String: Any]
        let segments = try #require(json?["segments"] as? [[String: Any]])
        #expect(segments[0]["source"] as? String == "system")
        #expect(segments[1]["source"] as? String == "mic")
    }

    @Test func fallsBackToTextWhenBackendHasNoSegments() throws {
        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let micURL = tmpDir.appendingPathComponent("mic.json")
        try writeTranscript(
            TranscriptJSON(audioFile: "mic.wav", durationSeconds: 3, text: "plain mic text", segments: []),
            to: micURL
        )

        let result = try TranscriptMerger.merge(micJSONURL: micURL, systemJSONURL: nil, outputTextURL: nil, outputJSONURL: nil)

        #expect(result.segmentCount == 1)
        #expect(result.text.contains("Mic: plain mic text"))
    }

    private func writeTranscript(_ transcript: TranscriptJSON, to url: URL) throws {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        try encoder.encode(transcript).write(to: url)
    }
}
