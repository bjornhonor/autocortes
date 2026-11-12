"""Modo live: acompanha várias lives ao mesmo tempo e entrega shorts numa fila de revisão.

Por canal: gravador (streamlink -> ffmpeg), chat (IRC da Twitch) e analisador (sinais -> picos).
Para todos os canais: um juiz (Whisper + Claude) e um renderizador, cada um fazendo um trabalho
por vez para não disputarem a CPU. Se o Claude não responder (sem login, limite da
assinatura), o pico vira short do mesmo jeito, só pelos sinais, marcado como "sem juiz".
"""

import dataclasses
import itertools
import json
import queue
import re
import shutil
import tempfile
import threading
import time
import unicodedata
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from autocortes.config import ClipConfig, Config
from autocortes.detect import judge
from autocortes.detect.scorer import Peak, PeakDetector
from autocortes.detect.signals import ChatWindows, audio_levels
from autocortes.ffmpeg import FFmpegError, run_ffmpeg
from autocortes.ingest import segments as seg
from autocortes.ingest.chat import TwitchChat, twitch_channel
from autocortes.ingest.recorder import Recorder
from autocortes.render import captions
from autocortes.render.pipeline import render_short

Log = Callable[[str, str, str], None]  # (canal, mensagem, estilo do rich)
QUEUE_DIR = "fila"
URL_NOISE = {"live", "watch", "c", "channel", "user", "videos"}
CHAT_BEFORE, CHAT_AFTER, CHAT_SAMPLE = 15.0, 30.0, 40  # chat mandado ao juiz em volta do pico
CLAUDE_PAUSE = 300  # s sem tentar o Claude depois de uma falha (sem login, limite...)
FRAME_SPAN = (-10.0, 6.0)  # quadros para o juiz: de 10 s antes a 6 s depois do pico
REPORT_EVERY = 300  # s entre resumos no terminal


def channel_slug(url: str) -> str:
    """Nome curto do canal para pastas e mensagens: twitch.tv/Gaules -> gaules."""
    parsed = urlparse(url if "://" in url else "https://" + url)
    parts = [p.lstrip("@") for p in parsed.path.split("/") if p and p not in URL_NOISE]
    fallback = parse_qs(parsed.query).get("v", [parsed.hostname or "live"])[0]
    name = parts[0] if parts else fallback
    return re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-") or "live"


def clock(seconds: float) -> str:
    """75 -> '1:15'; 3723 -> '1:02:03'."""
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _z(value: float | None) -> str:
    """z-score para o log: '-' quando o sinal não existe (ex.: canal sem chat)."""
    return "-" if value is None else f"{value:.1f}"


def frame_times(peak_t: float, count: int, lo: float, hi: float) -> list[float]:
    """Instantes dos quadros mandados ao juiz, espalhados em volta do pico.

    Vai mais para antes do pico do que para depois: o lance acontece antes da reação.
    """
    if count <= 0:
        return []
    first = max(lo, peak_t + FRAME_SPAN[0])
    last = min(hi - 0.5, peak_t + FRAME_SPAN[1])
    if count == 1 or last <= first:
        return [round(min(max(peak_t, lo), hi - 0.5), 2)]
    step = (last - first) / (count - 1)
    return [round(first + i * step, 2) for i in range(count)]


def slugify(text: str, size: int = 40) -> str:
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-")[:size].strip("-") or "corte"


@dataclass
class Candidate:
    channel: "Channel"
    session: seg.Session
    peak: Peak
    chat: list[tuple[float, str, str]]  # (t, autor, texto) em volta do pico


@dataclass
class Approved:
    candidate: Candidate
    verdict: judge.Verdict
    words: list[captions.Word]  # tempo em s da sessão: viram a legenda sem transcrever de novo
    judged: bool  # False: o juiz não estava disponível e o corte saiu só pelos sinais


def fallback_verdict(candidate: Candidate, clip: ClipConfig) -> judge.Verdict:
    """Sem juiz: corte fixo em volta do pico, com nota tirada da intensidade do pico."""
    peak = candidate.peak
    return judge.Verdict(
        is_clip=True,
        score=max(1, min(10, round(peak.score))),
        start=max(0.0, peak.t - clip.pre_roll_seconds),
        end=peak.t + clip.post_roll_seconds,
        reason="sem juiz: corte pelos sinais de chat e áudio",
        title=f"{candidate.channel.name} {clock(peak.t)}",
        description="",
        hashtags=[],
    )


