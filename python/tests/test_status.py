from __future__ import annotations

from stt_vibevoice.status import REQUIRED_MODULES, RUNTIME_ENV_VAR, doctor, format_report


class TestDoctor:
    def test_doctor_returns_expected_top_level_keys(self):
        report = doctor()
        for key in ("platform", "binaries", "modules", "modules_all_available", "runtime", "model_cache", "ready"):
            assert key in report

    def test_doctor_platform_section_has_expected_keys(self):
        report = doctor()
        platform_info = report["platform"]
        assert "system" in platform_info
        assert "machine" in platform_info
        assert "is_apple_silicon" in platform_info
        assert isinstance(platform_info["is_apple_silicon"], bool)

    def test_doctor_binaries_section_has_expected_keys(self):
        report = doctor()
        assert "ffmpeg" in report["binaries"]
        assert "ffprobe" in report["binaries"]

    def test_doctor_modules_section_covers_required_modules(self):
        report = doctor()
        for module_name in REQUIRED_MODULES:
            assert module_name in report["modules"]
            assert isinstance(report["modules"][module_name], bool)

    def test_doctor_does_not_raise_regardless_of_mlx_availability(self):
        # This test passes whether or not mlx/mlx_audio are actually
        # installed in the current environment - doctor() must never raise.
        report = doctor()
        assert isinstance(report, dict)
        assert isinstance(report["ready"], bool)

    def test_doctor_runtime_section_reflects_env_var(self, monkeypatch, tmp_path):
        monkeypatch.delenv(RUNTIME_ENV_VAR, raising=False)
        report = doctor()
        assert report["runtime"]["configured"] is False

        runtime_dir = tmp_path / "runtime"
        runtime_dir.mkdir()
        monkeypatch.setenv(RUNTIME_ENV_VAR, str(runtime_dir))
        report_with_env = doctor()
        assert report_with_env["runtime"]["configured"] is True
        assert report_with_env["runtime"]["path"] == str(runtime_dir)
        # No .venv/bin/python created, so this should be False, not raise.
        assert report_with_env["runtime"]["python_exists"] is False

    def test_doctor_model_cache_section_present(self):
        report = doctor()
        assert "path" in report["model_cache"]
        assert "exists" in report["model_cache"]
        assert "entries" in report["model_cache"]


class TestFormatReport:
    def test_format_report_produces_readable_text(self):
        report = doctor()
        text = format_report(report)
        assert isinstance(text, str)
        assert "Apple Silicon" in text
        assert "ffmpeg" in text
        assert "overall ready" in text
