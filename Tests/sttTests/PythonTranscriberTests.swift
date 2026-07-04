import Foundation
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

    @Test func parsesValidJSONObjectAfterLogLineWithMalformedBraces() throws {
        let stdout = "[backend] retry context {not json}\n{\"backend\":\"fake\",\"text\":\"Recovered\",\"duration\":4.5}"
        let result = try PythonTranscriber.parseResult(
            audioPath: "recovered.wav",
            stdout: stdout,
            outputTextPath: nil,
            outputJSONPath: nil
        )

        #expect(result.backend == "fake")
        #expect(result.transcriptText == "Recovered")
        #expect(result.durationSeconds == 4.5)
    }

    @Test func prefersFinalJSONObjectWhenLogsContainEarlierJSON() throws {
        let stdout = """
        {"event":"loading","text":"not the transcript"}
        Wrote transcript: out.txt
        {"backend":"summary","transcript_text":"Final transcript","duration":7.0}
        """
        let result = try PythonTranscriber.parseResult(
            audioPath: "final.wav",
            stdout: stdout,
            outputTextPath: nil,
            outputJSONPath: nil
        )

        #expect(result.backend == "summary")
        #expect(result.transcriptText == "Final transcript")
        #expect(result.durationSeconds == 7.0)
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

    @Test func transcribeTimeoutUsesActionableTranscriptionError() throws {
        let backendDir = try makeFakeBackend(transcribeSource: """
        from __future__ import annotations
        import time
        print("sleeping", flush=True)
        time.sleep(30)
        """)
        defer { try? FileManager.default.removeItem(at: backendDir) }

        let startedAt = Date()
        do {
            _ = try PythonTranscriber.transcribe(
                audioPath: "audio.wav",
                outputTextPath: nil,
                outputJSONPath: nil,
                device: "cpu",
                workingDirectory: backendDir,
                timeout: 0.2
            )
            Issue.record("Expected PythonTranscriber timeout")
        } catch PythonTranscriberError.timedOut(let seconds) {
            #expect(seconds == 0.2)
            let message = PythonTranscriberError.timedOut(seconds: seconds).errorDescription ?? ""
            #expect(message.contains("timed out after 0.2s"))
            #expect(message.contains("--timeout/--transcribe-timeout"))
        } catch {
            Issue.record("Unexpected error: \(error)")
        }
        #expect(Date().timeIntervalSince(startedAt) < 3)
    }

    private func makeFakeBackend(transcribeSource: String) throws -> URL {
        let backendDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        let packageDir = backendDir.appendingPathComponent("stt_vibevoice", isDirectory: true)
        try FileManager.default.createDirectory(at: packageDir, withIntermediateDirectories: true)
        try Data().write(to: packageDir.appendingPathComponent("__init__.py"))
        try transcribeSource.write(to: packageDir.appendingPathComponent("transcribe.py"), atomically: true, encoding: .utf8)
        return backendDir
    }
}
