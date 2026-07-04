import Testing
@testable import sttCore

@Suite("PythonTranscriber result parsing")
struct PythonTranscriberTests {

    @Test func buildsBackendArgumentsWithOptionalModelSettings() {
        let arguments = PythonTranscriber.buildTranscribeArguments(
            audioPath: "meeting.wav",
            outputTextPath: "out.txt",
            outputJSONPath: "out.json",
            device: "gpu",
            modelPath: "custom/model",
            maxNewTokens: 2048
        )

        #expect(arguments == [
            "-m", "stt_vibevoice.transcribe",
            "meeting.wav",
            "--device", "gpu",
            "--output", "out.txt",
            "--json", "out.json",
            "--model", "custom/model",
            "--max-new-tokens", "2048"
        ])
    }

    @Test func buildsBackendArgumentsWithoutEmptyModelPath() {
        let arguments = PythonTranscriber.buildTranscribeArguments(
            audioPath: "meeting.wav",
            outputTextPath: nil,
            outputJSONPath: nil,
            device: "auto",
            modelPath: "",
            maxNewTokens: nil
        )

        #expect(arguments == ["-m", "stt_vibevoice.transcribe", "meeting.wav", "--device", "auto"])
    }

    @Test func parsesJSONSurroundedByLogLines() throws {
        let stdout = """
        [backend] loading model
        {"backend":"vibevoice-mlx","detected_language":"en","duration":12.5,"transcript_text":"Hello world","transcript_file":"fallback.txt"}
        [backend] done
        """

        let result = try PythonTranscriber.parseResult(
            audioPath: "meeting.wav",
            stdout: stdout,
            outputTextPath: "out.txt",
            outputJSONPath: "out.json"
        )

        #expect(result.audioPath == "meeting.wav")
        #expect(result.backend == "vibevoice-mlx")
        #expect(result.language == "en")
        #expect(result.durationSeconds == 12.5)
        #expect(result.transcriptText == "Hello world")
        #expect(result.transcriptTextPath == "out.txt")
        #expect(result.transcriptJSONPath == "out.json")
        #expect(result.raw == stdout)
    }

    @Test func fallsBackToRawTextWhenNoJSONObjectExists() throws {
        let stdout = "plain transcript text only"
        let result = try PythonTranscriber.parseResult(
            audioPath: "note.wav",
            stdout: stdout,
            outputTextPath: nil,
            outputJSONPath: nil
        )

        #expect(result.transcriptText == stdout)
        #expect(result.backend == nil)
        #expect(result.language == nil)
        #expect(result.durationSeconds == nil)
        #expect(result.raw == stdout)
    }

    @Test func fallsBackToRawTextWhenJSONObjectIsMalformed() throws {
        let stdout = "before {not valid json} after"
        let result = try PythonTranscriber.parseResult(
            audioPath: "bad.wav",
            stdout: stdout,
            outputTextPath: nil,
            outputJSONPath: nil
        )

        #expect(result.transcriptText == stdout)
        #expect(result.backend == nil)
        #expect(result.raw == stdout)
    }

    @Test func languageAndTextFallbackKeysAreSupported() throws {
        let stdout = "{\"backend\":\"test-backend\",\"language\":\"fr\",\"text\":\"Bonjour\",\"duration\":3.0}"
        let result = try PythonTranscriber.parseResult(
            audioPath: "bonjour.wav",
            stdout: stdout,
            outputTextPath: nil,
            outputJSONPath: nil
        )

        #expect(result.backend == "test-backend")
        #expect(result.language == "fr")
        #expect(result.transcriptText == "Bonjour")
        #expect(result.durationSeconds == 3.0)
    }

    @Test func transcriptFileFallsBackToBackendPathWhenOutputTextPathMissing() throws {
        let stdout = "{\"transcript_file\":\"backend-output.txt\",\"text\":\"Saved text\"}"
        let result = try PythonTranscriber.parseResult(
            audioPath: "saved.wav",
            stdout: stdout,
            outputTextPath: nil,
            outputJSONPath: "structured.json"
        )

        #expect(result.transcriptTextPath == "backend-output.txt")
        #expect(result.transcriptJSONPath == "structured.json")
        #expect(result.transcriptText == "Saved text")
    }
}
