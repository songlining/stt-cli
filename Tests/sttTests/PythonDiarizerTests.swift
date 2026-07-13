import Foundation
import Testing
@testable import sttCore

@Suite("PythonDiarizer")
struct PythonDiarizerTests {

    // MARK: - DiarizationResult decoding

    @Test func decodesRealisticDiarizeJSONPayload() throws {
        // Exact diarize.py output shape: provider/model/embeddingModel/numSpeakers/
        // distanceThreshold/segments (with speaker_id as a STRING)/speakers.
        let json = """
        {
            "distanceThreshold": 0.15,
            "embeddingModel": "speechbrain/spkrec-ecapa-voxceleb",
            "model": "speechbrain/spkrec-ecapa-voxceleb",
            "numSpeakers": 2,
            "provider": "speechbrain",
            "segments": [
                {"duration": 2.0, "end_time": 2.0, "speaker_id": "0", "start_time": 0.0, "text": "hello"},
                {"duration": 2.0, "end_time": 4.0, "speaker_id": "1", "start_time": 2.0, "text": "world"},
                {"duration": 1.5, "end_time": 5.5, "speaker_id": "0", "start_time": 4.0, "text": "again"}
            ],
            "speakers": [
                {"id": "0", "segmentCount": 2, "totalSpeechSeconds": 3.5},
                {"id": "1", "segmentCount": 1, "totalSpeechSeconds": 2.0}
            ]
        }
        """.data(using: .utf8)!

        let result = try JSONDecoder().decode(DiarizationResult.self, from: json)

        #expect(result.provider == "speechbrain")
        #expect(result.model == "speechbrain/spkrec-ecapa-voxceleb")
        #expect(result.embeddingModel == "speechbrain/spkrec-ecapa-voxceleb")
        #expect(result.numSpeakers == 2)
        #expect(result.distanceThreshold == 0.15)

        // Speaker ids are decoded as Strings.
        #expect(result.segments.map(\.speakerID) == ["0", "1", "0"])
        #expect(result.segments[0].text == "hello")
        #expect(result.segments[0].startTime == 0.0)
        #expect(result.segments[0].endTime == 2.0)
        #expect(result.segments[0].duration == 2.0)

        #expect(result.speakers.count == 2)
        #expect(result.speakers[0].id == "0")
        #expect(result.speakers[0].segmentCount == 2)
        #expect(result.speakers[0].totalSpeechSeconds == 3.5)
        #expect(result.speakers[1].id == "1")
        #expect(result.speakers[1].segmentCount == 1)
    }

    @Test func decodesNullDistanceThresholdWhenNumSpeakersForced() throws {
        // When --num-speakers is used, diarize.py sets distanceThreshold to null.
        let json = """
        {
            "distanceThreshold": null,
            "embeddingModel": "mfcc-test",
            "model": "mfcc-test",
            "numSpeakers": 3,
            "provider": "mfcc-test",
            "segments": [
                {"end_time": 1.0, "speaker_id": "0", "start_time": 0.0, "text": "a"},
                {"end_time": 2.0, "speaker_id": "1", "start_time": 1.0, "text": "b"},
                {"end_time": 3.0, "speaker_id": "2", "start_time": 2.0, "text": "c"}
            ],
            "speakers": [
                {"id": "0", "segmentCount": 1, "totalSpeechSeconds": 1.0},
                {"id": "1", "segmentCount": 1, "totalSpeechSeconds": 1.0},
                {"id": "2", "segmentCount": 1, "totalSpeechSeconds": 1.0}
            ]
        }
        """.data(using: .utf8)!

        let result = try JSONDecoder().decode(DiarizationResult.self, from: json)
        #expect(result.distanceThreshold == nil)
        #expect(result.numSpeakers == 3)
        #expect(result.segments.map(\.speakerID) == ["0", "1", "2"])
    }

