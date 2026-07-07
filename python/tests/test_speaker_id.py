from __future__ import annotations

import json
import sys
import types
import wave
from pathlib import Path

import pytest

from stt_vibevoice import speaker_id


def _write_wav(path, samples, framerate=16000):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(framerate)
        frames = bytearray()
        for sample in samples:
            frames += int(sample).to_bytes(2, byteorder="little", signed=True)
        handle.writeframes(bytes(frames))


def _tone(duration_seconds, framerate=16000, amplitude=8000, period=64):
    n = int(duration_seconds * framerate)
    samples = []
    for i in range(n):
        # simple deterministic square-ish wave, avoids numpy/math deps.
        samples.append(amplitude if (i // period) % 2 == 0 else -amplitude)
    return samples


def _silence(duration_seconds, framerate=16000):
    return [0] * int(duration_seconds * framerate)


class TestMfccTestProvider:
    def test_embedding_is_deterministic(self, tmp_path):
        wav_path = tmp_path / "a.wav"
        _write_wav(wav_path, _tone(1.0))

        embedding1, model1 = speaker_id.embed_audio_file(wav_path, "mfcc-test")
        embedding2, model2 = speaker_id.embed_audio_file(wav_path, "mfcc-test")

        assert embedding1 == embedding2
        assert model1 == model2
        assert len(embedding1) > 0

    def test_different_audio_yields_different_embedding(self, tmp_path):
        tone_path = tmp_path / "tone.wav"
        silence_path = tmp_path / "silence.wav"
        _write_wav(tone_path, _tone(1.0))
        _write_wav(silence_path, _silence(1.0))

        tone_embedding, _ = speaker_id.embed_audio_file(tone_path, "mfcc-test")
        silence_embedding, _ = speaker_id.embed_audio_file(silence_path, "mfcc-test")

        assert tone_embedding != silence_embedding

    def test_unknown_provider_raises(self, tmp_path):
        wav_path = tmp_path / "a.wav"
        _write_wav(wav_path, _tone(1.0))
        with pytest.raises(speaker_id.SpeakerIdError):
            speaker_id.embed_audio_file(wav_path, "not-a-real-provider")


def _install_fake_speechbrain(monkeypatch, encode_batch=None, from_hparams=None):
    """Installs fake ``soundfile``/``torch``/``speechbrain`` modules into sys.modules.

    This exercises the real ``_speechbrain_embed_file`` integration code
    (model loading call, soundfile.read call, tensor -> list conversion)
    without requiring the actual (heavy, Python 3.11/3.12-only) speechbrain
    or torch packages to be installed.
    """
    import numpy as np

    class FakeTensor:
        def __init__(self, values):
            self._values = values

        def squeeze(self):
            return self

        def tolist(self):
            return self._values

    class FakeClassifier:
        @classmethod
        def from_hparams(cls, source=None, savedir=None, run_opts=None):
            if from_hparams is not None:
                from_hparams(source=source, savedir=savedir, run_opts=run_opts)
            return cls()

        def encode_batch(self, signal):
            values = encode_batch(signal) if encode_batch is not None else [0.1, 0.2, 0.3]
            return FakeTensor(values)

    class FakeTorchTensor:
        def __init__(self, data):
            self._data = data
            self.ndim = getattr(data, "ndim", 1)

        def unsqueeze(self, dim):
            self.ndim = 2
            return self

        def float(self):
            return self

    class FakeTorch:
        Tensor = FakeTorchTensor

        @staticmethod
        def from_numpy(data):
            return FakeTorchTensor(data)

    fake_soundfile = types.ModuleType("soundfile")
    fake_soundfile.read = lambda path: (np.array([0.0, 0.1, -0.1, 0.2], dtype=np.float64), 16000)

    fake_speaker_module = types.ModuleType("speechbrain.inference.speaker")
    fake_speaker_module.EncoderClassifier = FakeClassifier

    fake_inference_module = types.ModuleType("speechbrain.inference")
    fake_inference_module.speaker = fake_speaker_module

    fake_speechbrain_module = types.ModuleType("speechbrain")
    fake_speechbrain_module.inference = fake_inference_module

    monkeypatch.setitem(sys.modules, "soundfile", fake_soundfile)
    # Force the device-selection probe to fall back to CPU deterministically,
    # regardless of whether a real torch install with MPS support happens to
    # be present in the environment running these tests.
    monkeypatch.setitem(sys.modules, "torch", FakeTorch)
    monkeypatch.setitem(sys.modules, "speechbrain", fake_speechbrain_module)
    monkeypatch.setitem(sys.modules, "speechbrain.inference", fake_inference_module)
    monkeypatch.setitem(sys.modules, "speechbrain.inference.speaker", fake_speaker_module)

    return fake_soundfile, fake_speaker_module


class TestSpeechbrainProvider:
    """Exercises the speechbrain provider without requiring the real, heavy
    speechbrain/torchaudio/torch dependency stack to be installed."""

    def test_embeds_using_mocked_speechbrain_and_soundfile(self, tmp_path, monkeypatch):
        wav_path = tmp_path / "a.wav"
        _write_wav(wav_path, _tone(1.0))

        calls = {}

        def from_hparams(source, savedir, run_opts):
            calls["source"] = source
            calls["savedir"] = savedir
            calls["run_opts"] = run_opts

        _install_fake_speechbrain(
            monkeypatch,
            encode_batch=lambda signal: [0.4, 0.5, 0.6],
            from_hparams=from_hparams,
        )

        embedding, model_id = speaker_id.embed_audio_file(wav_path, "speechbrain")

        assert embedding == [0.4, 0.5, 0.6]
        assert model_id == "speechbrain/spkrec-ecapa-voxceleb"
        assert calls["source"] == "speechbrain/spkrec-ecapa-voxceleb"
        assert calls["savedir"].endswith("speechbrain__spkrec-ecapa-voxceleb")
        assert calls["run_opts"] == {"device": "cpu"}

    def test_cache_dir_respects_env_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STT_SPEECHBRAIN_CACHE", str(tmp_path / "custom-cache"))
        cache_dir = speaker_id._speechbrain_cache_dir("speechbrain/spkrec-ecapa-voxceleb")
        assert cache_dir == tmp_path / "custom-cache" / "speechbrain__spkrec-ecapa-voxceleb"

    def test_missing_dependencies_raise_actionable_speaker_id_error(self, tmp_path, monkeypatch):
        wav_path = tmp_path / "a.wav"
        _write_wav(wav_path, _tone(1.0))

        # sys.modules[name] = None forces Python's import machinery to raise
        # ImportError for that module, regardless of whether it is actually
        # installed in this environment.
        monkeypatch.setitem(sys.modules, "speechbrain", None)

        with pytest.raises(speaker_id.SpeakerIdError) as excinfo:
            speaker_id.embed_audio_file(wav_path, "speechbrain")

        message = str(excinfo.value)
        assert "speechbrain" in message
        assert "torchaudio" in message
        assert "3.11" in message or "3.12" in message

    def test_model_loading_failure_raises_actionable_speaker_id_error(self, tmp_path, monkeypatch):
        wav_path = tmp_path / "a.wav"
        _write_wav(wav_path, _tone(1.0))

        def from_hparams(source, savedir, run_opts):
            raise RuntimeError("simulated network failure while downloading model")

        _install_fake_speechbrain(monkeypatch, from_hparams=from_hparams)

        with pytest.raises(speaker_id.SpeakerIdError) as excinfo:
            speaker_id.embed_audio_file(wav_path, "speechbrain")

        assert "failed to extract an embedding" in str(excinfo.value)


class TestCosineSimilarity:
    def test_identical_vectors_score_one(self):
        assert speaker_id.cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)

    def test_orthogonal_vectors_score_zero(self):
        assert speaker_id.cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_dimension_mismatch_raises(self):
        with pytest.raises(speaker_id.SpeakerIdError):
            speaker_id.cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0])


