import Foundation
import Testing
@testable import sttCore

@Suite("SpeakerLabelResolver conflict policy")
struct SpeakerLabelResolverTests {

    private func okExtraction(speakerId: String) -> SpeakerExtractionResult {
        SpeakerExtractionResult(
            speakerId: speakerId,
            provider: "mfcc-test",
            model: "stt-vibevoice/mfcc-test-v1",
            embedding: [1.0, 0.0],
            durationSeconds: 10.0,
            segmentCount: 2,
            status: "ok"
        )
    }

    private func tooShortExtraction(speakerId: String) -> SpeakerExtractionResult {
        SpeakerExtractionResult(
            speakerId: speakerId,
            provider: "mfcc-test",
            model: nil,
            embedding: nil,
            durationSeconds: 2.0,
            segmentCount: 1,
            status: "too_short"
        )
    }

    private func matchResult(best: SpeakerMatchBestMatch?) -> SpeakerMatchResult {
        SpeakerMatchResult(bestMatch: best, candidates: [], skippedProfiles: [], warnings: [])
    }

    @Test func noProfilesKeepsAllSpeakersAnonymous() {
        let candidates = [
            SpeakerCandidateInput(speakerId: "0", extraction: okExtraction(speakerId: "0"), matchResult: nil),
            SpeakerCandidateInput(speakerId: "1", extraction: okExtraction(speakerId: "1"), matchResult: nil)
        ]
        let result = SpeakerLabelResolver.resolve(candidates: candidates, hasProfiles: false)

        #expect(result["0"]?.matchStatus == SpeakerMatchStatus.noProfiles)
        #expect(result["0"]?.profileId == nil)
        #expect(result["1"]?.matchStatus == SpeakerMatchStatus.noProfiles)
    }

    @Test func tooShortSpeechStaysAnonymous() {
        let candidates = [
            SpeakerCandidateInput(speakerId: "0", extraction: tooShortExtraction(speakerId: "0"), matchResult: nil)
        ]
        let result = SpeakerLabelResolver.resolve(candidates: candidates, hasProfiles: true)

        #expect(result["0"]?.matchStatus == SpeakerMatchStatus.tooShort)
        #expect(result["0"]?.profileId == nil)
    }

    @Test func highConfidenceMatchRelabelsSpeaker() {
        let best = SpeakerMatchBestMatch(profileId: "profile-a", displayName: "Larry", confidence: 0.95, margin: 0.2, matched: true, status: "matched")
        let candidates = [
            SpeakerCandidateInput(speakerId: "0", extraction: okExtraction(speakerId: "0"), matchResult: matchResult(best: best))
        ]
        let result = SpeakerLabelResolver.resolve(candidates: candidates, hasProfiles: true)

        #expect(result["0"]?.displayName == "Larry")
        #expect(result["0"]?.profileId == "profile-a")
        #expect(result["0"]?.matchStatus == SpeakerMatchStatus.matched)
    }

    @Test func belowThresholdStaysAnonymous() {
        let best = SpeakerMatchBestMatch(profileId: "profile-a", displayName: "Larry", confidence: 0.5, margin: 0.3, matched: false, status: "below_threshold")
        let candidates = [
            SpeakerCandidateInput(speakerId: "0", extraction: okExtraction(speakerId: "0"), matchResult: matchResult(best: best))
        ]
        let result = SpeakerLabelResolver.resolve(candidates: candidates, hasProfiles: true)

        #expect(result["0"]?.matchStatus == SpeakerMatchStatus.belowThreshold)
        #expect(result["0"]?.profileId == nil)
        #expect(result["0"]?.displayName == "Speaker 0")
    }

    @Test func ambiguousMarginStaysAnonymous() {
        let best = SpeakerMatchBestMatch(profileId: "profile-a", displayName: "Larry", confidence: 0.9, margin: 0.01, matched: false, status: "ambiguous")
        let candidates = [
            SpeakerCandidateInput(speakerId: "0", extraction: okExtraction(speakerId: "0"), matchResult: matchResult(best: best))
        ]
        let result = SpeakerLabelResolver.resolve(candidates: candidates, hasProfiles: true)

        #expect(result["0"]?.matchStatus == SpeakerMatchStatus.ambiguous)
        #expect(result["0"]?.profileId == nil)
    }

    @Test func duplicateProfileMatchKeepsOnlyHighestConfidenceSpeaker() {
        let bestForSpeaker0 = SpeakerMatchBestMatch(profileId: "profile-a", displayName: "Larry", confidence: 0.95, margin: 0.2, matched: true, status: "matched")
        let bestForSpeaker1 = SpeakerMatchBestMatch(profileId: "profile-a", displayName: "Larry", confidence: 0.85, margin: 0.15, matched: true, status: "matched")
        let candidates = [
            SpeakerCandidateInput(speakerId: "0", extraction: okExtraction(speakerId: "0"), matchResult: matchResult(best: bestForSpeaker0)),
            SpeakerCandidateInput(speakerId: "1", extraction: okExtraction(speakerId: "1"), matchResult: matchResult(best: bestForSpeaker1))
        ]
        let result = SpeakerLabelResolver.resolve(candidates: candidates, hasProfiles: true)

        #expect(result["0"]?.matchStatus == SpeakerMatchStatus.matched)
        #expect(result["0"]?.profileId == "profile-a")
        #expect(result["1"]?.matchStatus == SpeakerMatchStatus.duplicateProfileMatch)
        #expect(result["1"]?.profileId == nil)
        #expect(result["1"]?.displayName == "Speaker 1")
    }

    @Test func distinctProfilesForDifferentSpeakersBothMatch() {
        let bestForSpeaker0 = SpeakerMatchBestMatch(profileId: "profile-a", displayName: "Larry", confidence: 0.95, margin: 0.2, matched: true, status: "matched")
        let bestForSpeaker1 = SpeakerMatchBestMatch(profileId: "profile-b", displayName: "Jamie", confidence: 0.9, margin: 0.2, matched: true, status: "matched")
        let candidates = [
            SpeakerCandidateInput(speakerId: "0", extraction: okExtraction(speakerId: "0"), matchResult: matchResult(best: bestForSpeaker0)),
            SpeakerCandidateInput(speakerId: "1", extraction: okExtraction(speakerId: "1"), matchResult: matchResult(best: bestForSpeaker1))
        ]
        let result = SpeakerLabelResolver.resolve(candidates: candidates, hasProfiles: true)

        #expect(result["0"]?.profileId == "profile-a")
        #expect(result["1"]?.profileId == "profile-b")
    }

    @Test func noComparableProfilesStaysAnonymous() {
        let candidates = [
            SpeakerCandidateInput(speakerId: "0", extraction: okExtraction(speakerId: "0"), matchResult: matchResult(best: nil))
        ]
        let result = SpeakerLabelResolver.resolve(candidates: candidates, hasProfiles: true)

        #expect(result["0"]?.matchStatus == SpeakerMatchStatus.noComparableProfiles)
        #expect(result["0"]?.profileId == nil)
    }
}