    @Test func decodesNullSegmentFieldsAndWritesSpeakerIDsBack() throws {
        let diarizationJSON = """
        {
            "provider": "mfcc-test",
            "model": "mfcc-test",
            "embeddingModel": "mfcc-test",
            "numSpeakers": 2,
            "distanceThreshold": null,
            "segments": [
                {"text": "hello", "start_time": 0.0, "end_time": 2.0, "speaker_id": "0"},
                {"text": null, "start_time": null, "end_time": null, "duration": null, "speaker_id": "1"}
            ],
            "speakers": [
                {"id": "0", "segmentCount": 1, "totalSpeechSeconds": 2.0},
                {"id": "1", "segmentCount": 1, "totalSpeechSeconds": 0.0}
            ]
        }
        """.data(using: .utf8)!
        let result = try JSONDecoder().decode(DiarizationResult.self, from: diarizationJSON)
        #expect(result.segments[1].text == nil)
        #expect(result.segments[1].startTime == nil)
        #expect(result.segments[1].endTime == nil)

        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmpDir) }
        let transcriptURL = tmpDir.appendingPathComponent("transcript.json")
        try """
        {"segments":[
          {"text":"hello","start_time":0.0,"end_time":2.0},
          {"text":null,"start_time":null,"end_time":null}
        ]}
        """.data(using: .utf8)!.write(to: transcriptURL)

        try TranscriptMerger.applyDiarizationToFile(transcriptURL: transcriptURL, result: result)
        let written = try JSONDecoder().decode(TranscriptJSON.self, from: Data(contentsOf: transcriptURL))
        #expect(written.segments.map(\.speakerID) == ["0", "1"])
        #expect(written.segments[1].text == "")
        #expect(written.segments[1].startTime == 0.0)
        #expect(written.segments[1].endTime == 0.0)
    }

    // MARK: - Write-back by index (pure function)

    @Test func applyDiarizedSpeakerIDsSetsSpeakersInOrderAndPreservesFields() throws {
        let transcript = TranscriptJSON(
            audioFile: "meeting.wav",
            backend: "vibevoice",
            device: "gpu",
            modelPath: "model.mlpackage",
            durationSeconds: 5.5,
            text: "hello world again",
            diarisedText: nil,
            segments: [
                TranscriptSegment(text: "hello", startTime: 0.0, endTime: 2.0, duration: 2.0, speakerID: nil, source: "mic"),
                TranscriptSegment(text: "world", startTime: 2.0, endTime: 4.0, duration: 2.0, speakerID: nil, source: "mic"),
                TranscriptSegment(text: "again", startTime: 4.0, endTime: 5.5, duration: 1.5, speakerID: nil, source: "mic")
            ]
        )

        let result = DiarizationResult(
            provider: "speechbrain",
            model: "ecapa",
            embeddingModel: "ecapa",
            numSpeakers: 2,
            distanceThreshold: 0.15,
            segments: [
                DiarizationSegment(text: "hello", startTime: 0.0, endTime: 2.0, duration: 2.0, speakerID: "0"),
                DiarizationSegment(text: "world", startTime: 2.0, endTime: 4.0, duration: 2.0, speakerID: "1"),
                DiarizationSegment(text: "again", startTime: 4.0, endTime: 5.5, duration: 1.5, speakerID: "0")
            ],
            speakers: []
        )

        let updated = try TranscriptMerger.applyDiarizedSpeakerIDs(transcript, result: result)

        // Speaker ids applied in index order.
        #expect(updated.segments.map(\.speakerID) == ["0", "1", "0"])

        // All other top-level fields preserved.
        #expect(updated.audioFile == "meeting.wav")
        #expect(updated.backend == "vibevoice")
        #expect(updated.device == "gpu")
        #expect(updated.modelPath == "model.mlpackage")
        #expect(updated.durationSeconds == 5.5)
        #expect(updated.text == "hello world again")
        #expect(updated.diarisedText == nil)

        // Per-segment fields other than speakerID preserved.
        #expect(updated.segments[0].text == "hello")
        #expect(updated.segments[0].startTime == 0.0)
        #expect(updated.segments[0].source == "mic")
    }

    @Test func applyDiarizedSpeakerIDsThrowsOnCountMismatch() {
        let transcript = TranscriptJSON(
            segments: [
                TranscriptSegment(text: "a", startTime: 0, endTime: 1),
                TranscriptSegment(text: "b", startTime: 1, endTime: 2)
            ]
        )
        let result = DiarizationResult(
            provider: "speechbrain",
            model: nil,
            embeddingModel: nil,
            numSpeakers: 1,
            distanceThreshold: nil,
            segments: [
                DiarizationSegment(text: "a", startTime: 0, endTime: 1, duration: 1.0, speakerID: "0")
            ],
            speakers: []
        )

        #expect(throws: DiarizedSpeakerIDError.self) {
            _ = try TranscriptMerger.applyDiarizedSpeakerIDs(transcript, result: result)
        }
    }

    @Test func applyDiarizedSpeakerIDsWithEqualCountsSucceeds() throws {
        // Single-segment happy path: counts match exactly.
        let transcript = TranscriptJSON(
            segments: [TranscriptSegment(text: "solo", startTime: 0, endTime: 1)]
        )
        let result = DiarizationResult(
            provider: "speechbrain",
            model: nil,
            embeddingModel: nil,
            numSpeakers: 1,
            distanceThreshold: nil,
            segments: [DiarizationSegment(text: "solo", startTime: 0, endTime: 1, duration: 1.0, speakerID: "0")],
            speakers: []
        )

        let updated = try TranscriptMerger.applyDiarizedSpeakerIDs(transcript, result: result)
        #expect(updated.segments.map(\.speakerID) == ["0"])
    }
}
