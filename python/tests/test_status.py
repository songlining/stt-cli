from __future__ import annotations

import json
import tomllib
from pathlib import Path

import stt_vibevoice.status as status_module
from stt_vibevoice.status import (
    REQUIRED_MODULES,
    RUNTIME_ENV_VAR,
    doctor,
    format_report,
    main,
    missing_requirements,
    setup_hint,
)


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


class TestSetupHint:
    def test_missing_requirements_lists_missing_modules_and_tools(self):
        report = {
            "platform": {"is_apple_silicon": False},
            "binaries": {"ffmpeg": None, "ffprobe": "/opt/homebrew/bin/ffprobe"},
            "modules": {
                "mlx": False,
                "mlx_audio": False,
                "huggingface_hub": True,
                "sentencepiece": False,
            },
        }

        missing = missing_requirements(report)

        assert "Apple Silicon macOS" in missing
        assert "ffmpeg" in missing
        assert "python module mlx" in missing
        assert "python module mlx_audio" in missing
        assert "python module sentencepiece" in missing
        assert "ffprobe" not in missing
        assert "python module huggingface_hub" not in missing

    def test_setup_hint_is_actionable_when_missing_dependencies(self):
        report = {
            "platform": {"is_apple_silicon": True},
            "binaries": {"ffmpeg": "/opt/homebrew/bin/ffmpeg", "ffprobe": "/opt/homebrew/bin/ffprobe"},
            "modules": {
                "mlx": False,
                "mlx_audio": False,
                "huggingface_hub": True,
                "sentencepiece": False,
            },
        }

        hint = setup_hint(report)

        assert "missing:" in hint
        assert "python module mlx" in hint
        assert "pip install 'mlx-audio[stt]'" in hint

    def test_setup_hint_reports_ready_when_nothing_missing(self):
        report = {
            "platform": {"is_apple_silicon": True},
            "binaries": {"ffmpeg": "/opt/homebrew/bin/ffmpeg", "ffprobe": "/opt/homebrew/bin/ffprobe"},
            "modules": {module: True for module in REQUIRED_MODULES},
        }

        assert setup_hint(report) == "backend ready; no setup needed"


class TestFormatReport:
    def test_format_report_produces_readable_text(self):
        report = doctor()
        text = format_report(report)
        assert isinstance(text, str)
        assert "Apple Silicon" in text
        assert "ffmpeg" in text
        assert "overall ready" in text

    def test_format_report_includes_setup_hint_when_not_ready(self):
        report = {
            "platform": {"system": "Darwin", "machine": "arm64", "is_apple_silicon": True},
            "binaries": {"ffmpeg": "/opt/homebrew/bin/ffmpeg", "ffprobe": "/opt/homebrew/bin/ffprobe"},
            "modules": {
                "mlx": False,
                "mlx_audio": False,
                "huggingface_hub": True,
                "sentencepiece": False,
            },
            "runtime": {"configured": False},
            "model_cache": {"path": "/tmp/cache", "exists": False},
            "ready": False,
        }

        text = format_report(report)

        assert "setup hint:" in text
        assert "mlx-audio[stt]" in text


class TestPackaging:
    def test_console_script_points_to_cli_main(self):
        pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
        pyproject = tomllib.loads(pyproject_path.read_text())

        assert pyproject["project"]["scripts"]["stt-vibevoice-doctor"] == "stt_vibevoice.status:main"


class TestStatusCLI:
    def test_main_prints_json_report(self, monkeypatch, capsys):
        monkeypatch.setattr(
            status_module,
            "doctor",
            lambda: {
                "platform": {"is_apple_silicon": True},
                "binaries": {"ffmpeg": "/bin/ffmpeg", "ffprobe": "/bin/ffprobe"},
                "modules": {module: True for module in REQUIRED_MODULES},
                "runtime": {"configured": False},
                "model_cache": {"path": "/tmp/cache", "exists": False},
                "ready": True,
            },
        )

        exit_code = main(["--json"])
        captured = capsys.readouterr()
        payload = json.loads(captured.out)

        assert exit_code == 0
        assert payload["ready"] is True

    def test_main_can_fail_when_backend_not_ready(self, monkeypatch, capsys):
        monkeypatch.setattr(
            status_module,
            "doctor",
            lambda: {
                "platform": {"system": "Darwin", "machine": "arm64", "is_apple_silicon": True},
                "binaries": {"ffmpeg": "/bin/ffmpeg", "ffprobe": "/bin/ffprobe"},
                "modules": {module: False for module in REQUIRED_MODULES},
                "runtime": {"configured": False},
                "model_cache": {"path": "/tmp/cache", "exists": False},
                "ready": False,
            },
        )

        exit_code = main(["--fail-if-not-ready"])
        captured = capsys.readouterr()

        assert exit_code == 1
        assert "overall ready: no" in captured.out
