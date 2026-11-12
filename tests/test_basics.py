from pathlib import Path

from typer.testing import CliRunner

from autocortes import __version__
from autocortes.cli import app
from autocortes.config import load_config
from autocortes.doctor import _listed

ROOT = Path(__file__).resolve().parents[1]
runner = CliRunner()


def test_version():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_config_yaml_matches_schema():
    config = load_config(ROOT / "config.yaml")
    assert (config.render.width, config.render.height) == (1080, 1920)
    assert config.llm.model == "claude-opus-5-5"


def test_doctor_runs():
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code in (0, 1), result.output  # 1 = falta algo obrigatório (ex.: ffmpeg)
    assert "ffmpeg" in result.output


FFMPEG_FILTERS = """\
Filters:
  T.. = Timeline support
 ... ass               V->V       Render ASS subtitles onto input video.
 ... subtitles         V->V       Render text subtitles onto input video.
"""

FFMPEG_ENCODERS = """\
 V....D libx264              libx264 H.264 / AVC / MPEG-4 AVC (codec h264)
 V....D h264_nvenc           NVIDIA NVENC H.264 encoder (codec h264)
"""


def test_claude_version_parsing(monkeypatch):
    from autocortes import doctor

    monkeypatch.setattr(doctor, "_run", lambda *_cmd: "2.1.322 (Claude Code)\n")
    assert doctor.claude_version("claude") == (2, 1, 322)
    assert doctor.claude_version("claude") >= doctor.MIN_CLAUDE_CODE
    monkeypatch.setattr(doctor, "_run", lambda *_cmd: "")
    assert doctor.claude_version("claude") is None


def test_listed_parses_ffmpeg_output():
    assert _listed(FFMPEG_FILTERS, "ass")
    assert not _listed(FFMPEG_FILTERS, "drawtext")
    assert _listed(FFMPEG_ENCODERS, "libx264")
    assert _listed(FFMPEG_ENCODERS, "h264_nvenc")
    assert not _listed(FFMPEG_ENCODERS, "libx265")
