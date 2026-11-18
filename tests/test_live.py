import base64
import json
import os
import subprocess
import time

import pytest

from autocortes.config import ClipConfig, DetectConfig, LLMConfig
from autocortes.detect import judge
from autocortes.detect.scorer import PeakDetector
from autocortes.detect.signals import ChatWindows, RollingZ, chat_weight
from autocortes.ingest import segments as seg
from autocortes.ingest.chat import TwitchChat, parse_line, twitch_channel
from autocortes.ingest.recorder import Recorder
from autocortes.live import channel_slug, clock, frame_times, slugify


@pytest.mark.parametrize(
    ("url", "name"),
    [
        ("https://www.twitch.tv/Gaules", "gaules"),
        ("twitch.tv/alanzoka/", "alanzoka"),
        ("https://www.youtube.com/@canal/live", None),
    ],
)
def test_twitch_channel(url, name):
    assert twitch_channel(url) == name


def test_parse_irc_lines():
    tagged = "@color=#FF0000 :fulano!fulano@fulano.tmi.twitch.tv PRIVMSG #gaules :KEKW kk"
    assert parse_line(tagged) == ("fulano", "KEKW kk")
    plain = ":ciclano!c@c.tmi.twitch.tv PRIVMSG #gaules :oi chat"
    assert parse_line(plain) == ("ciclano", "oi chat")
    assert parse_line("PING :tmi.twitch.tv") is None


@pytest.mark.parametrize(
    ("text", "weight"),
    [("KEKW", 2), ("kkkkkk", 2), ("CLIPA ISSO", 2), ("NAOOOO", 2), ("bom dia chat", 1), ("kk", 1)],
)
def test_chat_weight(text, weight):
    assert chat_weight(text) == weight


def test_chat_windows_cap_each_author():
    windows = ChatWindows(5)
    for _ in range(10):
        windows.add(1.0, "spammer", "KEKW")
    windows.add(2.0, "outro", "oi")
    windows.add(6.0, "outro", "oi")
    assert windows.value(0) == 2 * 2.0 + 1.0  # o spammer conta só 2 vezes
    assert windows.value(1) == 1.0
    windows.forget_before(1)
    assert windows.value(0) == 0.0


def test_rolling_z_flags_a_spike():
    z = RollingZ(size=50, min_history=10, floor=1.0)
    for value in (10, 11, 9, 10, 12, 10, 9, 11, 10, 10):
        assert z.push(value) is None  # histórico ainda curto
    assert z.push(10) == pytest.approx(0, abs=0.5)
    assert z.push(40) > 10


def detector(**overrides) -> PeakDetector:
    settings = dict(
        window_seconds=5,
        baseline_minutes=2,
        trigger_zscore=3.0,
        reaction_delay_seconds=8,
        min_gap_seconds=60,
        max_clips_per_hour=2,
    )
    return PeakDetector(DetectConfig(**(settings | overrides)))


def test_peak_detector_respects_gap_and_hourly_cap():
    peaks = []
    det = detector()
    for i in range(200):
        spike = i in (60, 62, 100, 150)  # 62 cai no intervalo mínimo; 150 passa do teto/hora
        chat = 50.0 if spike else 10.0 + i % 3
        audio = -10.0 if spike else -30.0 + i % 2
        if peak := det.update(i, chat, audio):
            peaks.append(peak)
    # o instante recua o atraso de reação do chat (8 s) a partir do meio da janela
    assert [p.t for p in peaks] == pytest.approx([60 * 5 + 2.5 - 8, 100 * 5 + 2.5 - 8])


def test_peak_without_chat_uses_audio_alone():
    det = detector()
    found = [det.update(i, None, -10.0 if i == 40 else -30.0 + i % 2) for i in range(60)]
    peaks = [p for p in found if p]
    assert len(peaks) == 1 and peaks[0].z_chat is None
    assert peaks[0].t == pytest.approx(40 * 5 + 2.5)  # sem chat, sem atraso de reação


def write_list(directory, rows):
    (directory / seg.LIST_NAME).write_text("\n".join(rows) + "\n", encoding="utf-8")


