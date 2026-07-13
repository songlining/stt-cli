import Foundation
import ArgumentParser
import sttCore

struct CheckFailure: Error, CustomStringConvertible {
    let message: String
    var description: String { message }
}

func check(_ condition: @autoclosure () -> Bool, _ message: String) throws {
    if !condition() { throw CheckFailure(message: message) }
}

func checkEqual<T: Equatable>(_ actual: T, _ expected: T, _ message: String) throws {
    if actual != expected {
        throw CheckFailure(message: "\(message): expected \(expected), got \(actual)")
    }
}

func rms(_ samples: [Int16]) -> Double {
    let audible = samples.map { abs(Double($0)) }.filter { $0 >= 32.0 }
    guard !audible.isEmpty else { return 0 }
    let sumSquares = audible.reduce(0.0) { $0 + ($1 * $1) }
    return (sumSquares / Double(audible.count)).squareRoot()
}

func peakMagnitude(_ samples: [Int16]) -> Int {
    samples.map { abs(Int($0)) }.max() ?? 0
}

func runChecks() throws {
    let doctor = try Doctor.parse(["--python-backend", "/tmp/backend", "--require-backend-ready"])
    try checkEqual(doctor.pythonBackend, "/tmp/backend", "doctor python backend parses")
    try check(doctor.requireBackendReady, "doctor require-backend-ready parses")

    let resetHelp = try Permissions.ResetHelp.parse(["--bundle-id", "com.example.stt"])
    try checkEqual(resetHelp.bundleID, "com.example.stt", "permissions reset-help bundle-id parses")

    let record = try Record.parse(["--mode", "mic", "--output", "foo.wav", "--duration", "1.5", "--fail-if-empty"])
    try checkEqual(record.mode, .mic, "record mode parses")
    try checkEqual(record.output, "foo.wav", "record output parses")
    try checkEqual(record.duration, 1.5, "record duration parses")
    try check(record.failIfEmpty, "record fail-if-empty parses")

    let system = try Record.parse(["--mode", "system", "--input-device", "BlackHole 2ch"])
    try checkEqual(system.mode, .system, "system mode parses")
    try checkEqual(system.inputDevice, "BlackHole 2ch", "input device parses")

    let fixtureDevices = [
        AudioDeviceInfo(id: 1, name: "External BlackHole Monitor", uid: "one", inputChannelCount: 2, isDefaultInput: false),
        AudioDeviceInfo(id: 2, name: "BlackHole 2ch", uid: "two", inputChannelCount: 2, isDefaultInput: false),
        AudioDeviceInfo(id: 3, name: "MacBook Pro Microphone", uid: "three", inputChannelCount: 1, isDefaultInput: true)
    ]
    try checkEqual(try DeviceList.selectInputDevice(named: "BlackHole 2ch", from: fixtureDevices).id, 2, "device exact match wins")
    try checkEqual(try DeviceList.selectInputDevice(named: "blackhole", from: fixtureDevices).id, 1, "device substring match is case-insensitive and ordered")
    do {
        _ = try DeviceList.selectInputDevice(named: "Missing Device", from: fixtureDevices)
        throw CheckFailure(message: "missing input device selection should fail")
    } catch DeviceListError.deviceNotFound(let name) {
        try checkEqual(name, "Missing Device", "device not found preserves requested name")
    }
    try checkEqual(try SystemAudioRecorder.selectFallbackDevice(named: "BlackHole", from: fixtureDevices).id, 1, "explicit fallback device selection")
    try checkEqual(try SystemAudioRecorder.selectFallbackDevice(named: nil, from: fixtureDevices).id, 2, "default fallback device candidate order")
    do {
        _ = try SystemAudioRecorder.selectFallbackDevice(named: nil, from: [fixtureDevices[2]])
        throw CheckFailure(message: "missing fallback candidates should fail")
    } catch SystemAudioRecorderError.noFallbackDeviceConfigured {
        // expected
    }

    let transcribe = try Transcribe.parse([
        "meeting.wav", "--output", "out.txt", "--json", "out.json", "--device", "gpu",
        "--timeout", "30", "--model", "custom/model", "--max-new-tokens", "2048",
        "--python-backend", "/tmp/backend", "--require-backend-ready"
    ])
    try checkEqual(transcribe.audioPath, "meeting.wav", "transcribe audio path parses")
    try checkEqual(transcribe.output, "out.txt", "transcribe output parses")
    try checkEqual(transcribe.json, "out.json", "transcribe json parses")
    try checkEqual(transcribe.device, .gpu, "transcribe device parses")
    try checkEqual(transcribe.timeout, 30, "transcribe timeout parses")
    try checkEqual(transcribe.model, "custom/model", "transcribe model parses")
    try checkEqual(transcribe.maxNewTokens, 2048, "transcribe max-new-tokens parses")
    try checkEqual(transcribe.pythonBackend, "/tmp/backend", "transcribe python backend parses")
    try check(transcribe.requireBackendReady, "transcribe require-backend-ready parses")

    let pipeline = try Pipeline.parse([
        "--mode", "meeting", "--name", "Customer Call", "--input-device", "BlackHole 2ch",
        "--duration", "5", "--fail-if-empty", "--transcribe-timeout", "120", "--device", "cpu",
        "--model", "custom/model", "--max-new-tokens", "2048", "--python-backend", "/tmp/backend",
        "--require-backend-ready"
    ])
    let mix = try Mix.parse(["mic.wav", "system.wav", "--output", "mixed.wav", "--fail-if-empty"])
    try checkEqual(mix.firstAudioPath, "mic.wav", "mix first input parses")
    try checkEqual(mix.secondAudioPath, "system.wav", "mix second input parses")
    try checkEqual(mix.output, "mixed.wav", "mix output parses")
    try check(mix.failIfEmpty, "mix fail-if-empty parses")

    try checkEqual(pipeline.mode, .meeting, "pipeline mode parses")
    try checkEqual(pipeline.name, "Customer Call", "pipeline name parses")
    try checkEqual(pipeline.inputDevice, "BlackHole 2ch", "pipeline input device parses")
    try checkEqual(pipeline.duration, 5, "pipeline duration parses")
    try check(pipeline.failIfEmpty, "pipeline fail-if-empty parses")
    try checkEqual(pipeline.transcribeTimeout, 120, "pipeline timeout parses")
    try checkEqual(pipeline.device, .cpu, "pipeline transcribe device parses")
    try checkEqual(pipeline.model, "custom/model", "pipeline model parses")
    try checkEqual(pipeline.maxNewTokens, 2048, "pipeline max-new-tokens parses")
    try checkEqual(pipeline.pythonBackend, "/tmp/backend", "pipeline python backend parses")
    try check(pipeline.requireBackendReady, "pipeline require-backend-ready parses")

    let diarize = try Diarize.parse([
        "--audio", "meeting.wav",
        "--transcript", "transcript.json",
        "--provider", "speechbrain",
        "--num-speakers", "3",
        "--min-speech-seconds", "1.5",
        "--python-backend", "/tmp/backend"
    ])
    try checkEqual(diarize.audio, "meeting.wav", "diarize audio parses")
    try checkEqual(diarize.transcript, "transcript.json", "diarize transcript parses")
    try checkEqual(diarize.provider, "speechbrain", "diarize provider parses")
    try checkEqual(diarize.numSpeakers, 3, "diarize num-speakers parses")
    try checkEqual(diarize.minSpeechSeconds, 1.5, "diarize min-speech-seconds parses")
    try checkEqual(diarize.pythonBackend, "/tmp/backend", "diarize python backend parses")
    do {
        _ = try Diarize.parse(["--audio", "a.wav", "--transcript", "t.json", "--num-speakers", "2", "--distance-threshold", "0.2"])
        throw CheckFailure(message: "diarize mutually exclusive flags should be rejected")
    } catch {
        // Expected: argparse rejects mutually exclusive flags.
    }

    // DiarizationResult JSON decoding.
    let diarizeJSON = """
    {"distanceThreshold":0.15,"embeddingModel":"ecapa","model":"ecapa","numSpeakers":2,"provider":"speechbrain","segments":[{"duration":2.0,"end_time":2.0,"speaker_id":"0","start_time":0.0,"text":"hi"},{"duration":2.0,"end_time":4.0,"speaker_id":"1","start_time":2.0,"text":"there"}],"speakers":[{"id":"0","segmentCount":1,"totalSpeechSeconds":2.0},{"id":"1","segmentCount":1,"totalSpeechSeconds":2.0}]}
    """.data(using: .utf8)!
    let diarizeResult = try JSONDecoder().decode(DiarizationResult.self, from: diarizeJSON)
    try checkEqual(diarizeResult.numSpeakers, 2, "diarization result numSpeakers decodes")
    try checkEqual(diarizeResult.segments.map(\.speakerID), ["0", "1"], "diarization speaker ids decode as strings")
    try checkEqual(diarizeResult.speakers[0].id, "0", "diarization speaker summary id decodes")

    // Pure write-back by index preserves other fields and applies speaker ids.
    let writeBackTranscript = TranscriptJSON(
        audioFile: "meeting.wav",
        backend: "vibevoice",
        durationSeconds: 4.0,
        text: "hi there",
        segments: [
            TranscriptSegment(text: "hi", startTime: 0, endTime: 2, duration: 2.0, speakerID: nil),
            TranscriptSegment(text: "there", startTime: 2, endTime: 4, duration: 2.0, speakerID: nil)
        ]
    )
    let writeBackResult = DiarizationResult(
        provider: "speechbrain",
        model: "ecapa",
        embeddingModel: "ecapa",
        numSpeakers: 2,
        distanceThreshold: 0.15,
        segments: [
            DiarizationSegment(text: "hi", startTime: 0, endTime: 2, duration: 2.0, speakerID: "0"),
            DiarizationSegment(text: "there", startTime: 2, endTime: 4, duration: 2.0, speakerID: "1")
        ],
        speakers: []
    )
    let applied = try TranscriptMerger.applyDiarizedSpeakerIDs(writeBackTranscript, result: writeBackResult)
    try checkEqual(applied.segments.map(\.speakerID), ["0", "1"], "write-back applies speaker ids in order")
    try checkEqual(applied.audioFile, "meeting.wav", "write-back preserves audio_file")
    try checkEqual(applied.backend, "vibevoice", "write-back preserves backend")
    try checkEqual(applied.text, "hi there", "write-back preserves text")
    // Count mismatch must throw.
    let shortResult = DiarizationResult(provider: "p", model: nil, embeddingModel: nil, numSpeakers: 1, distanceThreshold: nil, segments: [DiarizationSegment(text: "hi", startTime: 0, endTime: 1, duration: 1.0, speakerID: "0")], speakers: [])
    do {
        _ = try TranscriptMerger.applyDiarizedSpeakerIDs(writeBackTranscript, result: shortResult)
        throw CheckFailure(message: "write-back count mismatch should throw")
    } catch {
        // Expected.
    }

    // MARK: - applyDiarizationToFile round-trip (read -> apply -> write -> read)
    let applyFileDir = FileManager.default.temporaryDirectory.appendingPathComponent("stt-checks-\(UUID().uuidString)", isDirectory: true)
    try FileManager.default.createDirectory(at: applyFileDir, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: applyFileDir) }
    let applyFileURL = applyFileDir.appendingPathComponent("transcript.json")
    let applyFileTranscript = TranscriptJSON(
        audioFile: "roundtrip.wav",
        backend: "vibevoice",
        durationSeconds: 6.0,
        text: "one two",
        segments: [
            TranscriptSegment(text: "one", startTime: 0, endTime: 3, duration: 3.0, speakerID: nil),
            TranscriptSegment(text: "two", startTime: 3, endTime: 6, duration: 3.0, speakerID: nil)
        ]
    )
    let applyFileEncoder = JSONEncoder()
    applyFileEncoder.outputFormatting = [.prettyPrinted, .sortedKeys]
    try applyFileEncoder.encode(applyFileTranscript).write(to: applyFileURL)
    let applyFileResult = DiarizationResult(
        provider: "speechbrain",
        model: "ecapa",
        embeddingModel: "ecapa",
        numSpeakers: 2,
        distanceThreshold: nil,
        segments: [
            DiarizationSegment(text: "one", startTime: 0, endTime: 3, duration: 3.0, speakerID: "5"),
            DiarizationSegment(text: "two", startTime: 3, endTime: 6, duration: 3.0, speakerID: "6")
        ],
        speakers: [
            DiarizationSpeakerSummary(id: "5", segmentCount: 1, totalSpeechSeconds: 3.0),
            DiarizationSpeakerSummary(id: "6", segmentCount: 1, totalSpeechSeconds: 3.0)
        ]
    )
    try TranscriptMerger.applyDiarizationToFile(transcriptURL: applyFileURL, result: applyFileResult)
    let roundTripped = try JSONDecoder().decode(TranscriptJSON.self, from: Data(contentsOf: applyFileURL))
    try checkEqual(roundTripped.segments.map(\.speakerID), ["5", "6"], "applyDiarizationToFile writes speaker ids by index")
    try checkEqual(roundTripped.audioFile, "roundtrip.wav", "applyDiarizationToFile preserves audio_file")
    try checkEqual(roundTripped.backend, "vibevoice", "applyDiarizationToFile preserves backend")
    try checkEqual(roundTripped.text, "one two", "applyDiarizationToFile preserves text")
    try checkEqual(roundTripped.segments.count, 2, "applyDiarizationToFile preserves segment count")

    // MARK: - MeetingDiarizationConfig is constructable
    let meetingDiagConfig = MeetingDiarizationConfig(provider: "mfcc-test", numSpeakers: 3, distanceThreshold: nil, workingDirectory: nil)
    try checkEqual(meetingDiagConfig.provider, "mfcc-test", "MeetingDiarizationConfig provider")
    try checkEqual(meetingDiagConfig.numSpeakers, 3, "MeetingDiarizationConfig numSpeakers")
    try checkEqual(meetingDiagConfig.distanceThreshold, nil, "MeetingDiarizationConfig distanceThreshold nil")
    try checkEqual(meetingDiagConfig.workingDirectory, nil, "MeetingDiarizationConfig workingDirectory nil")

    // MARK: - transcribe-meeting --diarize CLI parsing
    let meetingWithDiarize = try TranscribeMeeting.parse([
        "mic.wav", "system.wav", "--diarize",
        "--diarize-num-speakers", "2", "--diarize-provider", "mfcc-test"
    ])
    try check(meetingWithDiarize.diarize, "transcribe-meeting --diarize parses")
    try checkEqual(meetingWithDiarize.diarizeNumSpeakers, 2, "transcribe-meeting --diarize-num-speakers parses")
    try checkEqual(meetingWithDiarize.diarizeProvider, "mfcc-test", "transcribe-meeting --diarize-provider parses")
    let meetingMutuallyExclusive = try TranscribeMeeting.parse([
        "mic.wav", "system.wav", "--diarize-num-speakers", "2", "--diarize-distance-threshold", "0.2"
    ])
    do {
        try meetingMutuallyExclusive.run()
        throw CheckFailure(message: "transcribe-meeting mutually exclusive diarize flags should be rejected")
    } catch {
        try check("\(error)".contains("mutually exclusive"), "transcribe-meeting rejects mutually exclusive diarize flags")
    }

    // MARK: - pipeline --diarize CLI parsing
    let pipelineWithDiarize = try Pipeline.parse([
        "--mode", "meeting", "--diarize",
        "--diarize-distance-threshold", "0.2", "--diarize-provider", "mfcc-test"
    ])
    try check(pipelineWithDiarize.diarize, "pipeline --diarize parses")
    try checkEqual(pipelineWithDiarize.diarizeDistanceThreshold, 0.2, "pipeline --diarize-distance-threshold parses")
    try checkEqual(pipelineWithDiarize.diarizeProvider, "mfcc-test", "pipeline --diarize-provider parses")
    let pipelineMutuallyExclusive = try Pipeline.parse([
        "--mode", "meeting", "--diarize-num-speakers", "2", "--diarize-distance-threshold", "0.2"
    ])
    do {
        try pipelineMutuallyExclusive.run()
        throw CheckFailure(message: "pipeline mutually exclusive diarize flags should be rejected")
    } catch {
        try check("\(error)".contains("mutually exclusive"), "pipeline rejects mutually exclusive diarize flags")
    }

    let env = ["STT_HOME": "/tmp/stt-test-home"]
    try checkEqual(Paths.appSupportDirectory(environment: env).path, "/tmp/stt-test-home", "STT_HOME override works")
    try checkEqual(Paths.recordingsDirectory(environment: env).path, "/tmp/stt-test-home/recordings", "recordings dir nested")

    let filePreflightDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: filePreflightDir) }
    try FileManager.default.createDirectory(at: filePreflightDir, withIntermediateDirectories: true)
    let existingAudioFile = filePreflightDir.appendingPathComponent("audio.wav")
    FileManager.default.createFile(atPath: existingAudioFile.path, contents: Data("x".utf8))
    try checkEqual(try Paths.requireExistingFile(existingAudioFile.path), existingAudioFile, "existing file preflight returns URL")
    let missingAudioFile = filePreflightDir.appendingPathComponent("missing.wav")
    do {
        try Paths.requireExistingFile(missingAudioFile.path)
        throw CheckFailure(message: "missing file preflight should fail")
    } catch PathsError.fileNotFound(let path) {
        try checkEqual(path, missingAudioFile.path, "missing file preflight preserves path")
    }
    do {
        try Paths.requireExistingFile(filePreflightDir.path)
        throw CheckFailure(message: "directory-as-file preflight should fail")
    } catch PathsError.notAFile(let path) {
        try checkEqual(path, filePreflightDir.path, "directory-as-file preflight preserves path")
    }
    try checkEqual(try Paths.requireNonEmptyFile(existingAudioFile.path), existingAudioFile, "non-empty file preflight returns URL")
    let emptyAudioFile = filePreflightDir.appendingPathComponent("empty.wav")
    FileManager.default.createFile(atPath: emptyAudioFile.path, contents: Data())
    do {
        try Paths.requireNonEmptyFile(emptyAudioFile.path)
        throw CheckFailure(message: "empty file preflight should fail")
    } catch PathsError.emptyFile(let path) {
        try checkEqual(path, emptyAudioFile.path, "empty file preflight preserves path")
    }

    let transcribeMissingAudio = try Transcribe.parse([missingAudioFile.path])
    do {
        try transcribeMissingAudio.run()
        throw CheckFailure(message: "transcribe missing audio should fail before backend launch")
    } catch {
        try check(error.localizedDescription.contains("Audio file not found: \(missingAudioFile.path)"), "transcribe missing audio preflight message")
    }
    let transcribeEmptyAudio = try Transcribe.parse([emptyAudioFile.path])
    do {
        try transcribeEmptyAudio.run()
        throw CheckFailure(message: "transcribe empty audio should fail before backend launch")
    } catch {
        try check(error.localizedDescription.contains("Audio file is empty (0 bytes): \(emptyAudioFile.path)"), "transcribe empty audio preflight message")
    }
    let mixMissingAudio = try Mix.parse([missingAudioFile.path, missingAudioFile.path])
    do {
        try mixMissingAudio.run()
        throw CheckFailure(message: "mix missing input should fail before mixing")
    } catch {
        try check(error.localizedDescription.contains("Audio file not found: \(missingAudioFile.path)"), "mix missing input preflight message")
    }
    try Pipeline.requireTranscribableAudio(at: existingAudioFile)
    do {
        try Pipeline.requireTranscribableAudio(at: emptyAudioFile)
        throw CheckFailure(message: "pipeline empty transcribable audio should fail")
    } catch {
        try check(error.localizedDescription.contains("Audio file is empty (0 bytes): \(emptyAudioFile.path)"), "pipeline empty audio preflight message")
    }

    let header = WAVWriter.header(sampleRate: 16_000, channels: 1, bitDepth: 16, dataSize: 32_000)
    try checkEqual(header.count, 44, "WAV header size")
    try checkEqual(header.subdata(in: 0..<4), Data("RIFF".utf8), "WAV RIFF marker")
    try checkEqual(header.subdata(in: 8..<12), Data("WAVE".utf8), "WAV WAVE marker")
    try checkEqual(WAVWriter.totalFileSize(dataSize: 1_000), 1_044, "WAV total file size")

    // Crash-safe StreamingWAVWriter: the on-disk WAV header must track appended
    // data periodically (not only on finish()), so a SIGKILLed/crashed
    // recorder never leaves a header-only or stale-size WAV.
    let csDir = FileManager.default.temporaryDirectory.appendingPathComponent("stt-checks-\(UUID().uuidString)", isDirectory: true)
    try FileManager.default.createDirectory(at: csDir, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: csDir) }
    let csURL = csDir.appendingPathComponent("crashsafe.wav")
    let csWriter = try StreamingWAVWriter(url: csURL, sampleRate: 48_000, channels: 2, bitDepth: 16)
    let chunk = Data(repeating: 0xAB, count: 1 << 16) // 64 KiB
    var csTotal = 0
    for _ in 0..<40 { // 2.5 MiB total, crosses the 1 MiB patch threshold
        try csWriter.append(chunk)
        csTotal += chunk.count
    }
    // Deliberately do NOT call finish(): simulate abrupt termination.
    let csData = try Data(contentsOf: csURL)
    try checkEqual(csData.count, 44 + csTotal, "StreamingWAVWriter append preserves total byte count before finish")
    let csDataSize = csData.subdata(in: 40..<44).withUnsafeBytes { UInt32(littleEndian: $0.load(as: UInt32.self)) }
    try check(csDataSize >= 1 << 20, "StreamingWAVWriter header patched mid-stream (not stale at 0)")
    try check(csDataSize <= UInt32(csTotal), "StreamingWAVWriter patched header does not exceed bytes written")
    let csRiff = csData.subdata(in: 4..<8).withUnsafeBytes { UInt32(littleEndian: $0.load(as: UInt32.self)) }
    try checkEqual(csRiff, 36 + csDataSize, "StreamingWAVWriter patched RIFF chunk size is consistent")
    try checkEqual(csData.suffix(chunk.count), chunk, "StreamingWAVWriter header patch did not truncate the data stream")

    let parsedWAV = try WAVPCMFile.parse(WAVPCMFile(sampleRate: 16_000, samples: [100, -100]).encodedData())
    try checkEqual(parsedWAV.samples, [100, -100], "WAV PCM parser round trip")
    let mixedWAV = try WAVMixer.mix(
        WAVPCMFile(sampleRate: 16_000, samples: [30_000, -30_000, 123]),
        WAVPCMFile(sampleRate: 16_000, samples: [10_000, -10_000]),
        mode: .raw
    )
    try checkEqual(mixedWAV.samples, [32_767, -32_768, 123], "WAV mixer clips and pads")

    let burstyMicSamples: [Int16] = (0..<400).map { index in
        guard index % 8 == 0 else { return 0 }
        return (index / 8).isMultiple(of: 2) ? 60 : -60
    }
    let continuousSystemSamples: [Int16] = (0..<400).map { index in
        guard index % 2 == 0 else { return 0 }
        return (index / 2).isMultiple(of: 2) ? 100 : -100
    }
    let burstyRawMix = try WAVMixer.mix(
        WAVPCMFile(sampleRate: 16_000, samples: burstyMicSamples),
        WAVPCMFile(sampleRate: 16_000, samples: continuousSystemSamples),
        mode: .raw
    )
    let burstyBalancedMix = try WAVMixer.mix(
        WAVPCMFile(sampleRate: 16_000, samples: burstyMicSamples),
        WAVPCMFile(sampleRate: 16_000, samples: continuousSystemSamples),
        mode: .balanced
    )
    try check(rms(burstyBalancedMix.samples) > rms(burstyRawMix.samples), "balanced mixer lifts bursty meeting RMS")
    try check(peakMagnitude(burstyBalancedMix.samples) > peakMagnitude(burstyRawMix.samples), "balanced mixer avoids unnecessary blanket attenuation")

    do {
        _ = try WAVPCMFile.parse(Data("not a wav".utf8))
        throw CheckFailure(message: "WAV parser should reject short data")
    } catch WAVMixerError.invalidHeader(let reason) {
        try check(reason.contains("shorter than"), "WAV parser short-data error")
    }
    do {
        var invalidRIFF = WAVPCMFile(sampleRate: 8_000, samples: [1]).encodedData()
        invalidRIFF.replaceSubrange(0..<4, with: Data("NOPE".utf8))
        _ = try WAVPCMFile.parse(invalidRIFF)
        throw CheckFailure(message: "WAV parser should reject missing RIFF")
    } catch WAVMixerError.invalidHeader(let reason) {
        try check(reason.contains("RIFF"), "WAV parser missing RIFF error")
    }
    do {
        var nonPCM = WAVPCMFile(sampleRate: 8_000, samples: [1]).encodedData()
        nonPCM[20] = 3
        nonPCM[21] = 0
        _ = try WAVPCMFile.parse(nonPCM)
        throw CheckFailure(message: "WAV parser should reject non-PCM format")
    } catch WAVMixerError.unsupportedFormat(let reason) {
        try check(reason.contains("integer PCM"), "WAV parser non-PCM error")
    }
    do {
        var zeroSampleRate = WAVPCMFile(sampleRate: 8_000, samples: [1]).encodedData()
        zeroSampleRate[24] = 0
        zeroSampleRate[25] = 0
        zeroSampleRate[26] = 0
        zeroSampleRate[27] = 0
        _ = try WAVPCMFile.parse(zeroSampleRate)
        throw CheckFailure(message: "WAV parser should reject zero sample rate")
    } catch WAVMixerError.unsupportedFormat(let reason) {
        try check(reason.contains("sample rate"), "WAV parser zero sample rate error")
    }
    do {
        var zeroChannels = WAVPCMFile(sampleRate: 8_000, samples: [1]).encodedData()
        zeroChannels[22] = 0
        zeroChannels[23] = 0
        _ = try WAVPCMFile.parse(zeroChannels)
        throw CheckFailure(message: "WAV parser should reject zero channels")
    } catch WAVMixerError.unsupportedFormat(let reason) {
        try check(reason.contains("channel count"), "WAV parser zero channels error")
    }
    do {
        _ = try WAVMixer.mix(
            WAVPCMFile(sampleRate: 8_000, bitDepth: 8, samples: [1]),
            WAVPCMFile(sampleRate: 8_000, samples: [1])
        )
        throw CheckFailure(message: "WAV mixer should reject non-16-bit direct input")
    } catch WAVMixerError.unsupportedFormat(let reason) {
        try check(reason.contains("16-bit"), "WAV mixer non-16-bit direct input error")
    }
    do {
        let misaligned = WAVWriter.header(sampleRate: 8_000, channels: 2, bitDepth: 16, dataSize: 2) + Data([0, 0])
        _ = try WAVPCMFile.parse(misaligned)
        throw CheckFailure(message: "WAV parser should reject misaligned data")
    } catch WAVMixerError.invalidHeader(let reason) {
        try check(reason.contains("aligned"), "WAV parser misaligned data error")
    }

    let meetingMixDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: meetingMixDir) }
    try FileManager.default.createDirectory(at: meetingMixDir, withIntermediateDirectories: true)
    let meetingMicURL = meetingMixDir.appendingPathComponent("mic.wav")
    let meetingSystemURL = meetingMixDir.appendingPathComponent("system.wav")
    let meetingMixedURL = meetingMixDir.appendingPathComponent("mixed.wav")
    try WAVPCMFile(sampleRate: 8_000, samples: [1_000, 2_000]).encodedData().write(to: meetingMicURL)
    try WAVPCMFile(sampleRate: 8_000, samples: [3_000]).encodedData().write(to: meetingSystemURL)
    let meetingSelection = Pipeline.resolveMeetingAudioSource(micURL: meetingMicURL, systemURL: meetingSystemURL, mixedURL: meetingMixedURL)
    try checkEqual(meetingSelection.audioToTranscribeURL, meetingMixedURL, "meeting pipeline selects mixed track")
    try checkEqual(meetingSelection.outputURLs, [meetingMicURL, meetingSystemURL, meetingMixedURL], "meeting pipeline records mixed output path")
    try check(meetingSelection.note == nil, "meeting pipeline has no mix note on success")
    try check(FileManager.default.fileExists(atPath: meetingMixedURL.path), "meeting pipeline writes mixed track")

    let meetingFallbackDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: meetingFallbackDir) }
    try FileManager.default.createDirectory(at: meetingFallbackDir, withIntermediateDirectories: true)
    let fallbackMicURL = meetingFallbackDir.appendingPathComponent("mic.wav")
    let fallbackSystemURL = meetingFallbackDir.appendingPathComponent("system.wav")
    let fallbackMixedURL = meetingFallbackDir.appendingPathComponent("mixed.wav")
    try WAVPCMFile(sampleRate: 16_000, samples: [1]).encodedData().write(to: fallbackMicURL)
    try Data("not a wav".utf8).write(to: fallbackSystemURL)
    let fallbackSelection = Pipeline.resolveMeetingAudioSource(micURL: fallbackMicURL, systemURL: fallbackSystemURL, mixedURL: fallbackMixedURL)
    try checkEqual(fallbackSelection.audioToTranscribeURL, fallbackMicURL, "meeting pipeline falls back to mic track")
    try checkEqual(fallbackSelection.outputURLs, [fallbackMicURL, fallbackSystemURL], "meeting pipeline fallback output paths")
    try check(fallbackSelection.note?.contains("Mixed track unavailable") == true, "meeting pipeline fallback note explains mix failure")
    try check(fallbackSelection.note?.contains("transcribing mic.wav instead") == true, "meeting pipeline fallback note explains mic transcription")

    let headerOnlyRecording = RecordingResult(
        outputURL: URL(fileURLWithPath: "/tmp/header-only.wav"),
        durationSeconds: 1,
        fileSizeBytes: 4096
    )
    try check(!headerOnlyRecording.likelyContainsAudioData, "header-only recording detected")
    try check(headerOnlyRecording.emptyAudioWarning?.contains("no audio frames") == true, "header-only recording warning")

    try check(BundleAttribution.isRunningFromAppBundle(bundlePath: "/Applications/stt.app"), "app bundle path detected")
    try check(!BundleAttribution.isRunningFromAppBundle(bundlePath: "/usr/local/bin/stt"), "bare binary path detected")
    let bundleDiagnostic = BundleAttribution.diagnosticLines(bundlePath: "/usr/local/bin/stt", bundleIdentifier: nil).joined(separator: "\n")
    try check(bundleDiagnostic.contains("./scripts/build-app-bundle.sh"), "bundle diagnostic includes build command")

    let attributionGuidance = AudioPermissions.tccAttributionGuidance(bundleID: "com.example.stt")
    try check(attributionGuidance.contains("./scripts/build-app-bundle.sh"), "TCC attribution guidance includes bundle build command")
    try check(attributionGuidance.contains("STT_RESET_TCC=1 ./scripts/manual-tcc-smoke.sh"), "TCC attribution guidance includes smoke command")

    let microphoneGuidance = AudioPermissions.microphoneResetGuidance(bundleID: "com.example.stt")
    try check(microphoneGuidance.contains("tccutil reset Microphone com.example.stt"), "microphone guidance includes reset command")

    let fallbackGuidance = SystemAudioRecorderError.fallbackConfigurationGuidance()
    try check(fallbackGuidance.contains("stt devices"), "system fallback guidance includes devices command")
    try check(fallbackGuidance.contains("--fail-if-empty"), "system fallback guidance includes fail-if-empty")

    let meetingMicError = MeetingRecorderError.micStartFailed(DeviceListError.deviceNotFound("Unit Test Mic")).errorDescription ?? ""
    try check(meetingMicError.contains("Failed to start mic capture:"), "meeting mic error includes context")
    try check(meetingMicError.contains("No input device found matching \"Unit Test Mic\""), "meeting mic error preserves underlying message")

    let meetingSystemFallbackError = MeetingRecorderError.systemStartFailed(SystemAudioRecorderError.noFallbackDeviceConfigured).errorDescription ?? ""
    try check(meetingSystemFallbackError.contains("Failed to start system-audio capture:"), "meeting system error includes context")
    try check(meetingSystemFallbackError.contains("No fallback input device configured or found for system-audio capture."), "meeting system error preserves fallback guidance")
    try check(meetingSystemFallbackError.contains("BlackHole"), "meeting system error includes routing guidance")

    let meetingSystemDeviceError = MeetingRecorderError.systemStartFailed(DeviceListError.deviceNotFound("definitely-missing-stt-meeting-device")).errorDescription ?? ""
    try check(meetingSystemDeviceError.contains("No input device found matching \"definitely-missing-stt-meeting-device\""), "meeting system error preserves missing device")

    let unavailableTap = NativeTapAvailability(
        osVersionSupported: true,
        createProcessTapSymbolAvailable: false,
        destroyProcessTapSymbolAvailable: true,
        createAggregateDeviceSymbolAvailable: true
    )
    try check(!unavailableTap.isPotentiallyAvailable, "native tap availability requires process tap symbol")
    try check(unavailableTap.summary.contains("AudioHardwareCreateProcessTap"), "native tap summary names missing symbol")
    let runtimeTap = SystemAudioRecorder.probeNativeTapAvailability()
    try check(!runtimeTap.summary.isEmpty, "runtime native tap probe has summary")

    let tapDiagnostic = NativeTapDiagnostic(
        availability: NativeTapAvailability(
            osVersionSupported: true,
            createProcessTapSymbolAvailable: true,
            destroyProcessTapSymbolAvailable: true,
            createAggregateDeviceSymbolAvailable: true
        ),
        createDestroyAttempted: false
    )
    try check(tapDiagnostic.summary.contains("STT_NATIVE_TAP_DIAGNOSTIC=1"), "native tap diagnostic summary tells user how to opt in")

    let stateTempDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: stateTempDir) }
    let state = SessionState(
        runID: "20260704-153300",
        name: "Customer Call",
        mode: .meeting,
        startedAt: Date(timeIntervalSince1970: 1_788_000_000),
        finishedAt: Date(timeIntervalSince1970: 1_788_000_123),
        durationSeconds: 123.4,
        outputPaths: ["/tmp/mic.wav", "/tmp/system.wav", "/tmp/mixed.wav"],
        separateTracks: true,
        transcribedAudioPath: "/tmp/mixed.wav",
        transcriptTextPath: "/tmp/transcript.txt",
        transcriptJSONPath: "/tmp/transcript.json",
        backend: "vibevoice-mlx",
        notes: "unit check"
    )
    _ = try SessionStateStore.write(state, toRunDirectory: stateTempDir)
    let decodedState = try SessionStateStore.read(fromRunDirectory: stateTempDir)
    try checkEqual(decodedState, state, "session state round trip")

    let backendArguments = PythonTranscriber.buildTranscribeArguments(
        audioPath: "meeting.wav",
        outputTextPath: "out.txt",
        outputJSONPath: "out.json",
        device: "gpu",
        modelPath: "custom/model",
        maxNewTokens: 2048
    )
    try check(backendArguments.contains("--model"), "backend arguments include model")
    try check(backendArguments.contains("custom/model"), "backend arguments include model path")
    try check(backendArguments.contains("--max-new-tokens"), "backend arguments include max token flag")
    try check(backendArguments.contains("2048"), "backend arguments include max token value")

    let parsedTranscript = try PythonTranscriber.parseResult(
        audioPath: "meeting.wav",
        stdout: "log before\n{\"backend\":\"vibevoice-mlx\",\"detected_language\":\"en\",\"duration\":12.5,\"transcript_text\":\"Hello world\"}\nlog after",
        outputTextPath: "out.txt",
        outputJSONPath: "out.json"
    )
    try checkEqual(parsedTranscript.backend, "vibevoice-mlx", "transcriber backend parses")
    try checkEqual(parsedTranscript.language, "en", "transcriber detected language parses")
    try checkEqual(parsedTranscript.durationSeconds, 12.5, "transcriber duration parses")
    try checkEqual(parsedTranscript.transcriptText, "Hello world", "transcriber text parses")
    try checkEqual(parsedTranscript.transcriptTextPath, "out.txt", "transcriber text path preserved")

    let noisyTranscript = try PythonTranscriber.parseResult(
        audioPath: "noisy.wav",
        stdout: "log with braces {not json}\n{\"backend\":\"fake\",\"text\":\"Recovered\",\"duration\":4.5}",
        outputTextPath: nil,
        outputJSONPath: nil
    )
    try checkEqual(noisyTranscript.backend, "fake", "transcriber skips malformed brace logs")
    try checkEqual(noisyTranscript.transcriptText, "Recovered", "transcriber parses JSON after noisy brace log")

    let finalJsonTranscript = try PythonTranscriber.parseResult(
        audioPath: "final.wav",
        stdout: "{\"event\":\"loading\",\"text\":\"not transcript\"}\n{\"backend\":\"summary\",\"transcript_text\":\"Final transcript\",\"duration\":7.0}",
        outputTextPath: nil,
        outputJSONPath: nil
    )
    try checkEqual(finalJsonTranscript.backend, "summary", "transcriber prefers final JSON summary")
    try checkEqual(finalJsonTranscript.transcriptText, "Final transcript", "transcriber extracts final JSON text")

    let rawTranscript = try PythonTranscriber.parseResult(
        audioPath: "raw.wav",
        stdout: "plain transcript text",
        outputTextPath: nil,
        outputJSONPath: nil
    )
    try checkEqual(rawTranscript.transcriptText, "plain transcript text", "raw transcript fallback works")

    let timeoutBackendDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
    defer { try? FileManager.default.removeItem(at: timeoutBackendDir) }
    let timeoutPackageDir = timeoutBackendDir.appendingPathComponent("stt_vibevoice", isDirectory: true)
    try FileManager.default.createDirectory(at: timeoutPackageDir, withIntermediateDirectories: true)
    try Data().write(to: timeoutPackageDir.appendingPathComponent("__init__.py"))
    try """
    from __future__ import annotations
    import time
    print("sleeping", flush=True)
    time.sleep(30)
    """.write(to: timeoutPackageDir.appendingPathComponent("transcribe.py"), atomically: true, encoding: .utf8)
    let transcriberTimeoutStartedAt = Date()
    do {
        _ = try PythonTranscriber.transcribe(
            audioPath: "audio.wav",
            outputTextPath: nil,
            outputJSONPath: nil,
            device: "cpu",
            workingDirectory: timeoutBackendDir,
            timeout: 0.2
        )
        throw CheckFailure(message: "PythonTranscriber should wrap backend timeout")
    } catch PythonTranscriberError.timedOut(let seconds) {
        try checkEqual(seconds, 0.2, "PythonTranscriber timeout seconds preserved")
        let message = PythonTranscriberError.timedOut(seconds: seconds).errorDescription ?? ""
        try check(message.contains("timed out after 0.2s"), "PythonTranscriber timeout message includes duration")
        try check(message.contains("--timeout/--transcribe-timeout"), "PythonTranscriber timeout message includes CLI guidance")
    }
    try check(Date().timeIntervalSince(transcriberTimeoutStartedAt) < 3, "PythonTranscriber timeout returns promptly")

    let locatorTempDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: locatorTempDir) }
    let bundledResource = locatorTempDir.appendingPathComponent("Resources", isDirectory: true)
    let bundledPython = bundledResource.appendingPathComponent("python", isDirectory: true)
    try FileManager.default.createDirectory(at: bundledPython, withIntermediateDirectories: true)
    let locatedBundledPython = Transcribe.findPythonBackendDirectory(
        environment: [:],
        currentDirectory: locatorTempDir.appendingPathComponent("elsewhere", isDirectory: true),
        bundleResourceURL: bundledResource
    )
    try checkEqual(locatedBundledPython?.path, bundledPython.path, "bundled python backend locator")

    let pythonBackendDir = URL(fileURLWithPath: FileManager.default.currentDirectoryPath).appendingPathComponent("python")
    if FileManager.default.fileExists(atPath: pythonBackendDir.path) {
        let status = try PythonTranscriber.statusReport(workingDirectory: pythonBackendDir, timeout: 5)
        try check(status.succeeded, "python backend status command succeeds")
        try check(status.standardOutput.contains("overall ready"), "python backend status reports readiness")
    }

    let echo = try ProcessRunner.run(executablePath: "/bin/echo", arguments: ["hello", "world"])
    try check(echo.succeeded, "echo succeeds")
    try checkEqual(echo.standardOutput.trimmingCharacters(in: .whitespacesAndNewlines), "hello world", "echo stdout captured")

    let slowStartedAt = Date()
    do {
        _ = try ProcessRunner.run(executablePath: "/bin/sh", arguments: ["-c", "sleep 30"], timeout: 0.2)
        throw CheckFailure(message: "slow process should time out")
    } catch ProcessRunnerError.timedOut(let command) {
        try checkEqual(command, "/bin/sh", "slow process timeout command")
    }
    try check(Date().timeIntervalSince(slowStartedAt) < 3, "slow process timeout returns promptly")

    let ignoringStartedAt = Date()
    do {
        _ = try ProcessRunner.run(executablePath: "/bin/sh", arguments: ["-c", "trap '' TERM; while true; do sleep 1; done"], timeout: 0.2)
        throw CheckFailure(message: "SIGTERM-ignoring process should time out")
    } catch ProcessRunnerError.timedOut(let command) {
        try checkEqual(command, "/bin/sh", "SIGTERM-ignoring timeout command")
    }
    try check(Date().timeIntervalSince(ignoringStartedAt) < 4, "SIGTERM-ignoring process timeout returns promptly")

    // MARK: - Speaker identification: CLI parsing

    let speakerEnroll = try Speaker.Enroll.parse([
        "Larry Song", "--audio", "/tmp/larry.wav", "--provider", "mfcc-test",
        "--minimum-speech-seconds", "8.0", "--replace"
    ])
    try checkEqual(speakerEnroll.displayName, "Larry Song", "speaker enroll display name parses")
    try checkEqual(speakerEnroll.audio, "/tmp/larry.wav", "speaker enroll audio parses")
    try checkEqual(speakerEnroll.provider, "mfcc-test", "speaker enroll provider parses")
    try check(speakerEnroll.replace, "speaker enroll replace flag parses")

    let speakerEnrollNeitherAudioNorDuration = try Speaker.Enroll.parse(["Larry Song"])
    do {
        try speakerEnrollNeitherAudioNorDuration.run()
        throw CheckFailure(message: "speaker enroll without --audio or --duration should fail")
    } catch {
        let message = "\(error)"
        try check(message.contains("Exactly one of --audio or --duration"), "speaker enroll requires exactly one source")
    }

    let speakerRemoveWithoutYes = try Speaker.Remove.parse(["Larry Song"])
    do {
        try speakerRemoveWithoutYes.run()
        throw CheckFailure(message: "speaker remove without --yes should fail")
    } catch {
        try check("\(error)".contains("--yes"), "speaker remove requires --yes")
    }

    let identifyParsed = try Identify.parse([
        "/tmp/clip.wav", "--provider", "mfcc-test", "--threshold", "0.8", "--margin", "0.1"
    ])
    try checkEqual(identifyParsed.audioPath, "/tmp/clip.wav", "identify audio path parses")
    try checkEqual(identifyParsed.threshold, 0.8, "identify threshold parses")
    try checkEqual(identifyParsed.margin, 0.1, "identify margin parses")

    let topLevelSpeakerList = try STT.parseAsRoot(["speaker", "list"])
    try check(topLevelSpeakerList is Speaker.ListProfiles, "top-level routes to speaker list subcommand")
    let topLevelIdentify = try STT.parseAsRoot(["identify", "/tmp/clip.wav"])
    try check(topLevelIdentify is Identify, "top-level routes to identify subcommand")

    // MARK: - Speaker identification: SpeakerLabelResolver conflict policy

    func okExtraction(speakerId: String) -> SpeakerExtractionResult {
        SpeakerExtractionResult(speakerId: speakerId, provider: "mfcc-test", model: "stt-vibevoice/mfcc-test-v1", embedding: [1.0, 0.0], durationSeconds: 10.0, segmentCount: 2, status: "ok")
    }

    let noProfilesResult = SpeakerLabelResolver.resolve(
        candidates: [SpeakerCandidateInput(speakerId: "0", extraction: okExtraction(speakerId: "0"), matchResult: nil)],
        hasProfiles: false
    )
    try checkEqual(noProfilesResult["0"]?.matchStatus, SpeakerMatchStatus.noProfiles, "resolver: no profiles keeps speaker anonymous")

    let matchedBest = SpeakerMatchBestMatch(profileId: "profile-a", displayName: "Larry", confidence: 0.95, margin: 0.2, matched: true, status: "matched")
    let matchedResult = SpeakerLabelResolver.resolve(
        candidates: [SpeakerCandidateInput(speakerId: "0", extraction: okExtraction(speakerId: "0"), matchResult: SpeakerMatchResult(bestMatch: matchedBest, candidates: [], skippedProfiles: [], warnings: []))],
        hasProfiles: true
    )
    try checkEqual(matchedResult["0"]?.displayName, "Larry", "resolver: high-confidence match relabels speaker")
    try checkEqual(matchedResult["0"]?.profileId, "profile-a", "resolver: high-confidence match sets profile id")

    let duplicateBest0 = SpeakerMatchBestMatch(profileId: "profile-a", displayName: "Larry", confidence: 0.95, margin: 0.2, matched: true, status: "matched")
    let duplicateBest1 = SpeakerMatchBestMatch(profileId: "profile-a", displayName: "Larry", confidence: 0.85, margin: 0.15, matched: true, status: "matched")
    let duplicateResult = SpeakerLabelResolver.resolve(
        candidates: [
            SpeakerCandidateInput(speakerId: "0", extraction: okExtraction(speakerId: "0"), matchResult: SpeakerMatchResult(bestMatch: duplicateBest0, candidates: [], skippedProfiles: [], warnings: [])),
            SpeakerCandidateInput(speakerId: "1", extraction: okExtraction(speakerId: "1"), matchResult: SpeakerMatchResult(bestMatch: duplicateBest1, candidates: [], skippedProfiles: [], warnings: []))
        ],
        hasProfiles: true
    )
    try checkEqual(duplicateResult["0"]?.matchStatus, SpeakerMatchStatus.matched, "resolver: duplicate match keeps higher-confidence speaker matched")
    try checkEqual(duplicateResult["1"]?.matchStatus, SpeakerMatchStatus.duplicateProfileMatch, "resolver: duplicate match demotes lower-confidence speaker")
    try checkEqual(duplicateResult["1"]?.profileId, nil, "resolver: demoted duplicate has no profile id")

    // MARK: - Speaker identification: end-to-end enroll/list/identify/remove (mfcc-test provider)

    if FileManager.default.fileExists(atPath: pythonBackendDir.path) {
        let speakerHomeDir = FileManager.default.temporaryDirectory.appendingPathComponent("stt-speaker-e2e-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: speakerHomeDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: speakerHomeDir) }

        let sampleWAV = speakerHomeDir.appendingPathComponent("sample.wav")
        var pcmData = Data()
        let frameCount = 9 * 16_000
        for i in 0..<frameCount {
            let sample: Int16 = (i / 64) % 2 == 0 ? 8000 : -8000
            withUnsafeBytes(of: sample.littleEndian) { pcmData.append(contentsOf: $0) }
        }
        let header = WAVWriter.header(sampleRate: 16_000, channels: 1, bitDepth: 16, dataSize: UInt32(pcmData.count))
        var fileData = header
        fileData.append(pcmData)
        try fileData.write(to: sampleWAV)

        let extraction = try PythonSpeakerIdentifier.extractWholeAudio(
            audioPath: sampleWAV.path,
            provider: "mfcc-test",
            minimumSpeechSeconds: 8.0,
            workingDirectory: pythonBackendDir,
            timeout: 15
        )
        try check(extraction.isOK, "speaker-id extract whole audio succeeds end-to-end")

        let profilesDir = speakerHomeDir.appendingPathComponent("speakers", isDirectory: true)
        let store = SpeakerProfileStore(directory: profilesDir)
        let profile = SpeakerProfile(
            displayName: "Larry Song",
            embeddingProvider: extraction.provider,
            embeddingModel: extraction.model ?? "",
            embedding: extraction.embedding ?? []
        )
        try store.save(profile)
        try checkEqual(try store.listSummaries().count, 1, "speaker profile store save round trip")

        let matchResultE2E = try SpeakerCLISupport.match(
            extraction: extraction,
            profiles: try store.listProfiles(),
            threshold: 0.5,
            margin: 0.01,
            workingDirectory: pythonBackendDir
        )
        try checkEqual(matchResultE2E.bestMatch?.profileId, profile.id.uuidString, "end-to-end match identifies enrolled profile")
        try check(matchResultE2E.bestMatch?.matched == true, "end-to-end match is confident")

        try store.delete(id: profile.id)
        try checkEqual(try store.listSummaries().count, 0, "speaker profile store delete removes profile")
    }

    // MARK: - Task 08: stt speaker audit / purity-preview / enroll-ranges / suggest-labels

    do {
        let audit = try Speaker.Audit.parse([
            "--transcript", "/tmp/session/transcript.json",
            "--force", "--min-useful-speech", "5.0", "--mixed-span-ratio", "3.0"
        ])
        try checkEqual(audit.transcript, "/tmp/session/transcript.json", "speaker audit transcript parses")
        try check(audit.force, "speaker audit --force parses")
        try checkEqual(audit.minUsefulSpeech, 5.0, "speaker audit min-useful-speech parses")

        let preview = try Speaker.PurityPreview.parse([
            "--transcript", "/tmp/session/transcript.json", "--speaker-id", "4",
            "--range", "12.0-45.0", "--range", "100.0-120.0", "--no-play"
        ])
        try checkEqual(preview.speakerId, "4", "speaker purity-preview speaker-id parses")
        try checkEqual(preview.range, ["12.0-45.0", "100.0-120.0"], "speaker purity-preview repeated ranges parse")
        try check(preview.noPlay, "speaker purity-preview --no-play parses")

        let enrollRanges = try Speaker.EnrollRanges.parse([
            "Domingo", "--transcript", "/tmp/session/transcript.json", "--speaker-id", "4",
            "--range", "12.0-45.0", "--no-enroll"
        ])
        try checkEqual(enrollRanges.displayName, "Domingo", "speaker enroll-ranges display name parses")
        try checkEqual(enrollRanges.range, ["12.0-45.0"], "speaker enroll-ranges range parses")
        try check(enrollRanges.noEnroll, "speaker enroll-ranges --no-enroll parses")

        let noRanges = try Speaker.EnrollRanges.parse([
            "Domingo", "--transcript", "/tmp/session/transcript.json", "--speaker-id", "4"
        ])
        do {
            try noRanges.run()
            throw CheckFailure(message: "enroll-ranges without --range should throw")
        } catch is ValidationError {
            // expected
        }

        let suggest = try Speaker.SuggestLabels.parse([
            "--transcript", "/tmp/session/transcript.json", "--threshold", "0.8", "--no-windows"
        ])
        try checkEqual(suggest.threshold, 0.8, "speaker suggest-labels threshold parses")
        try check(suggest.noWindows, "speaker suggest-labels --no-windows parses")

        let auditRoute = try STT.parseAsRoot(["speaker", "audit", "--transcript", "/tmp/session/transcript.json"])
        try check(auditRoute is Speaker.Audit, "top-level routing reaches speaker audit")
    }

    // MARK: - Task 08: range-aware / suggest-labels argument builders (pure, no subprocess)

    do {
        let extractRanges = PythonSpeakerIdentifier.buildExtractArguments(
            audioPath: "sample.wav", segmentsJSONPath: "transcript.json", speakerID: "4",
            provider: "mfcc-test", minimumSpeechSeconds: 8.0, ranges: ["2.0-12.0", "30.0-40.0"]
        )
        try checkEqual(extractRanges, [
            "-m", "stt_vibevoice.speaker_id", "extract", "--audio", "sample.wav",
            "--segments", "transcript.json", "--speaker-id", "4",
            "--provider", "mfcc-test", "--minimum-speech-seconds", "8.0",
            "--range", "2.0-12.0", "--range", "30.0-40.0"
        ], "buildExtractArguments includes repeated --range flags")

        let concatArgs = PythonSpeakerIdentifier.buildConcatenateArguments(
            audioPath: "sample.wav", segmentsJSONPath: "transcript.json", speakerID: "4",
            outPath: "out.wav", jsonOutputPath: "out.json",
            ranges: ["2.0-12.0"], bestSegments: false
        )
        try check(concatArgs.contains("--no-best-segments"), "buildConcatenateArguments respects bestSegments=false")
        try check(concatArgs.contains("--range"), "buildConcatenateArguments includes --range")

        let suggestArgs = PythonSpeakerIdentifier.buildSuggestLabelsArguments(
            transcript: "transcript.json",
            audioSources: [(source: "mic", path: "mic.wav"), (source: "system", path: "system.wav")],
            profilesPath: "profiles.json", provider: "mfcc-test", threshold: 0.78, margin: 0.05,
            minimumSpeechSeconds: 8.0, session: "/tmp/session", noWindows: false, nWindows: 2,
            jsonOutputPath: "out.json"
        )
        try checkEqual(suggestArgs, [
            "-m", "stt_vibevoice.speaker_id", "suggest-labels",
            "--transcript", "transcript.json",
            "--audio", "mic=mic.wav", "--audio", "system=system.wav",
            "--profiles", "profiles.json",
            "--provider", "mfcc-test", "--threshold", "0.78", "--margin", "0.05",
            "--minimum-speech-seconds", "8.0", "--session", "/tmp/session",
            "--n-windows", "2", "--json", "out.json"
        ], "buildSuggestLabelsArguments produces expected argument list")

        let noWindowsArgs = PythonSpeakerIdentifier.buildSuggestLabelsArguments(
            transcript: "transcript.json", audioSources: [], profilesPath: nil, provider: "mfcc-test",
            threshold: 0.78, margin: 0.05, minimumSpeechSeconds: 8.0, session: nil,
            noWindows: true, nWindows: 2, jsonOutputPath: nil
        )
        try check(noWindowsArgs.contains("--no-windows"), "suggest-labels args use --no-windows flag")
        try check(!noWindowsArgs.contains("--n-windows"), "suggest-labels args omit --n-windows when --no-windows")

        let defaultDir = PythonSpeakerIdentifier.defaultHelperScriptsDirectory(environment: ["STT_HELPER_SCRIPTS": "/custom/scripts"])
        try checkEqual(defaultDir, "/custom/scripts", "defaultHelperScriptsDirectory prefers env override")

        let fallbackDir = PythonSpeakerIdentifier.defaultHelperScriptsDirectory(environment: [:])
        try check(fallbackDir?.hasSuffix("/.pi/agent/skills/stt-meeting-recordings/scripts") == true, "defaultHelperScriptsDirectory falls back to known default")

        let missingScript = PythonSpeakerIdentifier.resolveHelperScriptPath(
            explicitOverride: FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString).path,
            environment: [:], fileManager: .default
        )
        try check(missingScript == nil, "resolveHelperScriptPath returns nil when script missing")

        let helperArgs = PythonSpeakerIdentifier.buildHelperScriptArguments(
            subcommand: "enroll-ranges", scriptPath: "/scripts/name_one_speaker.py",
            rangeArguments: ["2.0-12.0"],
            keywordArguments: [(flag: "--session", value: "/tmp/session"), (flag: "--no-enroll", value: nil)]
        )
        try checkEqual(helperArgs, [
            "/scripts/name_one_speaker.py", "enroll-ranges",
            "--range", "2.0-12.0", "--session", "/tmp/session", "--no-enroll"
        ], "buildHelperScriptArguments interleaves ranges then keyword args")
    }

    // MARK: - Task 08: suggest-labels result decoding against the REAL backend schema
    //
    // This is a regression guard: an earlier version of SpeakerSuggestionResult
    // used made-up snake_case CodingKeys and a wrong shape (flat fields instead
    // of nested config/profilesConsidered/duplicateClusterGroups/mixedClusterWarnings,
    // and `summary` typed as String instead of an object). That mismatch would
    // have made `stt speaker suggest-labels` throw invalidJSONOutput on every
    // real backend response. This fixture mirrors build_label_suggestions's
    // actual output (speaker_id.py) exactly.

    do {
        let json = """
        {
          "schemaVersion": 1,
          "status": "ok",
          "session": "/tmp/session",
          "generatedAt": "2026-07-13T00:00:00Z",
          "config": {"threshold": 0.78, "margin": 0.05, "provider": "mfcc-test", "model": "mfcc-test-v1"},
          "profilesConsidered": {"count": 1, "profileIds": ["p1"]},
          "clusters": [
            {
              "speakerId": "1", "source": "system", "durationSeconds": 12.0, "segmentCount": 2,
              "selectedRanges": [[0.0, 5.0], [10.0, 15.0]], "speechSeconds": 10.0,
              "bestMatch": {"profileId": "p1", "displayName": "Domingo", "confidence": 0.91, "margin": 0.2, "matched": true, "status": "matched"},
              "recommendation": "reuse_profile", "recommendationDetail": "matches Domingo"
            }
          ],
          "duplicateClusterGroups": [
            {
              "profileId": "p1", "nameHint": "Domingo", "displayName": "Domingo",
              "clusters": [{"speakerId": "1", "confidence": 0.91, "selectedRanges": [[0.0, 5.0]]}],
              "recommendation": "merge_or_relabel", "recommendationDetail": "dup"
            }
          ],
          "mixedClusterWarnings": [
            {
              "speakerId": "4",
              "windows": [
                {"label": "early", "range": [0.0, 12.0], "bestMatch": {"profileId": "p1", "displayName": "Domingo", "confidence": 0.9, "margin": 0.2, "matched": true, "status": "matched"}, "matchedProfileId": "p1"},
                {"label": "late", "range": [200.0, 212.0], "bestMatch": {"profileId": "p2", "displayName": "Gia", "confidence": 0.85, "margin": 0.18, "matched": true, "status": "matched"}, "matchedProfileId": "p2"}
              ],
              "conflictingProfileIds": ["p1", "p2"], "conflictingDisplayNames": ["Domingo", "Gia"],
              "recommendation": "do_not_enroll_whole_cluster", "recommendationDetail": "mixed"
            }
          ],
          "summary": {"clusterCount": 3, "matchedCount": 2, "duplicateGroupCount": 1, "mixedClusterCount": 1, "unmatchedCount": 1}
        }
        """
        let decoded = try JSONDecoder().decode(SpeakerSuggestionResult.self, from: Data(json.utf8))
        try checkEqual(decoded.schemaVersion, 1, "suggest-labels decodes schemaVersion")
        try checkEqual(decoded.status, "ok", "suggest-labels decodes status")
        try checkEqual(decoded.config?.threshold, 0.78, "suggest-labels decodes nested config.threshold")
        try checkEqual(decoded.profilesConsidered?.count, 1, "suggest-labels decodes profilesConsidered.count")
        try checkEqual(decoded.clusters?.first?.bestMatch?.displayName, "Domingo", "suggest-labels decodes cluster bestMatch (camelCase, reused SpeakerMatchBestMatch)")
        try checkEqual(decoded.clusters?.first?.selectedRanges, [[0.0, 5.0], [10.0, 15.0]], "suggest-labels decodes selectedRanges as [[Double]]")
        try checkEqual(decoded.duplicateClusterGroups?.first?.profileId, "p1", "suggest-labels decodes duplicateClusterGroups")
        try checkEqual(decoded.mixedClusterWarnings?.first?.conflictingDisplayNames, ["Domingo", "Gia"], "suggest-labels decodes mixedClusterWarnings")
        try checkEqual(decoded.mixedClusterWarnings?.first?.windows?.count, 2, "suggest-labels decodes per-window evidence")
        try checkEqual(decoded.summary?.duplicateGroupCount, 1, "suggest-labels decodes summary as an object, not a String")

        // Round trip: re-encode and decode again (this is what the CLI does
        // when it prints the result back out).
        let reencoded = try JSONEncoder().encode(decoded)
        let redecoded = try JSONDecoder().decode(SpeakerSuggestionResult.self, from: reencoded)
        try checkEqual(redecoded, decoded, "suggest-labels result round-trips through encode/decode")

        // no_profiles state
        let noProfilesJSON = """
        {"schemaVersion": 1, "status": "no_profiles", "clusters": [], "duplicateClusterGroups": [],
         "mixedClusterWarnings": [], "profilesConsidered": {"count": 0, "profileIds": []},
         "summary": {"clusterCount": 2, "matchedCount": 0, "duplicateGroupCount": 0, "mixedClusterCount": 0, "unmatchedCount": 2},
         "recommendation": "No speaker profiles are enrolled yet."}
        """
        let noProfiles = try JSONDecoder().decode(SpeakerSuggestionResult.self, from: Data(noProfilesJSON.utf8))
        try checkEqual(noProfiles.status, "no_profiles", "suggest-labels decodes no_profiles status")
        try check(noProfiles.recommendation != nil, "suggest-labels decodes top-level recommendation in no_profiles state")
    }
}

do {
    try runChecks()
    print("sttUnitChecks: all checks passed")
} catch {
    FileHandle.standardError.write("sttUnitChecks failed: \(error)\n".data(using: .utf8)!)
    exit(1)
}
