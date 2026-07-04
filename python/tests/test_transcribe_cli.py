from __future__ import annotations

import json

import stt_vibevoice.transcribe as transcribe_module


def test_main_prints_machine_readable_summary_after_writing_outputs(monkeypatch, tmp_path, capsys):
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"fake audio bytes")
    text_path = tmp_path / "out.txt"
    json_path = tmp_path / "out.json"

    def fake_transcribe_file(audio_path_arg, model_path, device, max_new_tokens):
        assert audio_path_arg == audio_path
        assert model_path == "custom/model"
        assert device == "cpu"
        assert max_new_tokens == 123
        return {
            "text": "hello from fake model",
            "diarised_text": "Speaker 1: hello from fake model",
            "segments": [],
            "duration_seconds": 2.5,
            "backend": "fake-backend",
            "device": "cpu",
            "model_path": "custom/model",
            "chunked": False,
        }

    monkeypatch.setattr(transcribe_module, "transcribe_file", fake_transcribe_file)

    exit_code = transcribe_module.main(
        [
            str(audio_path),
            "--output",
            str(text_path),
            "--json",
            str(json_path),
            "--device",
            "cpu",
            "--model",
            "custom/model",
            "--max-new-tokens",
            "123",
        ]
    )

    captured = capsys.readouterr()
    summary = json.loads(captured.out.strip().splitlines()[-1])

    assert exit_code == 0
    assert text_path.exists()
    assert json_path.exists()
    assert "hello from fake model" in text_path.read_text(encoding="utf-8")
    assert summary["backend"] == "fake-backend"
    assert summary["text"] == "hello from fake model"
    assert summary["transcript_text"] == "hello from fake model"
    assert summary["duration"] == 2.5
    assert summary["transcript_file"] == str(text_path)
    assert summary["json_file"] == str(json_path)
