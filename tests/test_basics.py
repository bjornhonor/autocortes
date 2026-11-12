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
    assert config.render.layout == "gta" and "GTA" in config.llm.focus


def test_ensaio_inherits_config_and_overrides_detection():
    ensaio = load_config(ROOT / "ensaio.yaml")
    base = load_config(ROOT / "config.yaml")
    assert ensaio.detect.trigger_zscore == 2.5 != base.detect.trigger_zscore
    assert ensaio.llm.focus == base.llm.focus and ensaio.render.layout == "gta"
    assert ensaio.detect.min_judge_score == base.detect.min_judge_score


def test_config_base_merges_nested_sections(tmp_path):
    (tmp_path / "a.yaml").write_text("detect:\n  window_seconds: 7\n  trigger_zscore: 4\n")
    (tmp_path / "b.yaml").write_text("base: a.yaml\ndetect:\n  trigger_zscore: 2\n")
    config = load_config(tmp_path / "b.yaml")
    assert (config.detect.window_seconds, config.detect.trigger_zscore) == (7, 2)


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