def test_read_segments_starts_at_zero_and_skips_partial_lines(tmp_path):
    write_list(
        tmp_path,
        ["seg_000000.ts,1.400000,11.400000", "seg_000001.ts,11.400000,21.433333", "seg_00"],
    )
    segments = seg.read_segments(tmp_path)
    assert [s.path.name for s in segments] == ["seg_000000.ts", "seg_000001.ts"]
    assert [(s.start, s.end) for s in segments] == [
        pytest.approx((0, 10)),
        pytest.approx((10, 20.033333)),
    ]


def test_covering_join_and_prune(tmp_path):
    rows = []
    for i in range(4):
        (tmp_path / f"seg_{i:06d}.ts").write_bytes(bytes([i]) * 3)
        rows.append(f"seg_{i:06d}.ts,{i * 10}.0,{(i + 1) * 10}.0")
    write_list(tmp_path, rows)
    segments = seg.read_segments(tmp_path)
    chosen = seg.covering(segments, 12, 25)
    assert [s.path.name for s in chosen] == ["seg_000001.ts", "seg_000002.ts"]
    assert seg.join(chosen, tmp_path / "trecho.ts") == 10.0
    assert (tmp_path / "trecho.ts").read_bytes() == b"\x01\x01\x01\x02\x02\x02"
    assert seg.prune(segments, before=20.5) == 2
    with pytest.raises(ValueError, match="apagado"):
        seg.join(seg.covering(seg.read_segments(tmp_path), 0, 5), tmp_path / "x.ts")