class TestSelectSpeakerSegments:
    def test_filters_by_speaker_and_minimum_duration(self):
        segments = [
            {"speaker_id": "0", "start_time": 0.0, "end_time": 2.0},
            {"speaker_id": "1", "start_time": 2.0, "end_time": 4.0},
            {"speaker_id": "0", "start_time": 4.0, "end_time": 4.2},  # too short, dropped
            {"speaker_id": "0", "start_time": 5.0, "end_time": 7.0},
        ]
        ranges = speaker_id.select_speaker_segments(segments, "0")
        assert ranges == [(0.0, 2.0), (5.0, 7.0)]

    def test_ignores_segments_missing_times(self):
        segments = [{"speaker_id": "0", "start_time": None, "end_time": 2.0}]
        assert speaker_id.select_speaker_segments(segments, "0") == []


class TestExtractWholeAudio:
    def test_below_minimum_returns_too_short(self, tmp_path):
        wav_path = tmp_path / "short.wav"
        _write_wav(wav_path, _tone(1.0))

        result = speaker_id.extract_whole_audio(
            audio_path=wav_path, provider="mfcc-test", minimum_speech_seconds=8.0
        )
        assert result["status"] == "too_short"
        assert result["embedding"] is None

    def test_meets_minimum_returns_ok(self, tmp_path):
        wav_path = tmp_path / "long.wav"
        _write_wav(wav_path, _tone(9.0))

        result = speaker_id.extract_whole_audio(
            audio_path=wav_path, provider="mfcc-test", minimum_speech_seconds=8.0
        )
        assert result["status"] == "ok"
        assert result["provider"] == "mfcc-test"
        assert result["embedding"]
        assert result["durationSeconds"] == pytest.approx(9.0, abs=0.01)


