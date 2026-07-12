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


def _write_wav_stereo(path, channels, framerate=16000):
    """Write a multi-channel WAV. ``channels`` is a list of sample lists,
    one per channel (all must be the same length)."""
    n_channels = len(channels)
    n_frames = len(channels[0])
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(n_channels)
        handle.setsampwidth(2)
        handle.setframerate(framerate)
        frames = bytearray()
        for i in range(n_frames):
            for ch in range(n_channels):
                frames += int(channels[ch][i]).to_bytes(2, byteorder="little", signed=True)
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


@pytest.mark.skipif(
    False,
    reason="placeholder",
)
class TestLoadAudioTensorResampleDownmix:
    """Verifies _load_audio_tensor normalises sample rate and channel count.

    Real meeting audio arrives at 44.1 kHz mono (mic) and 48 kHz stereo
    (system loopback); the ECAPA model expects 16 kHz mono. These tests run
    under the runtime/.venv (Python 3.11, torch + soundfile + scipy).
    """

    def test_44100hz_mono_is_resampled_to_16khz(self, tmp_path):
        wav = tmp_path / "mic.wav"
        _write_wav(wav, _tone(1.0, framerate=44100), framerate=44100)
        signal = speaker_id._load_audio_tensor(wav)
        assert signal.shape[0] == 1  # mono + batch dim
        # 1s at 16kHz = 16000 samples; allow small resampling rounding.
        assert abs(signal.shape[1] - speaker_id.ECAPA_SAMPLE_RATE) < 400

    def test_48000hz_stereo_is_downmixed_and_resampled(self, tmp_path):
        wav = tmp_path / "system.wav"
        left = _tone(1.0, framerate=48000, amplitude=8000)
        right = _tone(1.0, framerate=48000, amplitude=4000)
        _write_wav_stereo(wav, [left, right], framerate=48000)
        signal = speaker_id._load_audio_tensor(wav)
        assert signal.shape[0] == 1  # downmixed to mono
        assert abs(signal.shape[1] - speaker_id.ECAPA_SAMPLE_RATE) < 400  # 16kHz

    def test_16khz_mono_passes_through_unchanged(self, tmp_path):
        wav = tmp_path / "a.wav"
        _write_wav(wav, _tone(1.0), framerate=16000)
        signal = speaker_id._load_audio_tensor(wav)
        assert signal.shape == (1, 16000)


class TestBatchedEmbedding:
    def test_speechbrain_loads_model_once_for_many_files(self, tmp_path, monkeypatch):
        # The batched path must load the ECAPA model exactly once regardless of
        # how many files it embeds -- loading per file is the diarisation perf bug.
        paths = [tmp_path / f"clip{i}.wav" for i in range(3)]
        for p in paths:
            _write_wav(p, _tone(1.0))

        from_hparams_calls = {"count": 0}

        def from_hparams(source, savedir, run_opts):
            from_hparams_calls["count"] += 1

        _install_fake_speechbrain(
            monkeypatch,
            encode_batch=lambda signal: [0.4, 0.5, 0.6],
            from_hparams=from_hparams,
        )

        embeddings, model_id = speaker_id.embed_audio_files(paths, "speechbrain")

        assert from_hparams_calls["count"] == 1, "model must be loaded exactly once"
        assert len(embeddings) == 3
        assert all(emb == [0.4, 0.5, 0.6] for emb in embeddings)
        assert model_id == "speechbrain/spkrec-ecapa-voxceleb"

    def test_mfcc_test_loops_per_file(self, tmp_path):
        paths = [tmp_path / f"clip{i}.wav" for i in range(2)]
        for p in paths:
            _write_wav(p, _tone(1.0))

        embeddings, model_id = speaker_id.embed_audio_files(paths, "mfcc-test")

        assert len(embeddings) == 2
        assert model_id == "stt-vibevoice/mfcc-test-v1"
        # Deterministic: same source audio -> same embedding.
        assert embeddings[0] == embeddings[1]


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


class TestSelectSpeakerSegmentsNonspeechFilter:
    """Tests for the skip_nonspeech filtering added to select_speaker_segments."""

    def test_skips_bracketed_nonspeech_tags_by_default(self):
        segments = [
            {"speaker_id": "0", "start_time": 0.0, "end_time": 3.0, "text": "[Silence]"},
            {"speaker_id": "0", "start_time": 3.0, "end_time": 6.0, "text": "[Environmental Sounds]"},
            {"speaker_id": "0", "start_time": 6.0, "end_time": 9.0, "text": "Hello there"},
            {"speaker_id": "0", "start_time": 9.0, "end_time": 12.0, "text": "[Human Sounds]"},
            {"speaker_id": "0", "start_time": 12.0, "end_time": 15.0, "text": "How are you"},
        ]
        ranges = speaker_id.select_speaker_segments(segments, "0")
        assert ranges == [(6.0, 9.0), (12.0, 15.0)]

    def test_includes_nonspeech_when_skip_disabled(self):
        segments = [
            {"speaker_id": "0", "start_time": 0.0, "end_time": 3.0, "text": "[Silence]"},
            {"speaker_id": "0", "start_time": 3.0, "end_time": 6.0, "text": "Hello"},
        ]
        ranges = speaker_id.select_speaker_segments(segments, "0", skip_nonspeech=False)
        assert ranges == [(0.0, 3.0), (3.0, 6.0)]

    def test_only_speech_remaining_when_all_nonspeech(self):
        segments = [
            {"speaker_id": "0", "start_time": 0.0, "end_time": 3.0, "text": "[Silence]"},
            {"speaker_id": "0", "start_time": 3.0, "end_time": 6.0, "text": "[Music]"},
        ]
        assert speaker_id.select_speaker_segments(segments, "0") == []


class TestCapRangesByDuration:
    """Tests for cap_ranges_by_duration added this session."""

    def test_truncates_at_cap_mid_range(self):
        ranges = [(0.0, 10.0), (20.0, 30.0), (40.0, 50.0)]
        assert speaker_id.cap_ranges_by_duration(ranges, 25.0) == [(0.0, 10.0), (20.0, 30.0), (40.0, 45.0)]

    def test_zero_or_negative_returns_all(self):
        ranges = [(0.0, 10.0), (20.0, 30.0)]
        assert speaker_id.cap_ranges_by_duration(ranges, 0) == [(0.0, 10.0), (20.0, 30.0)]
        assert speaker_id.cap_ranges_by_duration(ranges, -5.0) == [(0.0, 10.0), (20.0, 30.0)]

    def test_cap_larger_than_total_returns_all(self):
        ranges = [(0.0, 10.0), (20.0, 30.0)]
        assert speaker_id.cap_ranges_by_duration(ranges, 100.0) == [(0.0, 10.0), (20.0, 30.0)]

    def test_exact_fit(self):
        ranges = [(0.0, 10.0), (20.0, 30.0)]
        assert speaker_id.cap_ranges_by_duration(ranges, 20.0) == [(0.0, 10.0), (20.0, 30.0)]

    def test_empty_ranges(self):
        assert speaker_id.cap_ranges_by_duration([], 30.0) == []


class TestNormalizeWavFile:
    """Tests for normalize_wav_file in wav_slicing."""

    def test_amplifies_quiet_audio(self, tmp_path):
        from stt_vibevoice import wav_slicing

        wav_path = tmp_path / "quiet.wav"
        _write_wav(wav_path, _tone(2.0, amplitude=100))  # very quiet
        gain = wav_slicing.normalize_wav_file(wav_path, target_dbfs=-19.0)
        assert gain > 1.0
        # verify the output is louder
        samples = wav_slicing.read_pcm16_mono_samples(wav_path)
        max_sample = max(abs(s) for s in samples)
        assert max_sample > 1000  # should be well above the input amplitude of 100

    def test_does_not_attenuate_already_loud_audio(self, tmp_path):
        from stt_vibevoice import wav_slicing

        wav_path = tmp_path / "loud.wav"
        loud_samples = _tone(2.0, amplitude=30000)  # near max
        _write_wav(wav_path, loud_samples)
        gain = wav_slicing.normalize_wav_file(wav_path, target_dbfs=-19.0)
        assert gain == 1.0  # never attenuates

    def test_silent_audio_returns_one(self, tmp_path):
        from stt_vibevoice import wav_slicing

        wav_path = tmp_path / "silent.wav"
        _write_wav(wav_path, _silence(2.0))
        gain = wav_slicing.normalize_wav_file(wav_path, target_dbfs=-19.0)
        assert gain == 1.0

    def test_preserves_stereo_channel_count(self, tmp_path):
        from stt_vibevoice import wav_slicing

        wav_path = tmp_path / "stereo.wav"
        tone = _tone(2.0, amplitude=100)
        _write_wav_stereo(wav_path, [tone, tone])
        wav_slicing.normalize_wav_file(wav_path, target_dbfs=-19.0)
        with wave.open(str(wav_path), "rb") as h:
            assert h.getnchannels() == 2
            assert h.getsampwidth() == 2


class TestRankRangesByEnergy:
    """Tests for rank_ranges_by_energy in wav_slicing."""

    def test_returns_sorted_by_energy_descending(self, tmp_path):
        from stt_vibevoice import wav_slicing

        # Build a WAV: first 2s quiet, next 2s loud, last 2s medium
        quiet = _tone(2.0, amplitude=500)
        loud = _tone(2.0, amplitude=20000)
        medium = _tone(2.0, amplitude=5000)
        wav_path = tmp_path / "mixed.wav"
        _write_wav(wav_path, quiet + loud + medium, framerate=16000)

        scored = wav_slicing.rank_ranges_by_energy(wav_path, [(0.0, 2.0), (2.0, 4.0), (4.0, 6.0)])
        assert len(scored) == 3
        # loudest (2-4s) should be first, quietest (0-2s) last
        assert scored[0][0] == 2.0  # loud segment starts at 2.0
        assert scored[-1][0] == 0.0  # quiet segment starts at 0.0
        # rms values should be descending
        rms_values = [s[2] for s in scored]
        assert rms_values == sorted(rms_values, reverse=True)

    def test_empty_ranges(self, tmp_path):
        from stt_vibevoice import wav_slicing

        wav_path = tmp_path / "tone.wav"
        _write_wav(wav_path, _tone(1.0))
        assert wav_slicing.rank_ranges_by_energy(wav_path, []) == []


