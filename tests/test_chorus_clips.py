"""Chat em coro e clipes dos viewers como sinais de pico."""

import io
import json
import urllib.error
from pathlib import Path

import pytest

from autocortes.config import Config, DetectConfig
from autocortes.detect import judge
from autocortes.detect.scorer import Peak, PeakDetector
from autocortes.detect.signals import ChatWindows, chorus_tokens
from autocortes.ingest import segments as seg
from autocortes.ingest.clips import Clip, ClipWatcher, TwitchApp, TwitchAuthError, rfc3339
from autocortes.live import Channel, clip_moment, merge_peaks

# --- coro ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("KKKKKKKKKK", {"kkk"}),
        ("kekw", {"kkk"}),
        ("hahahaha", {"kkk"}),
        ("NAAAAAAO ACREDITO", {"naao", "acredito", "naao acredito"}),
        ("que isso mano", {"que isso mano"}),  # só stopwords: vale a frase inteira
        ("1111", set()),
    ],
)
def test_chorus_tokens_normalize_laughs_and_repeats(text, expected):
    assert chorus_tokens(text) == expected


def test_chorus_counts_distinct_authors_saying_the_same_thing():
    windows = ChatWindows(5)
    for i in range(12):
        windows.add(1.0, f"viewer{i}", "K" * (3 + i))  # cada um ri de um jeito
    for _ in range(30):
        windows.add(2.0, "spammer", "NAAAO")  # um só repetindo não é coro
    windows.add(3.0, "outro", "boa noite chat")
    windows.add(6.0, "a", "KKKK")
    windows.add(7.0, "b", "kkkkkk")  # duas pessoas ainda não são coro
    assert windows.chorus(0) == (12, "kkk")
    assert windows.chorus(1) == (0, "")
    windows.forget_before(1)
    assert windows.chorus(0) == (0, "")


def test_chorus_triggers_a_peak_even_when_chat_volume_is_flat():
    cfg = DetectConfig(
        window_seconds=5,
        baseline_minutes=2,
        trigger_zscore=2.0,
        reaction_delay_seconds=8,
        min_gap_seconds=60,
        max_clips_per_hour=5,
    )
    det = PeakDetector(cfg)
    peaks = []
    for i in range(80):
        chorus = (25, "naao") if i == 50 else (1 + i % 2, "oi")
        if peak := det.update(i, 20.0 + i % 3, -30.0 + i % 2, chorus):
            peaks.append(peak)
    assert len(peaks) == 1
    assert peaks[0].z_chorus > 5 and peaks[0].chorus == "naao (25 pessoas)"


# --- clipes: API -----------------------------------------------------------------------------


class FakeTwitch:
    """Faz o papel do urlopen: responde token, users e clips com o que o teste mandar."""

    def __init__(self, pages=(), token_status=200, expire_first_token=False):
        self.pages = list(pages)
        self.token_status = token_status
        self.expire_first_token = expire_first_token
        self.requests = []
        self.tokens = 0

    def __call__(self, request):
        self.requests.append(request)
        url = request.full_url
        if url.startswith("https://id.twitch.tv"):
            if self.token_status != 200:
                raise urllib.error.HTTPError(url, self.token_status, "x", {}, io.BytesIO())
            self.tokens += 1
            return json.dumps({"access_token": f"t{self.tokens}", "expires_in": 3600}).encode()
        if self.expire_first_token and request.get_header("Authorization") == "Bearer t1":
            raise urllib.error.HTTPError(url, 401, "expirado", {}, io.BytesIO())
        if "/users?" in url:
            return json.dumps({"data": [{"id": "123", "login": "canal"}]}).encode()
        return json.dumps(self.pages.pop(0)).encode()


def clip_json(clip_id, created, title="lance", duration=30.0):
    return {
        "id": clip_id,
        "created_at": rfc3339(created),
        "duration": duration,
        "title": title,
        "view_count": 3,
        "url": f"https://clips.twitch.tv/{clip_id}",
    }


def test_twitch_app_lists_clips_across_pages_since_the_session_start():
    since = 1_800_000_000.0
    pages = [
        {"data": [clip_json("a", since + 120)], "pagination": {"cursor": "c1"}},
        {"data": [clip_json("b", since - 30), clip_json("c", since + 300)], "pagination": {}},
    ]
    fake = FakeTwitch(pages)
    app = TwitchApp(" id \n", "segredo ", opener=fake)
    clips = app.clips("123", since)
    assert [c.id for c in clips] == ["a", "c"]  # "b" é de antes (a API arredonda o minuto)
    assert clips[0].created == pytest.approx(since + 120)
    token_request, first_page, second_page = fake.requests
    assert b"client_id=id&client_secret=segredo&grant_type=client_credentials" in (
        token_request.data
    )
    assert first_page.get_header("Client-id") == "id"
    assert "broadcaster_id=123" in first_page.full_url and "started_at=" in first_page.full_url
    assert "after=c1" in second_page.full_url


