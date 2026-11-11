"""Renderiza de verdade com o ffmpeg (pula se ele não estiver instalado)."""

import subprocess
from pathlib import Path

import pytest

from autocortes.config import Config
from autocortes.ffmpeg import find_tool, probe, run_ffmpeg
from autocortes.render import facetrack
from autocortes.render.pipeline import render_short

pytestmark = pytest.mark.skipif(find_tool("ffmpeg") is None, reason="precisa do ffmpeg")


def make_video(path: Path, source: str, with_audio: bool) -> Path:
    args = ["-f", "lavfi", "-i", source]
    if with_audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-c:a", "aac", "-shortest"]
    run_ffmpeg([*args, "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)])
    return path


def frame_luma(video: Path, t: float) -> int:
    """Brilho médio (0-255) do quadro no instante t."""
    cmd = [
        str(find_tool("ffmpeg")), "-v", "error", "-ss", str(t), "-i", str(video),
        "-frames:v", "1", "-vf", "scale=1:1,format=gray", "-f", "rawvideo", "-",
    ]  # fmt: skip
    return subprocess.run(cmd, capture_output=True, check=True).stdout[0]


@pytest.fixture(scope="module")
def testsrc(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("src") / "testsrc.mp4"
    return make_video(path, "testsrc2=size=1280x720:rate=30:duration=3", with_audio=True)


@pytest.mark.parametrize("layout", ["blur", "center", "split"])
def test_layouts_render_a_vertical_video(testsrc, tmp_path, layout):
    result = render_short(
        testsrc,
        0.5,
        2.5,
        tmp_path / f"{layout}.mp4",
        Config(data_dir=tmp_path),
        layout=layout,
        facecam=(960, 0, 320, 180),
        with_captions=False,
    )
    info = probe(result.path)
    assert (info.width, info.height) == (1080, 1920)
    assert info.has_audio
    assert info.duration == pytest.approx(2.0, abs=0.15)


def test_face_layout_moves_the_crop_with_the_track(tmp_path, monkeypatch):
    # Metade esquerda preta, metade direita branca; o "rosto" troca de lado em t=1,5 s.
    source = make_video(
        tmp_path / "metades.mp4",
        "color=black:s=1280x720:r=30:d=3,drawbox=x=640:y=0:w=640:h=720:color=white:t=fill",
        with_audio=False,
    )
    samples = [(i / 4, 100.0 if i < 6 else 1180.0) for i in range(12)]
    monkeypatch.setattr(facetrack, "ensure_model", lambda _dir: Path("sem-modelo.onnx"))
    monkeypatch.setattr(facetrack, "detect_centers", lambda *_args: samples)
    result = render_short(
        source, 0.0, 3.0, tmp_path / "face.mp4", Config(data_dir=tmp_path), layout="face"
    )
    assert frame_luma(result.path, 0.5) < 60  # recorte na metade preta
    assert frame_luma(result.path, 2.5) > 200  # depois do corte seco, na metade branca
    assert "O vídeo não tem áudio: short sem legenda." in result.notes