class TestParseTimestamp:
    """Arrange-Act-Assert tests for the internal _parse_timestamp helper."""

    def test_plain_seconds_with_fraction(self):
        # Arrange / Act
        result = speaker_id._parse_timestamp("123.4")
        # Assert
        assert result == pytest.approx(123.4)

    def test_plain_integer_seconds(self):
        result = speaker_id._parse_timestamp("180")
        assert result == pytest.approx(180.0)

    def test_mmss_format(self):
        result = speaker_id._parse_timestamp("02:03")
        assert result == pytest.approx(123.0)  # 2*60 + 3

    def test_hhmmss_format(self):
        result = speaker_id._parse_timestamp("00:41:30")
        assert result == pytest.approx(2490.0)  # 41*60 + 30

    def test_hhmmss_with_hours(self):
        result = speaker_id._parse_timestamp("01:02:03")
        assert result == pytest.approx(3723.0)  # 3600 + 120 + 3

    def test_negative_seconds_raises(self):
        with pytest.raises(speaker_id.SpeakerIdError):
            speaker_id._parse_timestamp("-5")

    def test_invalid_string_raises(self):
        with pytest.raises(speaker_id.SpeakerIdError):
            speaker_id._parse_timestamp("abc")

    def test_empty_string_raises(self):
        with pytest.raises(speaker_id.SpeakerIdError):
            speaker_id._parse_timestamp("")

    def test_too_many_fields_raises(self):
        with pytest.raises(speaker_id.SpeakerIdError):
            speaker_id._parse_timestamp("1:2:3:4")


class TestParseTimeRange:
    """Arrange-Act-Assert tests for parse_time_range."""

    def test_seconds_range(self):
        # Arrange / Act
        result = speaker_id.parse_time_range("123.4-180.0")
        # Assert
        assert result == (pytest.approx(123.4), pytest.approx(180.0))

    def test_mmss_range(self):
        result = speaker_id.parse_time_range("02:03-03:00")
        assert result == (pytest.approx(123.0), pytest.approx(180.0))

    def test_hhmmss_range(self):
        result = speaker_id.parse_time_range("00:41:30-00:57:00")
        assert result == (pytest.approx(2490.0), pytest.approx(3420.0))

    def test_mixed_formats(self):
        # seconds start, HH:MM:SS end
        result = speaker_id.parse_time_range("120-00:02:30")
        assert result == (pytest.approx(120.0), pytest.approx(150.0))

    def test_missing_dash_raises(self):
        with pytest.raises(speaker_id.SpeakerIdError):
            speaker_id.parse_time_range("120to180")

    def test_missing_end_raises(self):
        with pytest.raises(speaker_id.SpeakerIdError):
            speaker_id.parse_time_range("120-")

    def test_missing_start_raises(self):
        with pytest.raises(speaker_id.SpeakerIdError):
            speaker_id.parse_time_range("-180")

    def test_reversed_range_raises(self):
        with pytest.raises(speaker_id.SpeakerIdError) as excinfo:
            speaker_id.parse_time_range("180.0-123.4")
        assert "strictly less than" in str(excinfo.value)

    def test_equal_start_end_raises(self):
        with pytest.raises(speaker_id.SpeakerIdError):
            speaker_id.parse_time_range("120-120")

    def test_invalid_timestamp_raises(self):
        with pytest.raises(speaker_id.SpeakerIdError):
            speaker_id.parse_time_range("abc-180")

    def test_error_message_echoes_offending_value(self):
        with pytest.raises(speaker_id.SpeakerIdError) as excinfo:
            speaker_id.parse_time_range("garbage")
        assert "garbage" in str(excinfo.value)


class TestParseTimeRanges:
    """Tests for parse_time_ranges (repeated values)."""

    def test_none_returns_empty(self):
        assert speaker_id.parse_time_ranges(None) == []

    def test_empty_list_returns_empty(self):
        assert speaker_id.parse_time_ranges([]) == []

    def test_single_range(self):
        result = speaker_id.parse_time_ranges(["10-20"])
        assert result == [(10.0, 20.0)]

    def test_multiple_ranges_preserve_order(self):
        result = speaker_id.parse_time_ranges(["10-20", "30-40", "02:03-03:00"])
        assert result == [
            (10.0, 20.0),
            (30.0, 40.0),
            (123.0, 180.0),
        ]

    def test_invalid_range_propagates_error(self):
        with pytest.raises(speaker_id.SpeakerIdError):
            speaker_id.parse_time_ranges(["10-20", "reversed-5"])


class TestSelectSpeakerSegmentsWithRanges:
    """Tests for the range-intersection mode of select_speaker_segments."""

    def test_no_ranges_unchanged_behavior(self):
        # Arrange
        segments = [
            {"speaker_id": "0", "start_time": 0.0, "end_time": 5.0, "text": "hi"},
            {"speaker_id": "1", "start_time": 5.0, "end_time": 10.0, "text": "yo"},
        ]
        # Act
        result = speaker_id.select_speaker_segments(segments, "0", ranges=None)
        # Assert — identical to calling without ranges
        assert result == [(0.0, 5.0)]

    def test_range_clips_to_segment_boundary(self):
        # Arrange: a 0-10s speaker segment; request 3-7s
        segments = [
            {"speaker_id": "0", "start_time": 0.0, "end_time": 10.0, "text": "hi"},
        ]
        # Act
        result = speaker_id.select_speaker_segments(segments, "0", ranges=[(3.0, 7.0)])
        # Assert: clipped to requested range, not full segment
        assert result == [(3.0, 7.0)]

    def test_range_excludes_unrelated_speakers(self):
        # Arrange: two speakers overlapping in time; range should only pick the matching speaker
        segments = [
            {"speaker_id": "0", "start_time": 0.0, "end_time": 10.0, "text": "speaker0"},
            {"speaker_id": "1", "start_time": 5.0, "end_time": 15.0, "text": "speaker1"},
        ]
        # Act
        result = speaker_id.select_speaker_segments(segments, "0", ranges=[(4.0, 12.0)])
        # Assert: only speaker 0, clipped to segment boundary at 10s
        assert result == [(4.0, 10.0)]

    def test_non_overlapping_range_yields_nothing(self):
        # Arrange
        segments = [
            {"speaker_id": "0", "start_time": 0.0, "end_time": 5.0, "text": "hi"},
        ]
        # Act: request a range entirely outside
        result = speaker_id.select_speaker_segments(segments, "0", ranges=[(100.0, 200.0)])
        # Assert
        assert result == []

    def test_overlapping_ranges_produce_multiple_clips(self):
        # Arrange: two segments; two overlapping ranges
        segments = [
            {"speaker_id": "0", "start_time": 0.0, "end_time": 10.0, "text": "a"},
            {"speaker_id": "0", "start_time": 20.0, "end_time": 30.0, "text": "b"},
        ]
        # Act
        result = speaker_id.select_speaker_segments(
            segments, "0", ranges=[(5.0, 8.0), (22.0, 27.0)]
        )
        # Assert
        assert result == [(5.0, 8.0), (22.0, 27.0)]

    def test_range_spanning_two_segments(self):
        # Arrange: one requested range covering two segments of same speaker
        segments = [
            {"speaker_id": "0", "start_time": 0.0, "end_time": 5.0, "text": "a"},
            {"speaker_id": "0", "start_time": 10.0, "end_time": 15.0, "text": "b"},
        ]
        # Act: range 3-12 covers part of both segments
        result = speaker_id.select_speaker_segments(segments, "0", ranges=[(3.0, 12.0)])
        # Assert: clipped per-segment
        assert result == [(3.0, 5.0), (10.0, 12.0)]

    def test_bracket_only_segments_excluded_with_ranges(self):
        # Arrange
        segments = [
            {"speaker_id": "0", "start_time": 0.0, "end_time": 5.0, "text": "[Silence]"},
            {"speaker_id": "0", "start_time": 5.0, "end_time": 10.0, "text": "hello"},
        ]
        # Act
        result = speaker_id.select_speaker_segments(segments, "0", ranges=[(0.0, 10.0)])
        # Assert: only the speech segment
        assert result == [(5.0, 10.0)]

    def test_clipped_piece_below_minimum_duration_dropped(self):
        # Arrange: a segment; a range that overlaps by only 0.1s
        segments = [
            {"speaker_id": "0", "start_time": 0.0, "end_time": 10.0, "text": "hi"},
        ]
        # Act: overlap is 9.9-10.0 = 0.1s < MINIMUM_SEGMENT_SECONDS (0.5)
        result = speaker_id.select_speaker_segments(segments, "0", ranges=[(9.9, 20.0)])
        # Assert
        assert result == []

    def test_ranges_do_not_include_other_speakers_speech(self):
        # Arrange: speaker 0 and 1 interleaved; range should not grab speaker 1
        segments = [
            {"speaker_id": "0", "start_time": 0.0, "end_time": 4.0, "text": "s0a"},
            {"speaker_id": "1", "start_time": 4.0, "end_time": 8.0, "text": "s1"},
            {"speaker_id": "0", "start_time": 8.0, "end_time": 12.0, "text": "s0b"},
        ]
        # Act: range 0-12 covers everything, but only speaker 0 segments should appear
        result = speaker_id.select_speaker_segments(segments, "0", ranges=[(0.0, 12.0)])
        # Assert
        assert result == [(0.0, 4.0), (8.0, 12.0)]


