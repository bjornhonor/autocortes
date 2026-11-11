import hashlib
from pathlib import Path

import pytest

from autocortes.config import CaptionsConfig
from autocortes.ffmpeg import VideoInfo
from autocortes.render import captions, facetrack, reframe
from autocortes.render.captions import Word
from autocortes.timecode import parse_time, time_tag

HD = VideoInfo(width=1920, height=1080, fps=30.0, duration=60.0, has_audio=True)
CROP_W = 608  # recorte 9:16 de um quadro 1920x1080


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("45", 45), ("1:15", 75), ("1:15,5", 75.5), ("01:02:03.5", 3723.5), ("75:00", 4500)],
)
def test_parse_time(text, seconds):
    assert parse_time(text) == pytest.approx(seconds)


@pytest.mark.parametrize("text", ["", "abc", "1:2:3:4", "1:75", "-5", "nan"])
def test_parse_time_rejects(text):
    with pytest.raises(ValueError):
        parse_time(text)


def test_time_tag():
    assert time_tag(75.4) == "1m15s"
    assert time_tag(3723) == "1h02m03s"


def make_words(*items: tuple[str, float, float]) -> list[Word]:
    return [Word(start, end, text) for text, start, end in items]


def test_group_words_breaks_on_size_pause_and_sentence_end():
    words = make_words(
        ("um", 0.0, 0.2), ("dois", 0.3, 0.5), ("três", 0.6, 0.8), ("quatro", 0.9, 1.0),
        ("cinco.", 1.1, 1.3), ("seis", 1.4, 1.6), ("sete", 3.0, 3.2),
    )  # fmt: skip
    groups = captions.group_words(words, 3)
    assert [[w.text for w in g] for g in groups] == [
        ["um", "dois", "três"],
        ["quatro", "cinco."],
        ["seis"],
        ["sete"],
    ]


def test_build_ass_highlights_one_word_per_event():
    words = make_words(
        ("fala", 0.0, 0.4), ("galera", 0.5, 1.0), ("tudo", 1.1, 1.4), ("bem?", 1.5, 1.9)
    )
    ass = captions.build_ass(words, CaptionsConfig(), 1080, 1920)
    events = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
    assert "PlayResY: 1920" in ass
    assert "Style: Short,Montserrat ExtraBold,88," in ass
    assert len(events) == 4
    assert events[0].startswith("Dialogue: 0,0:00:00.00,0:00:00.50,Short,")
    assert "{\\c&H00E1FF&" in events[0]
    assert events[0].endswith("}FALA{\\r} GALERA TUDO")
    assert events[2].startswith("Dialogue: 0,0:00:01.10,0:00:01.50,")  # cola no bloco seguinte
    assert events[3].startswith("Dialogue: 0,0:00:01.50,0:00:02.20,")  # último fica 0,3 s a mais


def write_wav(path: Path, samples: list[int], channels: int = 1, rate: int = 16000) -> Path:
    import struct
    import wave

    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return path


def test_read_wav_gives_float_samples(tmp_path):
    audio = captions.read_wav(write_wav(tmp_path / "a.wav", [0, 16384, -32768]))
    assert audio.dtype.name == "float32"
    assert audio.tolist() == [0.0, 0.5, -1.0]


def test_read_wav_rejects_other_formats(tmp_path):
    with pytest.raises(ValueError, match="16 kHz"):
        captions.read_wav(write_wav(tmp_path / "b.wav", [0, 0], channels=2))


def test_ass_helpers():
    assert captions.ass_time(3723.456) == "1:02:03.46"
    assert captions.ass_color("#FFE100") == "&H00E1FF&"
    assert captions._clean("{oi}\\N", uppercase=True) == "(OI)N"
    with pytest.raises(ValueError):
        captions.ass_color("amarelo")


def test_fit_crop():
    assert reframe.fit_crop(1920, 1080, 9 / 16) == (608, 1080)
    assert reframe.fit_crop(1080, 1920, 9 / 16) == (1080, 1920)
    assert reframe.fit_crop(1080, 1080, 9 / 16) == (608, 1080)