class Channel:
    """Um canal acompanhado: gravação, chat e detecção de picos."""

    def __init__(self, url: str, cfg: Config, log: Log):
        self.url, self.cfg, self.log = url, cfg, log
        self.name = channel_slug(url)
        self.session: seg.Session | None = None
        self.messages: deque[tuple[float, str, str]] = deque(maxlen=20_000)
        self.chat_total = self.peaks_total = 0
        self._lock = threading.Lock()
        self._reset()
        self.recorder = Recorder(
            url,
            cfg.data_dir / "live" / self.name,
            cfg.capture.quality,
            cfg.capture.segment_seconds,
            on_session=self._on_session,
            on_status=lambda text: log(self.name, text, ""),
        )
        twitch = twitch_channel(url)
        self.chat = None
        if twitch:
            self.chat = TwitchChat(twitch, self._on_chat, lambda text: log(self.name, text, ""))

    def _reset(self) -> None:
        detect = self.cfg.detect
        self.windows = ChatWindows(detect.window_seconds)
        self.detector = PeakDetector(detect)
        self.levels: dict[int, tuple[float, int]] = {}
        self.next_window = 0
        self.seen = 0  # segmentos já medidos
        self.pending: list[Peak] = []
        self.messages.clear()

    def start(self) -> None:
        self.recorder.start()
        if self.chat:
            self.chat.start()
        else:
            self.log(self.name, "sem chat (por enquanto só Twitch): picos só pelo áudio", "yellow")

    def stop(self) -> None:
        self.recorder.stop()
        if self.chat:
            self.chat.stop()

    def _on_session(self, session: seg.Session) -> None:
        with self._lock:
            self.session = session
            self._reset()
        retention = self.cfg.capture.retention_hours
        seg.remove_old_sessions(session.dir.parent, retention, keep=session.dir)

    def _on_chat(self, wall: float, author: str, text: str) -> None:
        with self._lock:
            if self.session is None:
                return
            t = wall - self.session.t0
            self.windows.add(t, author, text)
            self.messages.append((t, author, text))
            self.chat_total += 1

    def poll(self) -> list[Candidate]:
        """Mede os segmentos novos, avança as janelas fechadas e devolve candidatos prontos."""
        with self._lock:
            session = self.session
            if session is None:
                return []
            detect = self.cfg.detect
            segments = seg.read_segments(session.dir)
            for segment in segments[self.seen :]:
                levels = audio_levels(segment.path, segment.start, detect.window_seconds)
                for index, (total, count) in levels.items():
                    old_total, old_count = self.levels.get(index, (0.0, 0))
                    self.levels[index] = (old_total + total, old_count + count)
            self.seen = len(segments)
            if not segments:
                return []
            recorded = segments[-1].end
            chat_on = self.chat is not None and self.chat.connected
            while (self.next_window + 1) * detect.window_seconds <= recorded:
                index = self.next_window
                total, count = self.levels.pop(index, (0.0, 0))
                peak = self.detector.update(
                    index,
                    self.windows.value(index) if chat_on else None,
                    total / count if count else None,
                )
                if peak:
                    self.pending.append(peak)
                    self.peaks_total += 1
                    signals = f"chat {_z(peak.z_chat)}, áudio {_z(peak.z_audio)}"
                    message = f"pico em {clock(peak.t)} (intensidade {peak.score:.1f}: {signals})"
                    self.log(self.name, message, "cyan")
                self.next_window += 1
            self.windows.forget_before(self.next_window)
            ready = [p for p in self.pending if recorded >= p.t + detect.context_after_seconds]
            self.pending = [p for p in self.pending if p not in ready]
            seg.prune(segments, recorded - self.cfg.capture.retention_hours * 3600)
            return [Candidate(self, session, p, self._chat_near(p.t)) for p in ready]

    def _chat_near(self, t: float) -> list[tuple[float, str, str]]:
        near = [m for m in self.messages if t - CHAT_BEFORE <= m[0] <= t + CHAT_AFTER]
        if len(near) > CHAT_SAMPLE:
            step = len(near) / CHAT_SAMPLE
            near = [near[int(i * step)] for i in range(CHAT_SAMPLE)]
        return near