class TestFilterSpeakerSegments:
    """Tests for the metadata-returning filter_speaker_segments helper."""

    def test_no_ranges_returns_all_matching_segments(self):
        # Arrange
        segments = [
            {"speaker_id": "0", "start_time": 0.0, "end_time": 3.0, "text": "hi"},
            {"speaker_id": "1", "start_time": 3.0, "end_time": 6.0, "text": "yo"},
            {"speaker_id": "0", "start_time": 6.0, "end_time": 9.0, "text": "bye"},
        ]
        # Act
        result = speaker_id.filter_speaker_segments(segments, "0")
        # Assert
        assert result["speakerId"] == "0"
        assert result["requestedRanges"] is None
        assert result["selectedRanges"] == [(0.0, 3.0), (6.0, 9.0)]
        assert result["selectedSegmentCount"] == 2
        assert result["selectedSpeechSeconds"] == pytest.approx(6.0, abs=0.001)
        assert result["nonspeechExcluded"] == 0

    def test_with_ranges_clips_and_returns_metadata(self):
        # Arrange
        segments = [
            {"speaker_id": "0", "start_time": 0.0, "end_time": 10.0, "text": "hi"},
        ]
        # Act
        result = speaker_id.filter_speaker_segments(segments, "0", ranges=["3-7"])
        # Assert
        assert result["requestedRanges"] == [(3.0, 7.0)]
        assert result["selectedRanges"] == [(3.0, 7.0)]
        assert result["selectedSegmentCount"] == 1
        assert result["selectedSpeechSeconds"] == pytest.approx(4.0, abs=0.001)

    def test_bracket_only_excluded_and_counted(self):
        # Arrange
        segments = [
            {"speaker_id": "0", "start_time": 0.0, "end_time": 3.0, "text": "[Silence]"},
            {"speaker_id": "0", "start_time": 3.0, "end_time": 6.0, "text": "hi"},
        ]
        # Act
        result = speaker_id.filter_speaker_segments(segments, "0")
        # Assert
        assert result["selectedRanges"] == [(3.0, 6.0)]
        assert result["nonspeechExcluded"] == 1
        assert result["bracketOnlyExcluded"] == 1

    def test_non_matching_speaker_excluded(self):
        # Arrange
        segments = [
            {"speaker_id": "0", "start_time": 0.0, "end_time": 5.0, "text": "hi"},
            {"speaker_id": "1", "start_time": 5.0, "end_time": 10.0, "text": "yo"},
        ]
        # Act
        result = speaker_id.filter_speaker_segments(segments, "1")
        # Assert
        assert result["selectedRanges"] == [(5.0, 10.0)]
        assert result["selectedSegmentCount"] == 1

    def test_overlapping_ranges_metadata(self):
        # Arrange
        segments = [
            {"speaker_id": "0", "start_time": 0.0, "end_time": 10.0, "text": "a"},
            {"speaker_id": "0", "start_time": 20.0, "end_time": 30.0, "text": "b"},
        ]
        # Act
        result = speaker_id.filter_speaker_segments(
            segments, "0", ranges=["5-8", "22-27"]
        )
        # Assert
        assert result["requestedRanges"] == [(5.0, 8.0), (22.0, 27.0)]
        assert result["selectedRanges"] == [(5.0, 8.0), (22.0, 27.0)]
        assert result["selectedSpeechSeconds"] == pytest.approx(8.0, abs=0.001)

    def test_non_overlapping_range_returns_empty(self):
        # Arrange
        segments = [
            {"speaker_id": "0", "start_time": 0.0, "end_time": 5.0, "text": "hi"},
        ]
        # Act
        result = speaker_id.filter_speaker_segments(segments, "0", ranges=["100-200"])
        # Assert
        assert result["selectedRanges"] == []
        assert result["selectedSegmentCount"] == 0
        assert result["selectedSpeechSeconds"] == 0.0

    def test_invalid_range_raises(self):
        # Arrange
        segments = [{"speaker_id": "0", "start_time": 0.0, "end_time": 5.0, "text": "hi"}]
        # Act / Assert
        with pytest.raises(speaker_id.SpeakerIdError):
            speaker_id.filter_speaker_segments(segments, "0", ranges=["reversed-5"])

    def test_hhmmss_range_string_parses(self):
        # Arrange: 41m30s - 57m00s
        segments = [
            {"speaker_id": "0", "start_time": 2400.0, "end_time": 3600.0, "text": "hi"},
        ]
        # Act
        result = speaker_id.filter_speaker_segments(
            segments, "0", ranges=["00:41:30-00:57:00"]
        )
        # Assert
        assert result["requestedRanges"] == [(2490.0, 3420.0)]
        assert result["selectedRanges"] == [(2490.0, 3420.0)]
        assert result["selectedSpeechSeconds"] == pytest.approx(930.0, abs=0.001)


# ===========================================================================
# Task 02: range-aware concatenate / extract CLI
# ===========================================================================


class TestConcatenateRangeCliArgParsing:
    """Unit: argument parsing for repeated --range on the concatenate subcommand."""

    def test_concatenate_parses_single_range(self):
        parser = speaker_id.build_arg_parser()
        args = parser.parse_args(
            [
                "concatenate",
                "--audio", "a.wav",
                "--segments", "s.json",
                "--speaker-id", "0",
                "--out", "o.wav",
                "--range", "10-20",
            ]
        )
        assert args.range == ["10-20"]
        assert args.command == "concatenate"

    def test_concatenate_parses_repeated_ranges(self):
        parser = speaker_id.build_arg_parser()
        args = parser.parse_args(
            [
                "concatenate",
                "--audio", "a.wav",
                "--segments", "s.json",
                "--speaker-id", "0",
                "--out", "o.wav",
                "--range", "10-20",
                "--range", "30-40",
                "--range", "02:03-03:00",
            ]
        )
        assert args.range == ["10-20", "30-40", "02:03-03:00"]

    def test_concatenate_range_defaults_to_none(self):
        parser = speaker_id.build_arg_parser()
        args = parser.parse_args(
            [
                "concatenate",
                "--audio", "a.wav",
                "--segments", "s.json",
                "--speaker-id", "0",
                "--out", "o.wav",
            ]
        )
        assert args.range is None


class TestExtractRangeCliArgParsing:
    """Unit: argument parsing for repeated --range on the extract subcommand."""

    def test_extract_parses_single_range(self):
        parser = speaker_id.build_arg_parser()
        args = parser.parse_args(
            [
                "extract",
                "--audio", "a.wav",
                "--segments", "s.json",
                "--speaker-id", "0",
                "--range", "10-20",
            ]
        )
        assert args.range == ["10-20"]

    def test_extract_parses_repeated_ranges(self):
        parser = speaker_id.build_arg_parser()
        args = parser.parse_args(
            [
                "extract",
                "--audio", "a.wav",
                "--segments", "s.json",
                "--speaker-id", "0",
                "--range", "10-20",
                "--range", "30-40",
            ]
        )
        assert args.range == ["10-20", "30-40"]

    def test_extract_range_defaults_to_none(self):
        parser = speaker_id.build_arg_parser()
        args = parser.parse_args(
            [
                "extract",
                "--audio", "a.wav",
            ]
        )
        assert args.range is None


class TestExtractSpeakerSegmentsWithRanges:
    """Unit: JSON metadata fields for selected ranges in extract_speaker_segments."""

    def test_extract_with_ranges_includes_range_metadata(self, tmp_path):
        # Arrange: a 20s tone WAV
        wav_path = tmp_path / "full.wav"
        _write_wav(wav_path, _tone(20.0))
        segments = [
            {"speaker_id": "0", "start_time": 0.0, "end_time": 10.0, "text": "a"},
            {"speaker_id": "0", "start_time": 10.0, "end_time": 20.0, "text": "b"},
        ]
        # Act: request only 2-7s of speaker 0 (5s of speech)
        result = speaker_id.extract_speaker_segments(
            audio_path=wav_path,
            segments=segments,
            speaker_id="0",
            provider="mfcc-test",
            minimum_speech_seconds=3.0,
            ranges=[(2.0, 7.0)],
        )
        # Assert
        assert result["status"] == "ok"
        assert result["requestedRanges"] == [(2.0, 7.0)]
        assert result["selectedRanges"] == [(2.0, 7.0)]
        assert result["selectedSegmentCount"] == 1
        assert result["selectedSpeechSeconds"] == pytest.approx(5.0, abs=0.01)
        assert result["durationSeconds"] == pytest.approx(5.0, abs=0.05)
        assert result["embedding"]

    def test_extract_without_ranges_has_none_requested(self, tmp_path):
        # Arrange
        wav_path = tmp_path / "full.wav"
        _write_wav(wav_path, _tone(20.0))
        segments = [
            {"speaker_id": "0", "start_time": 0.0, "end_time": 12.0, "text": "a"},
        ]
        # Act
        result = speaker_id.extract_speaker_segments(
            audio_path=wav_path,
            segments=segments,
            speaker_id="0",
            provider="mfcc-test",
            minimum_speech_seconds=8.0,
        )
        # Assert: backward compatible -- requestedRanges is None
        assert result["status"] == "ok"
        assert result["requestedRanges"] is None
        assert result["selectedRanges"] == [(0.0, 12.0)]
        assert result["selectedSpeechSeconds"] == pytest.approx(12.0, abs=0.01)

    def test_extract_too_short_with_ranges_still_emits_metadata(self, tmp_path):
        # Arrange: the requested range yields too little speech
        wav_path = tmp_path / "full.wav"
        _write_wav(wav_path, _tone(20.0))
        segments = [
            {"speaker_id": "0", "start_time": 0.0, "end_time": 5.0, "text": "a"},
        ]
        # Act: request a 2s range, minimum is 8s
        result = speaker_id.extract_speaker_segments(
            audio_path=wav_path,
            segments=segments,
            speaker_id="0",
            provider="mfcc-test",
            minimum_speech_seconds=8.0,
            ranges=[(1.0, 3.0)],
        )
        # Assert
        assert result["status"] == "too_short"
        assert result["embedding"] is None
        assert result["requestedRanges"] == [(1.0, 3.0)]
        assert result["selectedRanges"] == [(1.0, 3.0)]
        assert result["selectedSpeechSeconds"] == pytest.approx(2.0, abs=0.01)

    def test_extract_ranges_clip_to_segment_boundary(self, tmp_path):
        # Arrange
        wav_path = tmp_path / "full.wav"
        _write_wav(wav_path, _tone(20.0))
        segments = [
            {"speaker_id": "0", "start_time": 5.0, "end_time": 10.0, "text": "a"},
        ]
        # Act: request 0-15, but segment is only 5-10 -> clipped to 5-10
        result = speaker_id.extract_speaker_segments(
            audio_path=wav_path,
            segments=segments,
            speaker_id="0",
            provider="mfcc-test",
            minimum_speech_seconds=3.0,
            ranges=[(0.0, 15.0)],
        )
        # Assert
        assert result["status"] == "ok"
        assert result["selectedRanges"] == [(5.0, 10.0)]
        assert result["durationSeconds"] == pytest.approx(5.0, abs=0.05)