class TestExtractSpeakerSegments:
    def test_concatenates_and_extracts(self, tmp_path):
        wav_path = tmp_path / "full.wav"
        _write_wav(wav_path, _tone(20.0))

        segments = [
            {"speaker_id": "0", "start_time": 0.0, "end_time": 5.0},
            {"speaker_id": "1", "start_time": 5.0, "end_time": 10.0},
            {"speaker_id": "0", "start_time": 10.0, "end_time": 15.0},
        ]

        result = speaker_id.extract_speaker_segments(
            audio_path=wav_path,
            segments=segments,
            speaker_id="0",
            provider="mfcc-test",
            minimum_speech_seconds=8.0,
        )
        assert result["status"] == "ok"
        assert result["segmentCount"] == 2
        assert result["durationSeconds"] == pytest.approx(10.0, abs=0.01)

    def test_too_short_when_speaker_speech_below_minimum(self, tmp_path):
        wav_path = tmp_path / "full.wav"
        _write_wav(wav_path, _tone(20.0))

        segments = [
            {"speaker_id": "0", "start_time": 0.0, "end_time": 2.0},
            {"speaker_id": "1", "start_time": 2.0, "end_time": 18.0},
        ]

        result = speaker_id.extract_speaker_segments(
            audio_path=wav_path,
            segments=segments,
            speaker_id="0",
            provider="mfcc-test",
            minimum_speech_seconds=8.0,
        )
        assert result["status"] == "too_short"
        assert result["embedding"] is None

    def test_no_diarization_for_speaker_returns_too_short(self, tmp_path):
        wav_path = tmp_path / "full.wav"
        _write_wav(wav_path, _tone(20.0))

        result = speaker_id.extract_speaker_segments(
            audio_path=wav_path,
            segments=[],
            speaker_id="0",
            provider="mfcc-test",
            minimum_speech_seconds=8.0,
        )
        assert result["status"] == "too_short"
        assert result["segmentCount"] == 0