class RenderWorker(threading.Thread):
    """Um short por vez: emenda os segmentos do trecho, renderiza e põe na fila de revisão."""

    def __init__(self, cfg: Config, log: Log):
        super().__init__(name="render", daemon=True)
        self.cfg, self.log = cfg, log
        self.inbox: queue.Queue[Approved] = queue.Queue()
        self.done = 0

    def run(self) -> None:
        while True:
            approved = self.inbox.get()
            name = approved.candidate.channel.name
            try:
                path = self.render(approved)
                self.done += 1
                tag = "" if approved.judged else " [sem juiz]"
                score = approved.verdict.score
                self.log(name, f"short pronto (nota {score}){tag}: {path.name}", "bold green")
            except Exception as exc:  # um corte com problema não pode derrubar a live
                self.log(name, f"render falhou: {exc}", "red")
            finally:
                self.inbox.task_done()

    def render(self, approved: Approved) -> Path:
        candidate, verdict = approved.candidate, approved.verdict
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name = f"{stamp}_{candidate.channel.name}_n{verdict.score}_{slugify(verdict.title)}"
        out = self.cfg.data_dir / QUEUE_DIR / f"{name}.mp4"
        work = Path(tempfile.mkdtemp(prefix="autocortes-render-"))
        try:
            segments = seg.read_segments(candidate.session.dir)
            source = work / "trecho.ts"
            offset = seg.join(seg.covering(segments, verdict.start, verdict.end), source)
            start = max(verdict.start, offset)
            words = [
                captions.Word(w.start - start, w.end - start, w.text)
                for w in approved.words
                if w.start >= start - 0.05 and w.end <= verdict.end + 0.05
            ]
            render_short(
                source,
                start - offset,
                verdict.end - offset,
                out,
                self.cfg,
                transcript=captions.Transcript(words, device="reuso"),
                headline=verdict.headline,
            )
        finally:
            shutil.rmtree(work, ignore_errors=True)
        out.with_suffix(".ass").unlink(missing_ok=True)  # na fila, só o que interessa
        write_sidecars(out, approved)
        return out


class JudgeWorker(threading.Thread):
    """Um candidato por vez, o pico mais forte primeiro: transcreve e pergunta ao Claude."""

    def __init__(self, cfg: Config, use_claude: bool, renderer: RenderWorker, log: Log):
        super().__init__(name="juiz", daemon=True)
        self.cfg, self.use_claude, self.renderer, self.log = cfg, use_claude, renderer, log
        self.inbox: queue.PriorityQueue = queue.PriorityQueue()
        self.usage_log = cfg.data_dir / QUEUE_DIR / "juiz.jsonl"
        self._order = itertools.count()
        self._claude_paused_until = 0.0

    def submit(self, candidate: Candidate) -> None:
        self.inbox.put((-candidate.peak.score, next(self._order), candidate))

    def run(self) -> None:
        while True:
            _, _, candidate = self.inbox.get()
            try:
                approved = self.evaluate(candidate)
                if approved:
                    self.renderer.inbox.put(approved)
            except Exception as exc:  # um candidato com problema não pode derrubar a live
                self.log(candidate.channel.name, f"juiz falhou: {exc}", "red")
            finally:
                self.inbox.task_done()

    def evaluate(self, candidate: Candidate) -> Approved | None:
        detect, clip = self.cfg.detect, self.cfg.clip
        peak, name = candidate.peak, candidate.channel.name
        lo = max(0.0, peak.t - detect.context_before_seconds)
        hi = peak.t + detect.context_after_seconds
        transcript, audio_start, frames, times = self._gather(candidate.session, lo, hi, peak.t)
        words = [
            dataclasses.replace(w, start=w.start + audio_start, end=w.end + audio_start)
            for w in transcript.words
        ]
        if not self.use_claude or time.monotonic() < self._claude_paused_until:
            return Approved(candidate, fallback_verdict(candidate, clip), words, judged=False)
        lines = [
            (audio_start + start, audio_start + end, text)
            for start, end, text in transcript.sentences
        ]
        prompt = judge.build_prompt(name, peak.t, peak.score, lines, candidate.chat, times)
        try:
            verdict, usage = judge.ask_claude(prompt, self.cfg.llm, clip, frames)
        except judge.JudgeError as exc:
            self._claude_paused_until = time.monotonic() + CLAUDE_PAUSE
            pause = CLAUDE_PAUSE // 60
            message = f"Claude indisponível ({exc}); cortes só pelos sinais por {pause} min"
            self.log(name, message, "yellow")
            return Approved(candidate, fallback_verdict(candidate, clip), words, judged=False)
        self._record(candidate, verdict, usage)
        if not verdict.is_clip or verdict.score < detect.min_judge_score:
            self.log(name, f"descartado (nota {verdict.score}): {verdict.reason}", "dim")
            return None
        verdict = judge.fit_verdict(verdict, lo, hi, clip)
        self.log(name, f"aprovado (nota {verdict.score}): {verdict.title}", "green")
        return Approved(candidate, verdict, words, judged=True)

    def _gather(
        self, session: seg.Session, lo: float, hi: float, peak_t: float
    ) -> tuple[captions.Transcript, float, list[bytes], list[float]]:
        """Transcreve [lo, hi] da gravação e tira quadros em volta do pico.

        Devolve a transcrição, onde o áudio começa, os quadros (JPEG) e os instantes deles.
        """
        work = Path(tempfile.mkdtemp(prefix="autocortes-juiz-"))
        try:
            source = work / "trecho.ts"
            offset = seg.join(seg.covering(seg.read_segments(session.dir), lo, hi), source)
            start = max(lo, offset)
            wav = work / "audio.wav"
            pcm = ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le"]
            window = ["-ss", f"{start - offset:.3f}", "-t", f"{hi - start:.3f}"]
            run_ffmpeg([*window, "-i", str(source), *pcm, str(wav)])
            transcript = captions.transcribe(wav, self.cfg.captions, self.cfg.language)
            frames: list[bytes] = []
            times: list[float] = []
            for t in frame_times(peak_t, self.cfg.llm.frames, start, hi):
                jpg = work / f"quadro_{len(frames)}.jpg"
                grab = ["-ss", f"{t - offset:.3f}", "-i", str(source), "-frames:v", "1"]
                try:
                    run_ffmpeg([*grab, "-vf", "scale=640:-2", "-q:v", "5", str(jpg)])
                except FFmpegError:
                    continue  # um quadro a menos não impede o julgamento
                if jpg.exists():
                    frames.append(jpg.read_bytes())
                    times.append(t)
            return transcript, start, frames, times
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _record(self, candidate: Candidate, verdict: judge.Verdict, usage: judge.Usage) -> None:
        """Guarda cada veredito e o consumo em data/fila/juiz.jsonl."""
        entry = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "channel": candidate.channel.name,
            "peak_t": round(candidate.peak.t, 1),
            "verdict": dataclasses.asdict(verdict),
            "usage": dataclasses.asdict(usage),
        }
        self.usage_log.parent.mkdir(parents=True, exist_ok=True)
        with self.usage_log.open("a", encoding="utf-8") as log:
            log.write(json.dumps(entry, ensure_ascii=False) + "\n")