class TestConcatenateRangeIntegration:
    """Integration/e2e: concatenate a selected range and verify WAV/metadata."""

    def _write_segments_json(self, path, segments):
        path.write_text(json.dumps({"segments": segments}), encoding="utf-8")

    def test_concatenate_range_creates_wav_with_only_requested_speech(self, tmp_path):
        # Arrange: 20s tone; speaker 0 talks in 0-10, speaker 1 in 10-20
        wav_path = tmp_path / "full.wav"
        _write_wav(wav_path, _tone(20.0))
        segments_path = tmp_path / "segments.json"
        self._write_segments_json(
            segments_path,
            [
                {"speaker_id": "0", "start_time": 0.0, "end_time": 10.0, "text": "a"},
                {"speaker_id": "1", "start_time": 10.0, "end_time": 20.0, "text": "b"},
            ],
        )
        out_wav = tmp_path / "out.wav"
        out_json = tmp_path / "out.json"
        # Act: request only 2-6s (within speaker 0's segment)
        exit_code = speaker_id.main(
            [
                "concatenate",
                "--audio", str(wav_path),
                "--segments", str(segments_path),
                "--speaker-id", "0",
                "--out", str(out_wav),
                "--no-best-segments",
                "--range", "2-6",
                "--json", str(out_json),
            ]
        )
        # Assert
        assert exit_code == 0
        payload = json.loads(out_json.read_text(encoding="utf-8"))
        assert payload["status"] == "ok"
        # JSON round-trip converts tuples to lists, so expected ranges are
        # list-of-lists here (not list-of-tuples).
        assert payload["requestedRanges"] == [[2.0, 6.0]]
        assert payload["selectedRanges"] == [[2.0, 6.0]]
        assert payload["selectedSegmentCount"] == 1
        assert payload["selectedSpeechSeconds"] == pytest.approx(4.0, abs=0.05)
        assert payload["durationSeconds"] == pytest.approx(4.0, abs=0.05)
        # output WAV is valid PCM readable by Python wave
        with wave.open(str(out_wav), "rb") as handle:
            assert handle.getnchannels() == 1
            assert handle.getsampwidth() == 2
            assert handle.getframerate() == 16000
            # ~4s of audio at 16kHz
            frames = handle.getnframes()
            assert abs(frames / 16000 - 4.0) < 0.1

    def test_concatenate_range_excludes_other_speakers(self, tmp_path):
        # Arrange
        wav_path = tmp_path / "full.wav"
        _write_wav(wav_path, _tone(20.0))
        segments_path = tmp_path / "segments.json"
        self._write_segments_json(
            segments_path,
            [
                {"speaker_id": "0", "start_time": 0.0, "end_time": 10.0, "text": "a"},
                {"speaker_id": "1", "start_time": 10.0, "end_time": 20.0, "text": "b"},
            ],
        )
        out_wav = tmp_path / "out.wav"
        out_json = tmp_path / "out.json"
        # Act: request 0-20 but only speaker 0 -> should produce 10s, not 20s
        exit_code = speaker_id.main(
            [
                "concatenate",
                "--audio", str(wav_path),
                "--segments", str(segments_path),
                "--speaker-id", "0",
                "--out", str(out_wav),
                "--no-best-segments",
                "--range", "0-20",
                "--json", str(out_json),
            ]
        )
        # Assert
        assert exit_code == 0
        payload = json.loads(out_json.read_text(encoding="utf-8"))
        assert payload["selectedSpeechSeconds"] == pytest.approx(10.0, abs=0.05)
        assert payload["durationSeconds"] == pytest.approx(10.0, abs=0.05)

    def test_concatenate_multiple_ranges(self, tmp_path):
        # Arrange: speaker 0 has segments at 0-10 and 20-30
        wav_path = tmp_path / "full.wav"
        _write_wav(wav_path, _tone(30.0))
        segments_path = tmp_path / "segments.json"
        self._write_segments_json(
            segments_path,
            [
                {"speaker_id": "0", "start_time": 0.0, "end_time": 10.0, "text": "a"},
                {"speaker_id": "1", "start_time": 10.0, "end_time": 20.0, "text": "b"},
                {"speaker_id": "0", "start_time": 20.0, "end_time": 30.0, "text": "c"},
            ],
        )
        out_wav = tmp_path / "out.wav"
        out_json = tmp_path / "out.json"
        # Act: request 1-4 and 22-27 (3s + 5s = 8s)
        exit_code = speaker_id.main(
            [
                "concatenate",
                "--audio", str(wav_path),
                "--segments", str(segments_path),
                "--speaker-id", "0",
                "--out", str(out_wav),
                "--no-best-segments",
                "--range", "1-4",
                "--range", "22-27",
                "--json", str(out_json),
            ]
        )
        # Assert
        assert exit_code == 0
        payload = json.loads(out_json.read_text(encoding="utf-8"))
        # JSON round-trip converts tuples to lists.
        assert payload["requestedRanges"] == [[1.0, 4.0], [22.0, 27.0]]
        assert payload["selectedRanges"] == [[1.0, 4.0], [22.0, 27.0]]
        assert payload["selectedSegmentCount"] == 2
        assert payload["selectedSpeechSeconds"] == pytest.approx(8.0, abs=0.05)
        assert payload["durationSeconds"] == pytest.approx(8.0, abs=0.05)

    def test_concatenate_no_range_backward_compatible(self, tmp_path):
        # Arrange
        wav_path = tmp_path / "full.wav"
        _write_wav(wav_path, _tone(20.0))
        segments_path = tmp_path / "segments.json"
        self._write_segments_json(
            segments_path,
            [
                {"speaker_id": "0", "start_time": 0.0, "end_time": 10.0, "text": "a"},
            ],
        )
        out_wav = tmp_path / "out.wav"
        out_json = tmp_path / "out.json"
        # Act: no --range
        exit_code = speaker_id.main(
            [
                "concatenate",
                "--audio", str(wav_path),
                "--segments", str(segments_path),
                "--speaker-id", "0",
                "--out", str(out_wav),
                "--no-best-segments",
                "--json", str(out_json),
            ]
        )
        # Assert
        assert exit_code == 0
        payload = json.loads(out_json.read_text(encoding="utf-8"))
        # backward-compatible: requestedRanges is None, full segment selected
        assert payload["requestedRanges"] is None
        assert payload["selectedRanges"] == [[0.0, 10.0]]
        assert payload["durationSeconds"] == pytest.approx(10.0, abs=0.05)

    def test_concatenate_range_no_usable_speech_specific_error(self, tmp_path):
        # Arrange: speaker 0 segment is at 0-10; request a non-overlapping range
        wav_path = tmp_path / "full.wav"
        _write_wav(wav_path, _tone(20.0))
        segments_path = tmp_path / "segments.json"
        self._write_segments_json(
            segments_path,
            [
                {"speaker_id": "0", "start_time": 0.0, "end_time": 10.0, "text": "a"},
            ],
        )
        out_wav = tmp_path / "out.wav"
        # Act: request 100-200 which does not overlap any segment
        exit_code = speaker_id.main(
            [
                "concatenate",
                "--audio", str(wav_path),
                "--segments", str(segments_path),
                "--speaker-id", "0",
                "--out", str(out_wav),
                "--range", "100-200",
            ]
        )
        # Assert: specific error message mentioning the requested range
        assert exit_code == 1

    def test_concatenate_range_respects_max_seconds(self, tmp_path):
        # Arrange
        wav_path = tmp_path / "full.wav"
        _write_wav(wav_path, _tone(20.0))
        segments_path = tmp_path / "segments.json"
        self._write_segments_json(
            segments_path,
            [
                {"speaker_id": "0", "start_time": 0.0, "end_time": 10.0, "text": "a"},
            ],
        )
        out_wav = tmp_path / "out.wav"
        out_json = tmp_path / "out.json"
        # Act: range 0-10 (10s), but max-seconds 3 -> capped to 3s
        exit_code = speaker_id.main(
            [
                "concatenate",
                "--audio", str(wav_path),
                "--segments", str(segments_path),
                "--speaker-id", "0",
                "--out", str(out_wav),
                "--no-best-segments",
                "--range", "0-10",
                "--max-seconds", "3",
                "--json", str(out_json),
            ]
        )
        # Assert
        assert exit_code == 0
        payload = json.loads(out_json.read_text(encoding="utf-8"))
        assert payload["durationSeconds"] == pytest.approx(3.0, abs=0.1)

    def test_concatenate_output_wav_readable_by_wave_module(self, tmp_path):
        # Arrange
        wav_path = tmp_path / "full.wav"
        _write_wav(wav_path, _tone(15.0))
        segments_path = tmp_path / "segments.json"
        self._write_segments_json(
            segments_path,
            [
                {"speaker_id": "0", "start_time": 0.0, "end_time": 12.0, "text": "a"},
            ],
        )
        out_wav = tmp_path / "out.wav"
        # Act
        speaker_id.main(
            [
                "concatenate",
                "--audio", str(wav_path),
                "--segments", str(segments_path),
                "--speaker-id", "0",
                "--out", str(out_wav),
                "--no-best-segments",
                "--range", "1-9",
            ]
        )
        # Assert: wave.open must succeed and report expected PCM params
        with wave.open(str(out_wav), "rb") as handle:
            assert handle.getnchannels() == 1
            assert handle.getsampwidth() == 2
            assert handle.getframerate() == 16000