class TestMatchCandidate:
    def _profile(self, profile_id, embedding, provider="mfcc-test", model="stt-vibevoice/mfcc-test-v1"):
        return {
            "id": profile_id,
            "displayName": f"Person {profile_id}",
            "embeddingProvider": provider,
            "embeddingModel": model,
            "embedding": embedding,
        }

    def test_matched_when_above_threshold_and_margin(self):
        candidate = {"embedding": [1.0, 0.0], "provider": "mfcc-test", "model": "stt-vibevoice/mfcc-test-v1"}
        profiles = [
            self._profile("a", [1.0, 0.0]),
            self._profile("b", [0.0, 1.0]),
        ]
        result = speaker_id.match_candidate(candidate, profiles, threshold=0.5, margin=0.1)
        assert result["bestMatch"]["profileId"] == "a"
        assert result["bestMatch"]["matched"] is True
        assert result["bestMatch"]["status"] == "matched"

    def test_below_threshold_stays_unmatched(self):
        candidate = {"embedding": [1.0, 0.0], "provider": "mfcc-test", "model": "stt-vibevoice/mfcc-test-v1"}
        profiles = [self._profile("a", [0.1, 0.995])]
        result = speaker_id.match_candidate(candidate, profiles, threshold=0.9, margin=0.05)
        assert result["bestMatch"]["matched"] is False
        assert result["bestMatch"]["status"] == "below_threshold"

    def test_ambiguous_margin_stays_unmatched(self):
        candidate = {"embedding": [1.0, 0.0], "provider": "mfcc-test", "model": "stt-vibevoice/mfcc-test-v1"}
        # Two profiles nearly identical in similarity to candidate -> low margin.
        profiles = [
            self._profile("a", [1.0, 0.01]),
            self._profile("b", [1.0, 0.011]),
        ]
        result = speaker_id.match_candidate(candidate, profiles, threshold=0.5, margin=0.5)
        assert result["bestMatch"]["matched"] is False
        assert result["bestMatch"]["status"] == "ambiguous"

    def test_provider_model_mismatch_is_skipped(self):
        candidate = {"embedding": [1.0, 0.0], "provider": "mfcc-test", "model": "stt-vibevoice/mfcc-test-v1"}
        profiles = [self._profile("a", [1.0, 0.0], provider="speechbrain", model="speechbrain/spkrec-ecapa-voxceleb")]
        result = speaker_id.match_candidate(candidate, profiles, threshold=0.5, margin=0.05)
        assert result["bestMatch"] is None
        assert result["skippedProfiles"][0]["reason"] == "provider_model_mismatch"

    def test_no_profiles_returns_no_best_match(self):
        candidate = {"embedding": [1.0, 0.0], "provider": "mfcc-test", "model": "stt-vibevoice/mfcc-test-v1"}
        result = speaker_id.match_candidate(candidate, [], threshold=0.5, margin=0.05)
        assert result["bestMatch"] is None
        assert result["candidates"] == []

    def test_no_embedding_candidate_returns_warning(self):
        candidate = {"embedding": None, "provider": "mfcc-test", "model": "stt-vibevoice/mfcc-test-v1"}
        result = speaker_id.match_candidate(candidate, [self._profile("a", [1.0, 0.0])], threshold=0.5, margin=0.05)
        assert result["bestMatch"] is None
        assert result["warnings"]


class TestCLI:
    def test_extract_writes_json_file(self, tmp_path):
        wav_path = tmp_path / "long.wav"
        _write_wav(wav_path, _tone(9.0))
        out_path = tmp_path / "out.json"

        exit_code = speaker_id.main(
            [
                "extract",
                "--audio",
                str(wav_path),
                "--provider",
                "mfcc-test",
                "--minimum-speech-seconds",
                "8.0",
                "--json",
                str(out_path),
            ]
        )
        assert exit_code == 0
        payload = json.loads(out_path.read_text(encoding="utf-8"))
        assert payload["status"] == "ok"

    def test_extract_too_short_returns_nonzero_exit(self, tmp_path):
        wav_path = tmp_path / "short.wav"
        _write_wav(wav_path, _tone(1.0))
        out_path = tmp_path / "out.json"

        exit_code = speaker_id.main(
            [
                "extract",
                "--audio",
                str(wav_path),
                "--provider",
                "mfcc-test",
                "--minimum-speech-seconds",
                "8.0",
                "--json",
                str(out_path),
            ]
        )
        assert exit_code == 2
        payload = json.loads(out_path.read_text(encoding="utf-8"))
        assert payload["status"] == "too_short"

    def test_match_cli_writes_json_file(self, tmp_path):
        candidate_path = tmp_path / "candidate.json"
        profiles_path = tmp_path / "profiles.json"
        out_path = tmp_path / "match.json"

        candidate_path.write_text(
            json.dumps({"embedding": [1.0, 0.0], "provider": "mfcc-test", "model": "stt-vibevoice/mfcc-test-v1"}),
            encoding="utf-8",
        )
        profiles_path.write_text(
            json.dumps(
                {
                    "profiles": [
                        {
                            "id": "a",
                            "displayName": "A",
                            "embeddingProvider": "mfcc-test",
                            "embeddingModel": "stt-vibevoice/mfcc-test-v1",
                            "embedding": [1.0, 0.0],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        exit_code = speaker_id.main(
            [
                "match",
                "--candidate",
                str(candidate_path),
                "--profiles",
                str(profiles_path),
                "--threshold",
                "0.5",
                "--margin",
                "0.05",
                "--json",
                str(out_path),
            ]
        )
        assert exit_code == 0
        payload = json.loads(out_path.read_text(encoding="utf-8"))
        assert payload["bestMatch"]["profileId"] == "a"
