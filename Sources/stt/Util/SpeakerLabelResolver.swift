import Foundation

/// One diarized speaker's extraction + match results, as input to
/// `SpeakerLabelResolver.resolve(...)`. Assembled by the CLI layer after
/// running `PythonSpeakerIdentifier` for each diarized `speaker_id` found in
/// a transcript.
public struct SpeakerCandidateInput {
    public let speakerId: String
    /// Extraction result for this speaker's concatenated segments. `nil` if
    /// extraction was never attempted (e.g. diarization present but this
    /// speaker had zero usable segments).
    public let extraction: SpeakerExtractionResult?
    /// Match result against enrolled profiles. `nil` when extraction was
    /// too short (or missing) and matching was therefore skipped.
    public let matchResult: SpeakerMatchResult?

    public init(speakerId: String, extraction: SpeakerExtractionResult?, matchResult: SpeakerMatchResult?) {
        self.speakerId = speakerId
        self.extraction = extraction
        self.matchResult = matchResult
    }
}

/// Final speaker identity decision for one diarized speaker, as documented
/// in the "Speaker identity output" contract in
/// `SPEAKER_IDENTIFICATION_PLAN.md`.
public struct SpeakerLabelAssignment: Codable, Equatable {
    public let displayName: String
    public let profileId: String?
    public let confidence: Double?
    public let margin: Double?
    public let matchStatus: String

    public init(displayName: String, profileId: String?, confidence: Double?, margin: Double?, matchStatus: String) {
        self.displayName = displayName
        self.profileId = profileId
        self.confidence = confidence
        self.margin = margin
        self.matchStatus = matchStatus
    }

    static func anonymous(speakerId: String, status: String, confidence: Double? = nil, margin: Double? = nil) -> SpeakerLabelAssignment {
        SpeakerLabelAssignment(
            displayName: "Speaker \(speakerId)",
            profileId: nil,
            confidence: confidence,
            margin: margin,
            matchStatus: status
        )
    }
}

/// Match status strings used in `SpeakerLabelAssignment.matchStatus`. These
/// mirror the conflict-policy bullet list in
/// `SPEAKER_IDENTIFICATION_PLAN.md` "Speaker identity output" section.
public enum SpeakerMatchStatus {
    public static let matched = "matched"
    public static let noProfiles = "no_profiles"
    public static let noDiarization = "no_diarization"
    public static let tooShort = "too_short"
    public static let belowThreshold = "below_threshold"
    public static let ambiguous = "ambiguous"
    public static let duplicateProfileMatch = "duplicate_profile_match"
    /// Extension beyond the plan's enumerated statuses: every enrolled
    /// profile was skipped for this candidate (e.g. provider/model
    /// mismatch for all profiles), so there was nothing left to score.
    public static let noComparableProfiles = "no_comparable_profiles"
}

/// Pure, side-effect-free implementation of the speaker identification
/// conflict policy documented in `SPEAKER_IDENTIFICATION_PLAN.md`. Takes
/// already-computed Python extraction/match results (one per diarized
/// speaker) and decides final display names, resolving duplicate-profile
/// conflicts across speakers.
public enum SpeakerLabelResolver {

    /// Resolves final speaker label assignments for every diarized speaker.
    ///
    /// - Parameters:
    ///   - candidates: one entry per diarized speaker id found in a
    ///     transcript.
    ///   - hasProfiles: whether any speaker profiles are enrolled at all.
    ///     When `false`, every speaker stays anonymous with `no_profiles`,
    ///     regardless of `candidates`.
    /// - Returns: a `[speakerId: SpeakerLabelAssignment]` map covering every
    ///   speaker id present in `candidates`.
    public static func resolve(candidates: [SpeakerCandidateInput], hasProfiles: Bool) -> [String: SpeakerLabelAssignment] {
        guard hasProfiles else {
            var result: [String: SpeakerLabelAssignment] = [:]
            for candidate in candidates {
                result[candidate.speakerId] = .anonymous(speakerId: candidate.speakerId, status: SpeakerMatchStatus.noProfiles)
            }
            return result
        }

        var tentative: [String: SpeakerLabelAssignment] = [:]

        for candidate in candidates {
            tentative[candidate.speakerId] = tentativeAssignment(for: candidate)
        }

        return resolveDuplicateProfileMatches(tentative)
    }

    private static func tentativeAssignment(for candidate: SpeakerCandidateInput) -> SpeakerLabelAssignment {
        guard let extraction = candidate.extraction, extraction.isOK else {
            return .anonymous(speakerId: candidate.speakerId, status: SpeakerMatchStatus.tooShort)
        }

        guard let matchResult = candidate.matchResult else {
            return .anonymous(speakerId: candidate.speakerId, status: SpeakerMatchStatus.tooShort)
        }

        guard let bestMatch = matchResult.bestMatch else {
            return .anonymous(speakerId: candidate.speakerId, status: SpeakerMatchStatus.noComparableProfiles)
        }

        if bestMatch.matched {
            return SpeakerLabelAssignment(
                displayName: bestMatch.displayName ?? "Speaker \(candidate.speakerId)",
                profileId: bestMatch.profileId,
                confidence: bestMatch.confidence,
                margin: bestMatch.margin,
                matchStatus: SpeakerMatchStatus.matched
            )
        }

        return .anonymous(
            speakerId: candidate.speakerId,
            status: bestMatch.status,
            confidence: bestMatch.confidence,
            margin: bestMatch.margin
        )
    }

    /// If two or more diarized speakers matched the same profile, only the
    /// highest-confidence speaker keeps that profile assignment; the rest
    /// revert to anonymous with `duplicate_profile_match`.
    private static func resolveDuplicateProfileMatches(_ assignments: [String: SpeakerLabelAssignment]) -> [String: SpeakerLabelAssignment] {
        var byProfileID: [String: [(speakerId: String, assignment: SpeakerLabelAssignment)]] = [:]
        for (speakerId, assignment) in assignments where assignment.matchStatus == SpeakerMatchStatus.matched {
            guard let profileId = assignment.profileId else { continue }
            byProfileID[profileId, default: []].append((speakerId, assignment))
        }

        var result = assignments
        for (_, matches) in byProfileID where matches.count > 1 {
            let sorted = matches.sorted { ($0.assignment.confidence ?? 0) > ($1.assignment.confidence ?? 0) }
            for loser in sorted.dropFirst() {
                result[loser.speakerId] = .anonymous(
                    speakerId: loser.speakerId,
                    status: SpeakerMatchStatus.duplicateProfileMatch,
                    confidence: loser.assignment.confidence,
                    margin: loser.assignment.margin
                )
            }
        }
        return result
    }
}
