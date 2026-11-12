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
from autocortes.live import channel_slug, clock, slugify


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


def test_ask_claude_makes_a_lean_call(monkeypatch):
    calls = {}

    def fake_run(cmd, **kwargs):
        calls.update(cmd=cmd, **kwargs)
        return subprocess.CompletedProcess(cmd, 0, claude_output(), "")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-teste")
    monkeypatch.setattr(judge.shutil, "which", lambda _name: "claude")
    monkeypatch.setattr(judge.subprocess, "run", fake_run)
    verdict, _ = judge.ask_claude("o trecho", LLMConfig(), ClipConfig())
    cmd = calls["cmd"]
    assert verdict.is_clip and cmd[:2] == ["claude", "-p"]
    assert cmd[cmd.index("--tools") + 1] == "" and "--strict-mcp-config" in cmd
    assert cmd[cmd.index("--model") + 1] == "claude-opus-5-5"
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == judge.SCHEMA
    assert calls["input"] == "o trecho" and "ANTHROPIC_API_KEY" not in calls["env"]


def test_build_prompt_has_transcript_and_chat():
    lines, chat = [(1826.6, " CONSEGUI ")], [(1831.0, "fulano", "KEKW")]
    prompt = judge.build_prompt("gaules", 1832.0, 4.1, lines, chat)
    assert "t=1832" in prompt and "[1826.6] CONSEGUI" in prompt and "[1831] fulano: KEKW" in prompt


def test_names_and_clock():
    assert channel_slug("https://www.twitch.tv/Gaules") == "gaules"
    assert channel_slug("youtube.com/@Casimiro/live") == "casimiro"
    assert channel_slug("https://www.youtube.com/watch?v=abc123") == "abc123"
    assert clock(75) == "1:15" and clock(3723) == "1:02:03"
    assert slugify("Caiu 4 vezes no MESMO buraco!") == "caiu-4-vezes-no-mesmo-buraco"
    assert slugify("Ação é ótima") == "acao-e-otima"