class TestExtractRangeIntegration:
    """Integration/e2e: extract an embedding from selected ranges and verify
    unrelated ranges do not affect selected speech seconds."""

    def _write_segments_json(self, path, segments):
        path.write_text(json.dumps({"segments": segments}), encoding="utf-8")

    def test_extract_range_cli_includes_range_metadata(self, tmp_path):
        # Arrange
        wav_path = tmp_path / "full.wav"
        _write_wav(wav_path, _tone(20.0))
        segments_path = tmp_path / "segments.json"
        self._write_segments_json(
            segments_path,
            [
                {"speaker_id": "0", "start_time": 0.0, "end_time": 12.0, "text": "a"},
            ],
        )
        out_json = tmp_path / "out.json"
        # Act: extract from 1-9 (8s) with minimum 8s
        exit_code = speaker_id.main(
            [
                "extract",
                "--audio", str(wav_path),
                "--segments", str(segments_path),
                "--speaker-id", "0",
                "--provider", "mfcc-test",
                "--minimum-speech-seconds", "8",
                "--range", "1-9",
                "--json", str(out_json),
            ]
        )
        # Assert
        assert exit_code == 0
        payload = json.loads(out_json.read_text(encoding="utf-8"))
        assert payload["status"] == "ok"
        # JSON round-trip converts tuples to lists.
        assert payload["requestedRanges"] == [[1.0, 9.0]]
        assert payload["selectedRanges"] == [[1.0, 9.0]]
        assert payload["selectedSegmentCount"] == 1
        assert payload["selectedSpeechSeconds"] == pytest.approx(8.0, abs=0.05)
        assert payload["embedding"]

    def test_extract_range_unrelated_ranges_do_not_affect_selected_speech(self, tmp_path):
        # Arrange: speaker 0 has segments at 0-10 and 100-200 in the transcript,
        # but the WAV is only 20s long. We request a range that only overlaps
        # the first segment. The second segment (100-200) must NOT contribute.
        wav_path = tmp_path / "full.wav"
        _write_wav(wav_path, _tone(20.0))
        segments_path = tmp_path / "segments.json"
        self._write_segments_json(
            segments_path,
            [
                {"speaker_id": "0", "start_time": 0.0, "end_time": 10.0, "text": "a"},
                {"speaker_id": "0", "start_time": 100.0, "end_time": 200.0, "text": "b"},
            ],
        )
        out_json = tmp_path / "out.json"
        # Act: request only 0-8 (within the first segment)
        exit_code = speaker_id.main(
            [
                "extract",
                "--audio", str(wav_path),
                "--segments", str(segments_path),
                "--speaker-id", "0",
                "--provider", "mfcc-test",
                "--minimum-speech-seconds", "5",
                "--range", "0-8",
                "--json", str(out_json),
            ]
        )
        # Assert: only 8s of speech selected, not 8+100=108s
        assert exit_code == 0
        payload = json.loads(out_json.read_text(encoding="utf-8"))
        assert payload["status"] == "ok"
        assert payload["selectedSpeechSeconds"] == pytest.approx(8.0, abs=0.05)
        assert payload["durationSeconds"] == pytest.approx(8.0, abs=0.05)
        assert payload["selectedSegmentCount"] == 1

    def test_extract_range_excludes_other_speakers_speech(self, tmp_path):
        # Arrange: speaker 0 and 1 interleaved; range covers both but only 0 should count
        wav_path = tmp_path / "full.wav"
        _write_wav(wav_path, _tone(20.0))
        segments_path = tmp_path / "segments.json"
        self._write_segments_json(
            segments_path,
            [
                {"speaker_id": "0", "start_time": 0.0, "end_time": 10.0, "text": "a"},
                {"speaker_id": "1", "start_time": 10.0, "end_time": 20.0, "text": "b"},
            ],
        )
        out_json = tmp_path / "out.json"
        # Act: request 0-20 (covers both), but extracting speaker 0 -> 10s
        exit_code = speaker_id.main(
            [
                "extract",
                "--audio", str(wav_path),
                "--segments", str(segments_path),
                "--speaker-id", "0",
                "--provider", "mfcc-test",
                "--minimum-speech-seconds", "5",
                "--range", "0-20",
                "--json", str(out_json),
            ]
        )
        # Assert
        assert exit_code == 0
        payload = json.loads(out_json.read_text(encoding="utf-8"))
        assert payload["selectedSpeechSeconds"] == pytest.approx(10.0, abs=0.05)
        assert payload["durationSeconds"] == pytest.approx(10.0, abs=0.05)

    def test_extract_range_no_range_backward_compatible(self, tmp_path):
        # Arrange
        wav_path = tmp_path / "full.wav"
        _write_wav(wav_path, _tone(20.0))
        segments_path = tmp_path / "segments.json"
        self._write_segments_json(
            segments_path,
            [
                {"speaker_id": "0", "start_time": 0.0, "end_time": 12.0, "text": "a"},
            ],
        )
        out_json = tmp_path / "out.json"
        # Act: no --range
        exit_code = speaker_id.main(
            [
                "extract",
                "--audio", str(wav_path),
                "--segments", str(segments_path),
                "--speaker-id", "0",
                "--provider", "mfcc-test",
                "--minimum-speech-seconds", "8",
                "--json", str(out_json),
            ]
        )
        # Assert: backward compatible, requestedRanges is None
        assert exit_code == 0
        payload = json.loads(out_json.read_text(encoding="utf-8"))
        assert payload["requestedRanges"] is None
        # JSON round-trip converts tuples to lists.
        assert payload["selectedRanges"] == [[0.0, 12.0]]