def test_center_crops_the_middle():
    assert reframe.center(HD, 1080, 1920) == "[src]crop=608:1080:656:0,scale=1080:1920[framed]"


def test_split_stacks_facecam_over_gameplay():
    chain = reframe.split(HD, 1080, 1920, (1500, 40, 384, 216))
    assert "[cam]crop=384:216:1500:40," in chain
    assert "crop=1080:608[top]" in chain
    assert "scale=1080:1312[bottom]" in chain
    assert chain.endswith("vstack=inputs=2[framed]")


def test_split_rejects_facecam_outside_the_frame():
    with pytest.raises(ValueError):
        reframe.split(HD, 1080, 1920, (1800, 0, 384, 216))


def samples_from(centers: list[float | None]) -> list[facetrack.Sample]:
    return [(i / facetrack.SAMPLE_FPS, c) for i, c in enumerate(centers)]


def test_track_without_faces_stays_centered():
    assert facetrack.plan_track(samples_from([None] * 8), 1920, CROP_W, 30) == [(0.0, 656)]


def test_track_ignores_small_jitter():
    jitter = [960.0 + d for d in (0, 15, -20, 10, -5, 25, -15, 0)]
    track = facetrack.plan_track(samples_from(jitter), 1920, CROP_W, 30)
    assert len(track) == 1


def test_track_pans_smoothly_toward_a_moving_face():
    centers: list[float | None] = [700.0 + 40 * i for i in range(24)]
    track = facetrack.plan_track(samples_from(centers), 1920, CROP_W, 30)
    xs = [x for _, x in track]
    assert xs == sorted(xs)  # anda num sentido só
    assert max(b - a for a, b in zip(xs, xs[1:], strict=False)) <= 40  # sem saltos
    assert xs[-1] > xs[0] + 500


def test_track_cuts_on_lasting_jump_and_ignores_one_off_spike():
    centers: list[float | None] = [400.0] * 8 + [1500.0] * 8
    centers[3] = 1500.0  # detecção errada isolada
    track = facetrack.plan_track(samples_from(centers), 1920, CROP_W, 30)
    assert track == [(0.0, 96), (2.0, 1196)]


def test_track_fills_gaps_with_known_position():
    track = facetrack.plan_track(samples_from([None, None, 400.0, None, 400.0]), 1920, CROP_W, 30)
    assert track == [(0.0, 96)]


def test_pick_face_prefers_largest_then_continuity():
    faces = [[100, 0, 50, 50], [500, 0, 60, 60]]  # x, y, w, h
    assert facetrack.pick_face(faces, None) == 530  # maior
    assert facetrack.pick_face(faces, 130) == 125  # tamanho parecido: fica no mesmo rosto
    assert facetrack.pick_face(None, 130) is None


def test_sendcmd_script():
    script = facetrack.sendcmd_script([(0.0, 96), (2.0, 1196)])
    assert script == "0.000 crop@face x 96;\n2.000 crop@face x 1196;\n"


def test_ensure_model_downloads_once_and_checks_the_hash(tmp_path, monkeypatch):
    payload = b"modelo de mentira"
    downloads = []

    def fake_download(url, dest):
        downloads.append(url)
        Path(dest).write_bytes(payload)

    monkeypatch.setattr(facetrack.urllib.request, "urlretrieve", fake_download)
    monkeypatch.setattr(facetrack, "MODEL_SHA256", hashlib.sha256(payload).hexdigest())
    assert facetrack.ensure_model(tmp_path).read_bytes() == payload
    facetrack.ensure_model(tmp_path)  # já está baixado: não baixa de novo
    assert downloads == [facetrack.MODEL_URL]


def test_ensure_model_rejects_a_corrupted_download(tmp_path, monkeypatch):
    def fake_download(_url, dest):
        Path(dest).write_bytes(b"corrompido")

    monkeypatch.setattr(facetrack.urllib.request, "urlretrieve", fake_download)
    with pytest.raises(RuntimeError, match="checksum"):
        facetrack.ensure_model(tmp_path)
    assert list(tmp_path.iterdir()) == []  # não deixa arquivo pela metade