def write_sidecars(video: Path, approved: Approved) -> None:
    """Ao lado do .mp4: .json com tudo e .txt com o texto pronto para colar no post."""
    candidate, verdict = approved.candidate, approved.verdict
    data = {
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "channel": candidate.channel.name,
        "url": candidate.channel.url,
        "judged": approved.judged,
        "peak_t": round(candidate.peak.t, 1),
        "peak_score": round(candidate.peak.score, 2),
        **dataclasses.asdict(verdict),
        "video": video.name,
    }
    text = json.dumps(data, ensure_ascii=False, indent=2)
    video.with_suffix(".json").write_text(text, encoding="utf-8")
    tags = " ".join(f"#{tag}" for tag in verdict.hashtags)
    post = "\n\n".join(part for part in (verdict.title, verdict.description, tags) if part)
    video.with_suffix(".txt").write_text(post + "\n", encoding="utf-8")


def watch(
    urls: list[str], cfg: Config, *, use_claude: bool, minutes: float | None, log: Log
) -> None:
    """Acompanha as lives até Ctrl+C (ou até `minutes`); os shorts saem em data/fila."""
    renderer = RenderWorker(cfg, log)
    judge_worker = JudgeWorker(cfg, use_claude, renderer, log)
    channels = [Channel(url, cfg, log) for url in dict.fromkeys(urls)]
    renderer.start()
    judge_worker.start()
    for channel in channels:
        channel.start()
    deadline = time.monotonic() + minutes * 60 if minutes else None
    next_report = time.monotonic() + REPORT_EVERY
    try:
        while deadline is None or time.monotonic() < deadline:
            for channel in channels:
                try:
                    for candidate in channel.poll():
                        judge_worker.submit(candidate)
                except Exception as exc:  # um canal com problema não para os outros
                    log(channel.name, f"erro na análise: {exc}", "red")
            if time.monotonic() >= next_report:
                next_report += REPORT_EVERY
                for channel in channels:
                    stats = f"chat: {channel.chat_total} msgs | picos: {channel.peaks_total}"
                    log(channel.name, stats, "dim")
                pending = f"julgar: {judge_worker.inbox.qsize()} | render: {renderer.inbox.qsize()}"
                log("fila", f"{pending} | prontos: {renderer.done}", "dim")
            time.sleep(2)
    finally:
        for channel in channels:
            channel.stop()
        for channel in channels:  # cada gravador fecha o último segmento e arruma a pasta
            channel.recorder.join(timeout=20)
    if deadline is not None:  # tempo marcado (testes): termina o que já entrou na fila
        limit = time.monotonic() + 600
        while time.monotonic() < limit and (
            judge_worker.inbox.unfinished_tasks or renderer.inbox.unfinished_tasks
        ):
            time.sleep(1)