class TestLabelSuggestions:
    """Unit tests for the pure, I/O-free label-suggestion grouping logic.

    These build precomputed ``match_candidate``-shaped results directly (no
    audio files, no ML backend, no profiles directory, no transcript
    mutation) and assert on ``build_label_suggestions`` output.
    """

    def _profile(self, profile_id, display_name=None):
        return {
            "id": profile_id,
            "displayName": display_name or f"Person {profile_id}",
            "embeddingProvider": "mfcc-test",
            "embeddingModel": "stt-vibevoice/mfcc-test-v1",
            "embedding": [1.0, 0.0],
        }

    def _best_match(self, profile_id, display_name, confidence, matched=True, status="matched", margin=0.5):
        return {
            "profileId": profile_id,
            "displayName": display_name,
            "confidence": confidence,
            "margin": margin,
            "matched": matched,
            "status": status,
        }

    def _match(self, best_match):
        return {
            "bestMatch": best_match,
            "candidates": [],
            "skippedProfiles": [],
            "warnings": [],
        }

    def _cluster(self, speaker_id, best_match, *, window_matches=None, source="system", **meta):
        cluster = {
            "speakerId": speaker_id,
            "source": source,
            "match": self._match(best_match),
        }
        cluster.update(meta)
        if window_matches is not None:
            cluster["windowMatches"] = window_matches
        return cluster

    def test_no_profiles_returns_no_profiles_result(self):
        cluster = self._cluster("0", self._best_match("a", "Person a", 0.9))
        result = speaker_id.build_label_suggestions(
            [cluster], profiles=[], threshold=0.78, margin=0.05
        )
        assert result["status"] == "no_profiles"
        assert result["profilesConsidered"] == {"count": 0, "profileIds": []}
        assert result["clusters"] == []
        assert result["duplicateClusterGroups"] == []
        assert result["mixedClusterWarnings"] == []
        # No profiles means everything is reported as unmatched.
        assert result["summary"] == {
            "clusterCount": 1,
            "matchedCount": 0,
            "duplicateGroupCount": 0,
            "mixedClusterCount": 0,
            "unmatchedCount": 1,
        }
        # Machine-readable + explanatory.
        assert isinstance(result["recommendation"], str)
        assert result["schemaVersion"] == speaker_id.LABEL_SUGGESTIONS_SCHEMA_VERSION

    def test_one_confident_match_produces_reuse_profile_recommendation(self):
        profiles = [self._profile("a", "Alice")]
        cluster = self._cluster(
            "0",
            self._best_match("a", "Alice", 0.95),
            durationSeconds=12.0,
            segmentCount=3,
            selectedRanges=[[0.0, 4.0], [6.0, 8.0]],
            speechSeconds=6.0,
        )
        result = speaker_id.build_label_suggestions(
            [cluster], profiles=profiles, threshold=0.78, margin=0.05
        )
        assert result["status"] == "ok"
        assert len(result["clusters"]) == 1
        c = result["clusters"][0]
        assert c["speakerId"] == "0"
        assert c["recommendation"] == "reuse_profile"
        assert c["bestMatch"]["profileId"] == "a"
        # Cluster metadata is echoed.
        assert c["durationSeconds"] == 12.0
        assert c["segmentCount"] == 3
        assert c["selectedRanges"] == [[0.0, 4.0], [6.0, 8.0]]
        assert c["speechSeconds"] == 6.0
        # A single confident match is NOT a duplicate group.
        assert result["duplicateClusterGroups"] == []
        assert result["mixedClusterWarnings"] == []
        assert result["summary"]["matchedCount"] == 1
        assert result["summary"]["unmatchedCount"] == 0
        assert result["summary"]["duplicateGroupCount"] == 0

    def test_two_clusters_same_profile_produce_one_duplicate_group(self):
        profiles = [self._profile("a", "Alice")]
        clusters = [
            self._cluster("1", self._best_match("a", "Alice", 0.95), selectedRanges=[[0.0, 4.0]]),
            self._cluster("2", self._best_match("a", "Alice", 0.92), selectedRanges=[[8.0, 12.0]]),
        ]
        result = speaker_id.build_label_suggestions(
            clusters, profiles=profiles, threshold=0.78, margin=0.05
        )
        groups = result["duplicateClusterGroups"]
        assert len(groups) == 1
        group = groups[0]
        assert group["profileId"] == "a"
        assert group["nameHint"] == "Alice"
        assert group["displayName"] == "Alice"
        assert group["recommendation"] == "merge_or_relabel"
        # Both clusters are listed, with confidence values, sorted by speakerId.
        member_ids = [m["speakerId"] for m in group["clusters"]]
        assert member_ids == ["1", "2"]
        confidences = {m["speakerId"]: m["confidence"] for m in group["clusters"]}
        assert confidences["1"] == 0.95
        assert confidences["2"] == 0.92
        assert result["summary"]["duplicateGroupCount"] == 1
        assert result["summary"]["matchedCount"] == 2

    def test_clusters_matching_different_profiles_do_not_duplicate_group(self):
        # Two clusters confidently matching *different* profiles are NOT a
        # duplicate group -- they are simply two good distinct matches.
        profiles = [self._profile("a", "Alice"), self._profile("b", "Bob")]
        clusters = [
            self._cluster("1", self._best_match("a", "Alice", 0.95)),
            self._cluster("2", self._best_match("b", "Bob", 0.93)),
        ]
        result = speaker_id.build_label_suggestions(
            clusters, profiles=profiles, threshold=0.78, margin=0.05
        )
        assert result["duplicateClusterGroups"] == []
        assert result["summary"]["matchedCount"] == 2

    def test_windows_matching_different_profiles_produce_mixed_warning(self):
        profiles = [self._profile("a", "Alice"), self._profile("b", "Bob")]
        window_matches = [
            {
                "label": "early",
                "range": [0.0, 6.0],
                "match": self._match(self._best_match("a", "Alice", 0.95)),
            },
            {
                "label": "late",
                "range": [10.0, 16.0],
                "match": self._match(self._best_match("b", "Bob", 0.93)),
            },
        ]
        cluster = self._cluster("0", None, window_matches=window_matches)
        result = speaker_id.build_label_suggestions(
            [cluster], profiles=profiles, threshold=0.78, margin=0.05
        )
        warnings = result["mixedClusterWarnings"]
        assert len(warnings) == 1
        w = warnings[0]
        assert w["speakerId"] == "0"
        assert w["conflictingProfileIds"] == ["a", "b"]
        assert w["conflictingDisplayNames"] == ["Alice", "Bob"]
        assert w["recommendation"] == "do_not_enroll_whole_cluster"
        # Window evidence is preserved.
        assert len(w["windows"]) == 2
        assert w["windows"][0]["matchedProfileId"] == "a"
        assert w["windows"][1]["matchedProfileId"] == "b"
        assert result["summary"]["mixedClusterCount"] == 1

    def test_windows_matching_same_profile_do_not_warn(self):
        profiles = [self._profile("a", "Alice")]
        window_matches = [
            {"label": "early", "range": [0.0, 6.0],
             "match": self._match(self._best_match("a", "Alice", 0.95))},
            {"label": "late", "range": [10.0, 16.0],
             "match": self._match(self._best_match("a", "Alice", 0.94))},
        ]
        cluster = self._cluster("0", self._best_match("a", "Alice", 0.95), window_matches=window_matches)
        result = speaker_id.build_label_suggestions(
            [cluster], profiles=profiles, threshold=0.78, margin=0.05
        )
        assert result["mixedClusterWarnings"] == []

    def test_output_ordering_is_deterministic_for_unsorted_input(self):
        # Feed clusters out of speakerId order; output must be sorted.
        profiles = [
            self._profile("z", "Zed"),
            self._profile("a", "Alice"),
        ]
        clusters = [
            self._cluster("3", self._best_match("a", "Alice", 0.95)),
            self._cluster("1", self._best_match("a", "Alice", 0.94)),
            self._cluster("2", self._best_match("z", "Zed", 0.90)),
        ]
        result = speaker_id.build_label_suggestions(
            clusters, profiles=profiles, threshold=0.78, margin=0.05
        )
        # Cluster suggestions sorted by speakerId.
        assert [c["speakerId"] for c in result["clusters"]] == ["1", "2", "3"]
        # Duplicate group for profile 'a' lists members sorted by speakerId.
        group = result["duplicateClusterGroups"][0]
        assert group["profileId"] == "a"
        assert [m["speakerId"] for m in group["clusters"]] == ["1", "3"]
        # Duplicate groups sorted by profileId ('a' before 'z' is irrelevant
        # here since only 'a' has 2+ members, but profilesConsidered is sorted).
        assert result["profilesConsidered"]["profileIds"] == ["z", "a"]

    def test_below_threshold_best_match_is_not_confident(self):
        # ``matched: False`` (below threshold / ambiguous) must not count as a
        # confident match even though bestMatch is present.
        profiles = [self._profile("a", "Alice"), self._profile("b", "Bob")]
        cluster = self._cluster(
            "0", self._best_match("a", "Alice", 0.6, matched=False, status="below_threshold")
        )
        result = speaker_id.build_label_suggestions(
            [cluster], profiles=profiles, threshold=0.78, margin=0.05
        )
        c = result["clusters"][0]
        assert c["recommendation"] == "no_confident_match"
        assert result["duplicateClusterGroups"] == []
        assert result["summary"]["matchedCount"] == 0
        assert result["summary"]["unmatchedCount"] == 1

    def test_config_and_provenance_are_echoed(self):
        profiles = [self._profile("a", "Alice")]
        cluster = self._cluster("0", self._best_match("a", "Alice", 0.95))
        result = speaker_id.build_label_suggestions(
            [cluster],
            profiles=profiles,
            threshold=0.82,
            margin=0.07,
            session="sess-123",
            provider="speechbrain",
            model="spkrec",
            generated_at="2026-01-01T00:00:00+00:00",
        )
        assert result["config"] == {
            "threshold": 0.82,
            "margin": 0.07,
            "provider": "speechbrain",
            "model": "spkrec",
        }
        assert result["session"] == "sess-123"
        assert result["generatedAt"] == "2026-01-01T00:00:00+00:00"
        assert result["profilesConsidered"] == {"count": 1, "profileIds": ["a"]}