def test_twitch_app_renews_an_expired_token_once():
    fake = FakeTwitch([{"data": [], "pagination": {}}], expire_first_token=True)
    assert TwitchApp("id", "s", opener=fake).clips("123", 0.0) == []
    assert fake.tokens == 2


def test_twitch_app_rejects_bad_credentials():
    with pytest.raises(TwitchAuthError):
        TwitchApp("id", "errado", opener=FakeTwitch(token_status=400)).authorize()


def test_clip_watcher_reports_only_new_clips():
    since = 1_800_000_000.0
    first = {"data": [clip_json("a", since + 10)], "pagination": {}}
    second = {"data": [clip_json("a", since + 10), clip_json("b", since + 70)], "pagination": {}}
    got = []
    watcher = ClipWatcher(
        TwitchApp("id", "s", opener=FakeTwitch([first, second])),
        "canal",
        since=lambda: since,
        on_clips=got.append,
        on_status=lambda text: None,
    )
    watcher.check("123")
    watcher.check("123")
    assert [[c.id for c in batch] for batch in got] == [["a"], ["b"]]


# --- clipes: picos ---------------------------------------------------------------------------


def make_clip(clip_id: str, created: float, title: str = "lance") -> Clip:
    return Clip(clip_id, created, 30.0, title, 1, "")


def test_clip_moment_backs_off_the_reaction_delay():
    assert clip_moment(make_clip("a", 1000.0), t0=400.0, reaction_delay=8) == 592.0


def test_merge_peaks_takes_the_clip_instant_and_joins_what_each_knows():
    signal = Peak(100.0, 3.5, 4.0, 2.0, z_chorus=6.0, chorus="kkk (20 pessoas)")
    clip = Peak(90.0, 10.0, None, None, clips=("ELE VOOU",))
    merged = merge_peaks(signal, clip)
    assert merged.t == 90.0 and merged.score == 10.0
    assert merged.z_chat == 4.0 and merged.chorus == "kkk (20 pessoas)"
    assert merged.clips == ("ELE VOOU",)


@pytest.fixture
def channel(tmp_path):
    cfg = Config(data_dir=tmp_path)
    log = []
    app = TwitchApp("id", "s", opener=FakeTwitch())
    chan = Channel("twitch.tv/canal", cfg, lambda *a: log.append(a), app)
    chan.session = seg.Session(Path(tmp_path), t0=1000.0)
    return chan


def test_viewer_clips_become_one_peak_per_moment(channel):
    channel._on_clips([make_clip("a", 1300.0, "ELE VOOU"), make_clip("b", 1320.0, "voou kkk")])
    channel._on_clips([make_clip("c", 1700.0, "outro lance"), make_clip("x", 900.0)])
    assert channel.clips_total == 3  # o "x" é de antes da gravação
    peaks = sorted(channel.pending, key=lambda p: p.t)
    assert [p.t for p in peaks] == [292.0, 692.0]
    assert peaks[0].clips == ("ELE VOOU", "voou kkk") and peaks[0].score == 11.0


def test_clips_min_count_waits_for_more_viewers(channel):
    channel.cfg.detect.clips_min_count = 2
    channel._on_clips([make_clip("a", 1300.0)])
    assert channel.pending == []
    channel._on_clips([make_clip("b", 1310.0)])
    assert len(channel.pending) == 1 and len(channel.pending[0].clips) == 2


def test_a_clip_of_a_moment_already_judged_is_not_judged_again(channel):
    channel.sent.append(290.0)
    channel._on_clips([make_clip("a", 1300.0)])
    assert channel.pending == []


def test_signal_peak_merges_into_a_pending_clip_peak(channel):
    channel._on_clips([make_clip("a", 1300.0, "ELE VOOU")])
    added = channel._add_peak(Peak(300.0, 4.0, 5.0, 1.0, z_chorus=7.0, chorus="naao (9 pessoas)"))
    assert not added and len(channel.pending) == 1
    assert channel.pending[0].t == 292.0 and channel.pending[0].chorus == "naao (9 pessoas)"


def test_build_prompt_tells_the_judge_about_clips_and_chorus():
    prompt = judge.build_prompt(
        "canal", 292.0, 10.0, [], [], chorus="naao (9 pessoas)", clips=("ELE VOOU", "voou kkk")
    )
    assert 'Viewers criaram 2 clipe(s) deste trecho na Twitch: "ELE VOOU"; "voou kkk".' in prompt
    assert "O chat repetiu em coro: naao (9 pessoas)." in prompt
    assert "Viewers" not in judge.build_prompt("canal", 292.0, 3.0, [], [])