def test_recorder_and_chat_stop_and_join(tmp_path, monkeypatch):
    # Regressão: um atributo `_stop` nas threads escondia o método interno que o join() usa.
    monkeypatch.setattr(Recorder, "_record", lambda self: None)
    monkeypatch.setattr(TwitchChat, "_listen", lambda self: self._halt.wait())
    threads = [
        Recorder("twitch.tv/canal", tmp_path, "best", 10, on_session=lambda _session: None),
        TwitchChat("canal", on_message=lambda *_: None),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.stop()
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_chat_reports_connection_once(monkeypatch):
    statuses = []

    def fake_listen(self):
        self._mark_connected()
        self._mark_connected()  # segunda vez não repete o aviso
        self._halt.wait()

    monkeypatch.setattr(TwitchChat, "_listen", fake_listen)
    chat = TwitchChat("canal", on_message=lambda *_: None, on_status=statuses.append)
    chat.start()
    deadline = time.monotonic() + 5
    while not statuses and time.monotonic() < deadline:
        time.sleep(0.01)
    chat.stop()
    chat.join(timeout=5)
    assert statuses == ["chat conectado"] and chat.connected


def test_remove_old_sessions_keeps_recent_and_current(tmp_path):
    old, recent, current = (tmp_path / name for name in ("antiga", "recente", "atual"))
    for folder in (old, recent, current):
        folder.mkdir()
        (folder / "seg_000000.ts").write_bytes(b"x")
    three_hours_ago = time.time() - 3 * 3600
    os.utime(old, (three_hours_ago, three_hours_ago))
    os.utime(current, (three_hours_ago, three_hours_ago))
    assert seg.remove_old_sessions(tmp_path, max_age_hours=2, keep=current) == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ["atual", "recente"]


def claude_output(**answer) -> str:
    verdict = {
        "is_clip": True,
        "score": 8,
        "start": 1779.0,
        "end": 1840.2,
        "reason": "virada clara",
        "title": "Caiu 4 vezes no mesmo buraco",
        "headline": "NEM ELE *ACREDITOU*",
        "description": "E na quinta deu certo.",
        "hashtags": ["#GTA6", "Cortes"],
    } | answer
    return json.dumps(
        {
            "type": "result",
            "is_error": False,
            "duration_ms": 9000,
            "total_cost_usd": 0.04,
            "usage": {"input_tokens": 1200, "cache_read_input_tokens": 300, "output_tokens": 900},
            "modelUsage": {"claude-opus-5-5": {}},
            "result": "",
            "structured_output": verdict,
        }
    )


def claude_stream_output(**answer) -> str:
    """Saída em stream-json: um evento por linha, o veredito no último (type=result)."""
    result = json.loads(claude_output(**answer))
    events = [{"type": "system", "subtype": "init"}, {"type": "assistant", "message": {}}, result]
    return "\n".join(json.dumps(event) for event in events) + "\n"


def test_parse_response_reads_the_result_event_of_stream_json():
    verdict, usage = judge.parse_response(claude_stream_output(score=9))
    assert verdict.score == 9 and verdict.headline == "NEM ELE *ACREDITOU*"
    assert usage.cost_usd == 0.04


def test_parse_response_reads_structured_output_and_usage():
    verdict, usage = judge.parse_response(claude_output())
    assert verdict.title == "Caiu 4 vezes no mesmo buraco"
    assert verdict.hashtags == ["gta6", "cortes"]
    assert (usage.model, usage.input_tokens, usage.output_tokens) == ("claude-opus-5-5", 1500, 900)
    assert usage.cost_usd == 0.04 and usage.seconds == 9.0


def test_parse_response_falls_back_to_result_text():
    data = json.loads(claude_output())
    data["result"] = json.dumps(data.pop("structured_output"))
    verdict, _ = judge.parse_response(json.dumps(data))
    assert verdict.score == 8


def test_parse_response_errors():
    expired = {"is_error": True, "result": "Failed to authenticate: OAuth session expired"}
    with pytest.raises(judge.JudgeError, match="OAuth"):
        judge.parse_response(json.dumps(expired))
    with pytest.raises(judge.JudgeError, match="boom"):
        judge.parse_response("não é json", "boom")


def test_fit_verdict_keeps_window_and_duration():
    clip = ClipConfig(min_seconds=15, max_seconds=60)
    long, _ = judge.parse_response(claude_output(start=1700.0, end=1900.0))
    fitted = judge.fit_verdict(long, 1742.0, 1862.0, clip)
    assert (fitted.start, fitted.end) == (1742.0, 1802.0)
    short, _ = judge.parse_response(claude_output(start=1800.0, end=1805.0))
    fitted = judge.fit_verdict(short, 1742.0, 1862.0, clip)
    assert fitted.end - fitted.start == 15


def test_subscription_env_drops_api_keys(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-teste")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "x")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "")
    env = judge.subscription_env()
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        assert name not in env
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    assert judge.subscription_env()["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"


def test_subscription_env_removes_spaces_from_a_pasted_token(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-abc def\n")
    assert judge.subscription_env()["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-abcdef"


def test_ask_claude_sends_text_and_frames_as_stream_json(monkeypatch):
    calls = {}

    def fake_run(cmd, **kwargs):
        calls.update(cmd=cmd, **kwargs)
        return subprocess.CompletedProcess(cmd, 0, claude_stream_output(), "")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-teste")
    monkeypatch.setattr(judge.shutil, "which", lambda _name: "claude")
    monkeypatch.setattr(judge.subprocess, "run", fake_run)
    llm = LLMConfig(focus="GTA, lances de jogo")
    verdict, _ = judge.ask_claude("o trecho", llm, ClipConfig(), [b"quadro1"])
    cmd = calls["cmd"]
    assert verdict.is_clip and verdict.headline == "NEM ELE *ACREDITOU*"
    assert cmd[:2] == ["claude", "-p"] and cmd[cmd.index("--input-format") + 1] == "stream-json"
    assert cmd[cmd.index("--tools") + 1] == "" and "--strict-mcp-config" in cmd
    assert cmd[cmd.index("--model") + 1] == "claude-opus-5-5"
    assert "Foco destes canais: GTA, lances de jogo" in cmd[cmd.index("--system-prompt") + 1]
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == judge.SCHEMA
    content = json.loads(calls["input"])["message"]["content"]
    assert content[0] == {"type": "text", "text": "o trecho"}
    assert content[1]["type"] == "image"
    assert content[1]["source"]["data"] == base64.b64encode(b"quadro1").decode()
    assert "ANTHROPIC_API_KEY" not in calls["env"]


def test_build_prompt_has_transcript_chat_and_frames():
    lines, chat = [(1826.6, 1829.9, " CONSEGUI ")], [(1831.0, "fulano", "KEKW")]
    prompt = judge.build_prompt("gaules", 1832.0, 4.1, lines, chat)
    assert "t=1832" in prompt and "[1826.6-1829.9] CONSEGUI" in prompt
    assert "[1831] fulano: KEKW" in prompt and "Quadros" not in prompt
    with_frames = judge.build_prompt("gaules", 1832.0, 4.1, lines, chat, [1822.0, 1830.0])
    assert "Quadros anexados, em ordem, nos instantes t=1822, 1830 s." in with_frames


def test_frame_times_spread_around_the_peak():
    assert frame_times(100, 5, 40, 120) == [90, 94, 98, 102, 106]
    assert frame_times(5, 5, 0, 25) == [0, 2.75, 5.5, 8.25, 11.0]  # começo da gravação
    assert frame_times(100, 1, 40, 120) == [100]
    assert frame_times(100, 0, 40, 120) == []


def test_names_and_clock():
    assert channel_slug("https://www.twitch.tv/Gaules") == "gaules"
    assert channel_slug("youtube.com/@Casimiro/live") == "casimiro"
    assert channel_slug("https://www.youtube.com/watch?v=abc123") == "abc123"
    assert clock(75) == "1:15" and clock(3723) == "1:02:03"
    assert slugify("Caiu 4 vezes no MESMO buraco!") == "caiu-4-vezes-no-mesmo-buraco"
    assert slugify("Ação é ótima") == "acao-e-otima"


def test_short_dir_groups_by_day_and_channel(tmp_path):
    from autocortes.live import short_dir

    when = time.strptime("2026-11-19 21:40", "%Y-%m-%d %H:%M")
    first = short_dir(tmp_path, when, "felps", 8, True, "Ele VOOU de moto até o telhado!")
    assert first == tmp_path / "2026-11-19" / "felps" / "21h40_n8_ele-voou-de-moto-ate-o-telhado"
    first.mkdir(parents=True)
    again = short_dir(tmp_path, when, "felps", 8, True, "Ele VOOU de moto até o telhado!")
    assert again.name == "21h40_n8_ele-voou-de-moto-ate-o-telhado-2"
    assert short_dir(tmp_path, when, "felps", 3, False, "x").name == "21h40_n3_sem-juiz_x"


def test_sidecars_and_queue_listing(tmp_path):
    from types import SimpleNamespace

    from typer.testing import CliRunner

    from autocortes.cli import app
    from autocortes.detect.scorer import Peak
    from autocortes.live import Approved, Candidate, write_sidecars

    folder = tmp_path / "fila" / "2026-11-19" / "felps" / "21h40_n8_ele-voou"
    folder.mkdir(parents=True)
    video = folder / "ele-voou.mp4"
    video.write_bytes(b"")
    verdict = judge.Verdict(True, 8, 1.0, 30.0, "bom", "Ele voou", "desc", ["gta", "felps"])
    channel = SimpleNamespace(name="felps", url="twitch.tv/felps")
    peak = Peak(10.0, 10.0, None, None, chorus="kkk (9 pessoas)", clips=("VOOU",))
    write_sidecars(video, Approved(Candidate(channel, None, peak, []), verdict, [], True))
    info = json.loads((folder / "info.json").read_text(encoding="utf-8"))
    assert info["video"] == "ele-voou.mp4" and info["viewer_clips"] == ["VOOU"]
    assert (folder / "post.txt").read_text(encoding="utf-8") == "Ele voou\n\ndesc\n\n#gta #felps\n"

    config = tmp_path / "c.yaml"
    config.write_text(f"data_dir: {tmp_path.as_posix()}\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["-c", str(config), "queue"])
    assert result.exit_code == 0, result.output
    assert "Ele voou" in result.output and "21h40_n8_ele-voou" in result.output