class TestSuggestLabelsAdapter:
    """Unit tests for the ``suggest_labels`` matching adapter (task 06b).

    These exercise the orchestration (extraction -> matching -> grouping) with
    *mocked* extraction and matching hooks so no audio, ML backend, profiles
    directory, or transcript mutation is required. The adapter must:
      - map mocked extraction/match outputs into the grouping input shape,
      - handle no-profiles and no-usable-speech states cleanly (non-fatal),
      - pass threshold/margin through to match and into the output config,
      - never write files or mutate inputs,
      - produce duplicate and mixed-cluster evidence from adapter inputs.
    """

    # -- factory helpers -------------------------------------------------

    def _profile(self, profile_id, display_name=None, embedding=None):
        return {
            "id": profile_id,
            "displayName": display_name or f"Person {profile_id}",
            "embeddingProvider": "mfcc-test",
            "embeddingModel": "stt-vibevoice/mfcc-test-v1",
            "embedding": embedding or [1.0, 0.0],
        }

    def _segment(self, speaker_id, start, end, *, source="mic", text="hello"):
        return {
            "speaker_id": str(speaker_id),
            "source": source,
            "start_time": start,
            "end_time": end,
            "text": text,
        }

    def _ok_extraction(self, speaker_id, embedding, *, duration=10.0, segment_count=2,
                       ranges=None, model="stt-vibevoice/mfcc-test-v1"):
        return {
            "speakerId": str(speaker_id),
            "provider": "mfcc-test",
            "model": model,
            "embedding": embedding,
            "durationSeconds": duration,
            "segmentCount": segment_count,
            "status": "ok",
            "selectedRanges": ranges or [[0.0, 5.0], [5.0, 10.0]],
            "selectedSpeechSeconds": duration,
        }

    def _too_short_extraction(self, speaker_id):
        return {
            "speakerId": str(speaker_id),
            "provider": "mfcc-test",
            "model": None,
            "embedding": None,
            "durationSeconds": 0.5,
            "segmentCount": 0,
            "status": "too_short",
            "selectedRanges": [],
            "selectedSpeechSeconds": 0.5,
        }

    # -- core mapping ----------------------------------------------------

    def test_maps_mocked_extraction_and_match_into_grouping_shape(self, tmp_path):
        """The adapter extracts one embedding per cluster, matches it, and feeds
        the result into build_label_suggestions in the correct shape."""
        profiles = [self._profile("a", "Alice")]
        segments = [
            self._segment("0", 0.0, 5.0),
            self._segment("0", 6.0, 11.0),
            self._segment("1", 0.0, 8.0, source="system"),
        ]
        extract_calls = []
        match_calls = []

        def fake_extract(segs, *, audio_paths, source, provider, minimum_speech_seconds):
            sid = str(segs[0]["speaker_id"])
            extract_calls.append((sid, source))
            return self._ok_extraction(sid, [1.0, 0.0])

        def fake_match(candidate, profs, threshold, margin):
            # Always match the first profile confidently.
            p = profs[0]
            match_calls.append((candidate["speakerId"], threshold, margin))
            return {
                "bestMatch": {
                    "profileId": p["id"],
                    "displayName": p["displayName"],
                    "confidence": 0.95,
                    "margin": 0.5,
                    "matched": True,
                    "status": "matched",
                },
                "candidates": [],
                "skippedProfiles": [],
                "warnings": [],
            }

        result = speaker_id.suggest_labels(
            segments=segments,
            audio_paths={"mic": tmp_path / "mic.wav", "system": tmp_path / "system.wav"},
            profiles=profiles,
            provider="mfcc-test",
            threshold=0.78,
            margin=0.05,
            extract_cluster_fn=fake_extract,
            match_fn=fake_match,
        )
        assert result["schemaVersion"] == speaker_id.LABEL_SUGGESTIONS_SCHEMA_VERSION
        assert result["status"] == "ok"
        # Both clusters extracted + matched.
        assert sorted(sid for sid, _ in extract_calls) == ["0", "1"]
        assert sorted(sid for sid, _, _ in match_calls) == ["0", "1"]
        # Source resolved per cluster from the transcript.
        sources = dict(extract_calls)
        assert sources["0"] == "mic"
        assert sources["1"] == "system"
        # Both clusters matched profile 'a' -> one duplicate group.
        assert result["summary"]["matchedCount"] == 2
        assert result["summary"]["duplicateGroupCount"] == 1

    def test_threshold_and_margin_passed_to_match_and_output_config(self, tmp_path):
        profiles = [self._profile("a", "Alice")]
        seen_thresholds = []
        seen_margins = []

        def fake_extract(segs, **kw):
            return self._ok_extraction(segs[0]["speaker_id"], [1.0, 0.0])

        def fake_match(candidate, profs, threshold, margin):
            seen_thresholds.append(threshold)
            seen_margins.append(margin)
            return {"bestMatch": None, "candidates": [], "skippedProfiles": [], "warnings": []}

        result = speaker_id.suggest_labels(
            segments=[self._segment("0", 0.0, 8.0)],
            audio_paths={"mic": tmp_path / "mic.wav"},
            profiles=profiles,
            provider="mfcc-test",
            threshold=0.82,
            margin=0.07,
            extract_cluster_fn=fake_extract,
            match_fn=fake_match,
        )
        assert seen_thresholds == [0.82]
        assert seen_margins == [0.07]
        assert result["config"]["threshold"] == 0.82
        assert result["config"]["margin"] == 0.07

    # -- clean edge states ----------------------------------------------

    def test_no_profiles_returns_no_profiles_status(self, tmp_path):
        def fake_extract(segs, **kw):
            return self._ok_extraction(segs[0]["speaker_id"], [1.0, 0.0])

        result = speaker_id.suggest_labels(
            segments=[self._segment("0", 0.0, 8.0)],
            audio_paths={"mic": tmp_path / "mic.wav"},
            profiles=[],
            provider="mfcc-test",
            threshold=0.78,
            margin=0.05,
            extract_cluster_fn=fake_extract,
        )
        assert result["status"] == "no_profiles"
        assert result["profilesConsidered"] == {"count": 0, "profileIds": []}
        assert result["clusters"] == []
        assert result["summary"]["unmatchedCount"] == 1

    def test_no_usable_speech_is_non_fatal(self, tmp_path):
        profiles = [self._profile("a", "Alice")]

        def fake_extract(segs, **kw):
            return self._too_short_extraction(segs[0]["speaker_id"])

        result = speaker_id.suggest_labels(
            segments=[self._segment("0", 0.0, 0.3)],
            audio_paths={"mic": tmp_path / "mic.wav"},
            profiles=profiles,
            provider="mfcc-test",
            threshold=0.78,
            margin=0.05,
            extract_cluster_fn=fake_extract,
        )
        # Status is still 'ok' (grouping ran); the cluster just didn't match.
        assert result["status"] == "ok"
        assert len(result["clusters"]) == 1
        assert result["clusters"][0]["recommendation"] == "no_confident_match"
        assert result["summary"]["matchedCount"] == 0
        assert result["summary"]["unmatchedCount"] == 1
        # No duplicate groups or mixed warnings from a too-short cluster.
        assert result["duplicateClusterGroups"] == []
        assert result["mixedClusterWarnings"] == []

    def test_missing_audio_path_is_non_fatal_with_default_hook(self, tmp_path):
        """A cluster whose resolved audio WAV does not exist on disk must NOT
        crash the adapter (real default extraction hook, no mocks). It degrades
        to a non-fatal no-confident_match suggestion rather than raising."""
        profiles = [self._profile("a", "Alice")]
        result = speaker_id.suggest_labels(
            segments=[self._segment("0", 0.0, 8.0)],
            audio_paths={"mic": tmp_path / "does_not_exist.wav"},
            profiles=profiles,
            provider="mfcc-test",
            threshold=0.78,
            margin=0.05,
        )
        assert result["status"] == "ok"
        assert len(result["clusters"]) == 1
        assert result["clusters"][0]["recommendation"] == "no_confident_match"
        assert result["summary"]["matchedCount"] == 0
        assert result["summary"]["unmatchedCount"] == 1

    def test_mixed_speech_and_no_speech_clusters(self, tmp_path):
        """A cluster with speech + a cluster without speech both flow through."""
        profiles = [self._profile("a", "Alice")]

        def fake_extract(segs, **kw):
            sid = str(segs[0]["speaker_id"])
            if sid == "0":
                return self._ok_extraction(sid, [1.0, 0.0])
            return self._too_short_extraction(sid)

        def fake_match(candidate, profs, threshold, margin):
            p = profs[0]
            return {
                "bestMatch": {
                    "profileId": p["id"], "displayName": p["displayName"],
                    "confidence": 0.95, "margin": 0.5, "matched": True,
                    "status": "matched",
                },
                "candidates": [], "skippedProfiles": [], "warnings": [],
            }

        result = speaker_id.suggest_labels(
            segments=[
                self._segment("0", 0.0, 8.0),
                self._segment("1", 0.0, 0.3),
            ],
            audio_paths={"mic": tmp_path / "mic.wav"},
            profiles=profiles,
            provider="mfcc-test",
            threshold=0.78,
            margin=0.05,
            extract_cluster_fn=fake_extract,
            match_fn=fake_match,
        )
        assert result["summary"]["clusterCount"] == 2
        assert result["summary"]["matchedCount"] == 1
        assert result["summary"]["unmatchedCount"] == 1

    # -- non-mutation ----------------------------------------------------

    def test_adapter_does_not_mutate_segments_or_profiles(self, tmp_path):
        import copy

        profiles = [self._profile("a", "Alice")]
        segments = [self._segment("0", 0.0, 8.0)]
        profiles_snapshot = copy.deepcopy(profiles)
        segments_snapshot = copy.deepcopy(segments)

        def fake_extract(segs, **kw):
            return self._ok_extraction(segs[0]["speaker_id"], [1.0, 0.0])

        def fake_match(candidate, profs, threshold, margin):
            return {"bestMatch": None, "candidates": [], "skippedProfiles": [], "warnings": []}

        speaker_id.suggest_labels(
            segments=segments,
            audio_paths={"mic": tmp_path / "mic.wav"},
            profiles=profiles,
            provider="mfcc-test",
            threshold=0.78,
            margin=0.05,
            extract_cluster_fn=fake_extract,
            match_fn=fake_match,
        )
        assert profiles == profiles_snapshot
        assert segments == segments_snapshot

    def test_adapter_writes_no_files(self, tmp_path):
        before = set(p.name for p in tmp_path.iterdir())

        def fake_extract(segs, **kw):
            return self._ok_extraction(segs[0]["speaker_id"], [1.0, 0.0])

        speaker_id.suggest_labels(
            segments=[self._segment("0", 0.0, 8.0)],
            audio_paths={"mic": tmp_path / "mic.wav"},
            profiles=[self._profile("a", "Alice")],
            provider="mfcc-test",
            threshold=0.78,
            margin=0.05,
            extract_cluster_fn=fake_extract,
        )
        after = set(p.name for p in tmp_path.iterdir())
        assert before == after, f"adapter wrote files: {after - before}"

    # -- duplicate + mixed evidence --------------------------------------

    def test_two_clusters_same_profile_produce_duplicate_group(self, tmp_path):
        profiles = [self._profile("a", "Alice")]

        def fake_extract(segs, **kw):
            return self._ok_extraction(segs[0]["speaker_id"], [1.0, 0.0])

        def fake_match(candidate, profs, threshold, margin):
            p = profs[0]
            return {
                "bestMatch": {
                    "profileId": p["id"], "displayName": p["displayName"],
                    "confidence": 0.93, "margin": 0.4, "matched": True,
                    "status": "matched",
                },
                "candidates": [], "skippedProfiles": [], "warnings": [],
            }

        result = speaker_id.suggest_labels(
            segments=[self._segment("0", 0.0, 8.0), self._segment("1", 0.0, 8.0)],
            audio_paths={"mic": tmp_path / "mic.wav"},
            profiles=profiles,
            provider="mfcc-test",
            threshold=0.78,
            margin=0.05,
            extract_cluster_fn=fake_extract,
            match_fn=fake_match,
        )
        groups = result["duplicateClusterGroups"]
        assert len(groups) == 1
        assert groups[0]["profileId"] == "a"
        assert [m["speakerId"] for m in groups[0]["clusters"]] == ["0", "1"]

    def test_windows_matching_different_profiles_produce_mixed_warning(self, tmp_path):
        profiles = [self._profile("a", "Alice"), self._profile("b", "Bob", embedding=[0.0, 1.0])]

        def fake_extract_cluster(segs, **kw):
            return self._ok_extraction(
                segs[0]["speaker_id"], [1.0, 0.0],
                ranges=[[0.0, 9.0]], duration=9.0, segment_count=1,
            )

        def fake_extract_window(segs, *, window_range, **kw):
            return self._ok_extraction(
                segs[0]["speaker_id"], [1.0, 0.0],
                ranges=[list(window_range)], duration=3.0,
            )

        # The adapter calls match once per whole cluster then once per window.
        # For one cluster with 3 windows: call 1 = whole (no match),
        # calls 2..4 = windows (alternating profiles to force a conflict).
        call_index = {"n": 0}
        window_profiles = [("a", "Alice", 0.95), ("b", "Bob", 0.93), ("a", "Alice", 0.94)]

        def fake_match_stateful(candidate, profs, threshold, margin):
            call_index["n"] += 1
            n = call_index["n"]
            if n == 1:
                # whole-cluster match: no confident match
                return {"bestMatch": None, "candidates": [], "skippedProfiles": [], "warnings": []}
            pid, name, conf = window_profiles[n - 2]
            return {
                "bestMatch": {
                    "profileId": pid, "displayName": name, "confidence": conf,
                    "margin": 0.5, "matched": True, "status": "matched",
                },
                "candidates": [], "skippedProfiles": [], "warnings": [],
            }

        result = speaker_id.suggest_labels(
            segments=[self._segment("0", 0.0, 9.0)],
            audio_paths={"mic": tmp_path / "mic.wav"},
            profiles=profiles,
            provider="mfcc-test",
            threshold=0.78,
            margin=0.05,
            n_windows=3,
            extract_cluster_fn=fake_extract_cluster,
            extract_window_fn=fake_extract_window,
            match_fn=fake_match_stateful,
        )
        warnings = result["mixedClusterWarnings"]
        assert len(warnings) == 1
        w = warnings[0]
        assert w["speakerId"] == "0"
        assert set(w["conflictingProfileIds"]) == {"a", "b"}
        assert w["recommendation"] == "do_not_enroll_whole_cluster"
        assert len(w["windows"]) == 3

    # -- provenance + ordering -------------------------------------------

    def test_model_captured_from_extraction_and_echoed_in_config(self, tmp_path):
        def fake_extract(segs, **kw):
            return self._ok_extraction(segs[0]["speaker_id"], [1.0, 0.0],
                                      model="custom-model-v2")

        result = speaker_id.suggest_labels(
            segments=[self._segment("0", 0.0, 8.0)],
            audio_paths={"mic": tmp_path / "mic.wav"},
            profiles=[self._profile("a", "Alice")],
            provider="mfcc-test",
            threshold=0.78,
            margin=0.05,
            session="sess-42",
            generated_at="2026-07-11T00:00:00+00:00",
            extract_cluster_fn=fake_extract,
        )
        assert result["config"]["model"] == "custom-model-v2"
        assert result["config"]["provider"] == "mfcc-test"
        assert result["session"] == "sess-42"
        assert result["generatedAt"] == "2026-07-11T00:00:00+00:00"

    def test_clusters_processed_in_numeric_order(self, tmp_path):
        """Speaker ids 0..10 should be processed in numeric order, not lexical.

        (The pure ``build_label_suggestions`` re-sorts the output lexically by
        speakerId; this test verifies the adapter's own extraction order is
        numeric-aware so model/provenance capture is deterministic.)"""
        order = []

        def fake_extract(segs, **kw):
            sid = str(segs[0]["speaker_id"])
            order.append(sid)
            return self._ok_extraction(sid, [1.0, 0.0])

        segments = [self._segment(str(i), 0.0, 8.0) for i in [10, 2, 1, 0]]
        speaker_id.suggest_labels(
            segments=segments,
            audio_paths={"mic": tmp_path / "mic.wav"},
            profiles=[self._profile("a", "Alice")],
            provider="mfcc-test",
            threshold=0.78,
            margin=0.05,
            extract_cluster_fn=fake_extract,
        )
        assert order == ["0", "1", "2", "10"]

    # -- load_profiles helper --------------------------------------------

    def test_load_profiles_empty_when_dir_missing(self, tmp_path):
        assert speaker_id.load_profiles(tmp_path / "nope") == []

    def test_load_profiles_empty_when_dir_empty(self, tmp_path):
        assert speaker_id.load_profiles(tmp_path) == []

    def test_load_profiles_reads_profile_jsons(self, tmp_path):
        (tmp_path / "alice.json").write_text(json.dumps({
            "id": "alice", "displayName": "Alice",
            "embeddingProvider": "mfcc-test",
            "embeddingModel": "stt-vibevoice/mfcc-test-v1",
            "embedding": [1.0, 0.0],
        }), encoding="utf-8")
        (tmp_path / "bob.json").write_text(json.dumps({
            "id": "bob", "displayName": "Bob",
            "embeddingProvider": "mfcc-test",
            "embeddingModel": "stt-vibevoice/mfcc-test-v1",
            "embedding": [0.0, 1.0],
        }), encoding="utf-8")
        # index.json should be skipped.
        (tmp_path / "index.json").write_text(json.dumps({"order": []}), encoding="utf-8")
        # Malformed JSON should be skipped (best-effort).
        (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")

        loaded = speaker_id.load_profiles(tmp_path)
        ids = sorted(p["id"] for p in loaded)
        assert ids == ["alice", "bob"]

    def test_load_profiles_none_returns_empty(self):
        assert speaker_id.load_profiles(None) == []

    # -- window splitter (pure helper) -----------------------------------

    def test_split_ranges_into_windows_by_speech_duration(self):
        # 20s of speech across two ranges with a gap: split into 3 windows.
        windows = speaker_id._split_ranges_into_windows([(0.0, 10.0), (20.0, 30.0)], 3)
        assert len(windows) == 3
        labels = [label for label, _ in windows]
        assert labels == ["window-1", "window-2", "window-3"]
        # Each window bounding range is a valid (start < end) pair.
        for _, (s, e) in windows:
            assert e > s

    def test_split_ranges_zero_windows_returns_empty(self):
        assert speaker_id._split_ranges_into_windows([(0.0, 10.0)], 0) == []

    def test_split_ranges_empty_ranges_returns_empty(self):
        assert speaker_id._split_ranges_into_windows([], 3) == []

    def test_split_ranges_contiguous_ranges(self):
        windows = speaker_id._split_ranges_into_windows([(0.0, 3.0), (3.0, 6.0), (6.0, 9.0)], 3)
        assert len(windows) == 3
        # Contiguous ranges -> windows tile perfectly with no gaps.
        assert windows[0][1][0] == 0.0
        assert windows[-1][1][1] == 9.0

    # -- source resolution -----------------------------------------------

    def test_resolve_source_wav_explicit_source(self):
        paths = {"mic": Path("/a/m.wav"), "system": Path("/a/s.wav")}
        assert speaker_id._resolve_source_wav(paths, "mic") == Path("/a/m.wav")
        assert speaker_id._resolve_source_wav(paths, "system") == Path("/a/s.wav")

    def test_resolve_source_wav_falls_back_to_single_path(self):
        paths = {"default": Path("/a/x.wav")}
        assert speaker_id._resolve_source_wav(paths, None) == Path("/a/x.wav")

    def test_resolve_source_wav_none_when_empty(self):
        assert speaker_id._resolve_source_wav({}, "mic") is None

    def test_resolve_source_wav_case_insensitive(self):
        paths = {"Mic": Path("/a/m.wav")}
        assert speaker_id._resolve_source_wav(paths, "mic") == Path("/a/m.wav")

    # -- default extraction uses real backend (fast mfcc-test) -----------

    def test_default_extract_cluster_uses_real_backend(self, tmp_path):
        """Integration-flavored unit test: the default extraction hook actually
        slices audio via the existing backend. Uses the fast mfcc-test provider
        on a tiny generated WAV so no heavy ML is loaded."""
        wav = tmp_path / "mic.wav"
        _write_wav(wav, _tone(10.0))
        segments = [
            {"speaker_id": "0", "source": "mic", "start_time": 0.0, "end_time": 5.0, "text": "hi"},
            {"speaker_id": "0", "source": "mic", "start_time": 5.0, "end_time": 10.0, "text": "yo"},
        ]
        result = speaker_id._default_extract_cluster(
            segments,
            audio_paths={"mic": wav},
            source="mic",
            provider="mfcc-test",
            minimum_speech_seconds=8.0,
        )
        assert result["status"] == "ok"
        assert result["embedding"] is not None
        assert result["model"] == speaker_id._mfcc_test_model_id()
