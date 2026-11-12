"""Segmenta um vídeo como o gravador faz e renderiza um corte que atravessa segmentos."""

import time

import pytest

from autocortes.config import Config
from autocortes.detect.signals import audio_levels
from autocortes.ffmpeg import find_tool, probe, run_ffmpeg
from autocortes.ingest import segments as seg
from autocortes.live import Channel
from autocortes.render.pipeline import render_short

pytestmark = pytest.mark.skipif(find_tool("ffmpeg") is None, reason="precisa do ffmpeg")


@pytest.fixture
def session(tmp_path):
    folder = tmp_path / "sessao"
    folder.mkdir()
    # 9 s com áudio em pedaços de 2 s, mesmos parâmetros de segmento do gravador
    run_ffmpeg(
        [
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=9",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=9",
            "-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-f", "segment", "-segment_time", "2",
            "-segment_list", seg.LIST_NAME, "-segment_list_type", "csv", seg.PATTERN,
        ],  # fmt: skip
        cwd=folder,
    )
    return folder


def test_segments_join_and_render_across_boundaries(session, tmp_path):
    segments = seg.read_segments(session)
    assert len(segments) >= 4 and segments[0].start == 0
    source = tmp_path / "trecho.ts"
    offset = seg.join(seg.covering(segments, 3.0, 7.0), source)
    result = render_short(
        source,
        3.0 - offset,
        7.0 - offset,
        tmp_path / "corte.mp4",
        Config(data_dir=tmp_path),
        layout="center",
        with_captions=False,
    )
    info = probe(result.path)
    assert (info.width, info.height) == (1080, 1920)
    assert info.duration == pytest.approx(4.0, abs=0.2)


def test_channel_poll_measures_segments_and_places_chat(session, tmp_path):
    cfg = Config(data_dir=tmp_path)
    channel = Channel("twitch.tv/canal_teste", cfg, log=lambda *_: None)
    t0 = time.time()
    channel._on_session(seg.Session(session, t0))
    channel._on_chat(t0 + 3.0, "fulano", "KEKW")
    assert channel.poll() == []  # histórico ainda curto: nenhum pico
    segments = seg.read_segments(session)
    assert channel.seen == len(segments)
    assert channel.next_window == int(segments[-1].end // cfg.detect.window_seconds)
    t, author, text = channel.messages[0]
    assert (author, text) == ("fulano", "KEKW") and t == pytest.approx(3.0, abs=0.01)


def test_audio_levels_of_a_segment(session):
    first = seg.read_segments(session)[0]
    levels = audio_levels(first.path, first.start, window=5)
    total, seconds = levels[0]
    assert seconds >= 1
    assert -30 < total / seconds < -12  # seno do lavfi: ~ -21 dBFS
