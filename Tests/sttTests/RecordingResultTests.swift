import Foundation
import Testing
@testable import sttCore

@Suite("RecordingResult diagnostics")
struct RecordingResultTests {

    @Test func largeFilesAreTreatedAsLikelyContainingAudio() {
        let result = RecordingResult(
            outputURL: URL(fileURLWithPath: "/tmp/audio.wav"),
            durationSeconds: 2,
            fileSizeBytes: 180_000
        )

        #expect(result.likelyContainsAudioData)
        #expect(result.emptyAudioWarning == nil)
    }

    @Test func headerOnlyFilesProduceWarning() {
        let result = RecordingResult(
            outputURL: URL(fileURLWithPath: "/tmp/header-only.wav"),
            durationSeconds: 2,
            fileSizeBytes: 4096
        )

        #expect(result.likelyContainsAudioData == false)
        #expect(result.emptyAudioWarning?.contains("no audio frames") == true)
        #expect(result.emptyAudioWarning?.contains("4096 bytes") == true)
    }

    @Test func unknownFileSizeDoesNotWarn() {
        let result = RecordingResult(
            outputURL: URL(fileURLWithPath: "/tmp/unknown.wav"),
            durationSeconds: 2,
            fileSizeBytes: nil
        )

        #expect(result.likelyContainsAudioData)
        #expect(result.emptyAudioWarning == nil)
    }
}
