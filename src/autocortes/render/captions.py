"""Legendas estilo shorts: transcrição palavra a palavra (faster-whisper) e arquivo .ass."""

import functools
import os
import re
import wave
from dataclasses import dataclass, field
from pathlib import Path

from autocortes.config import CaptionsConfig

SENTENCE_END = (".", "!", "?", "…")
MAX_PAUSE = 0.6  # segundos de silêncio que encerram um bloco de palavras
LINGER = 0.3  # o último bloco fica na tela um pouco depois da fala

STYLE_FIELDS = (
    "Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
    "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, "
    "Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"
)
EVENT_FIELDS = "Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"


@dataclass(frozen=True)
class Word:
    start: float  # segundos desde o início do trecho
    end: float
    text: str


@dataclass(frozen=True)
class Transcript:
    words: list[Word]
    device: str  # onde o Whisper rodou: "cuda", "cpu" ou "reuso" (veio pronto do juiz)
    gpu_error: str | None = None  # por que a GPU foi descartada, quando se tentou usá-la
    sentences: list[tuple[float, float, str]] = field(default_factory=list)  # início, fim, texto


_gpu_error: str | None = None  # a GPU já falhou nesta execução: não tenta de novo a cada trecho


@functools.cache
def _model(name: str, device: str, compute_type: str):
    """Carrega o Whisper uma vez por processo (leva alguns segundos)."""
    from faster_whisper import WhisperModel

    return WhisperModel(name, device=device, compute_type=compute_type)


def transcribe(audio: Path, cfg: CaptionsConfig, language: str) -> Transcript:
    """Transcreve um WAV mono 16 kHz com timestamps por palavra.

    Com device=auto tenta a GPU primeiro; se faltarem as DLLs do CUDA (cuBLAS/cuDNN),
    cai para a CPU e não tenta mais a GPU nesta execução.
    """
    global _gpu_error
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    import ctranslate2

    samples = read_wav(audio)
    if cfg.device == "auto":
        gpu = _gpu_error is None and ctranslate2.get_cuda_device_count() > 0
        devices = ["cuda", "cpu"] if gpu else ["cpu"]
    else:
        devices = [cfg.device]

    for device in devices:
        try:
            model = _model(cfg.whisper_model, device, cfg.compute_type)
            segments, _ = model.transcribe(
                samples, language=language, word_timestamps=True, vad_filter=True
            )
            words, sentences = [], []
            for segment in segments:
                sentences.append((segment.start, segment.end, segment.text.strip()))
                words += [Word(w.start, w.end, w.word.strip()) for w in segment.words or []]
            words = [w for w in words if w.text]
            return Transcript(words, device, _gpu_error, sentences)
        except (RuntimeError, OSError) as exc:
            if device != "cuda" or device == devices[-1]:
                raise
            _gpu_error = (str(exc).splitlines() or [type(exc).__name__])[0]
    raise AssertionError("nenhum dispositivo disponível para o Whisper")


def read_wav(path: Path):
    """WAV PCM 16 bits mono 16 kHz -> array float32 em [-1, 1], como o Whisper espera.

    O áudio chega já decodificado pelo nosso ffmpeg: o caminho do próprio faster-whisper
    (PyAV) quebra com versões novas do PyAV (`metadata_errors` foi removido).
    """
    import numpy as np

    with wave.open(str(path), "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 16000):
            raise ValueError(f"{path.name}: esperava WAV mono, 16 bits, 16 kHz")
        frames = wav.readframes(wav.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


def group_words(words: list[Word], per_line: int) -> list[list[Word]]:
    """Blocos de até `per_line` palavras; uma pausa longa ou um fim de frase começam outro."""
    groups: list[list[Word]] = []
    for word in words:
        current = groups[-1] if groups else None
        if (
            current is None
            or len(current) >= per_line
            or word.start - current[-1].end > MAX_PAUSE
            or current[-1].text.endswith(SENTENCE_END)
        ):
            groups.append([word])
        else:
            current.append(word)
    return groups


def build_ass(
    words: list[Word],
    cfg: CaptionsConfig,
    width: int,
    height: int,
    headline: str = "",
    duration: float = 0.0,
) -> str:
    """Legenda .ass: blocos de palavras com a da vez em destaque e, se houver, a manchete no
    topo durante o vídeo todo."""
    outline = max(2, round(cfg.font_size * 0.07))
    style = (
        f"Style: Short,{cfg.font},{cfg.font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,"
        f"0,0,0,0,100,100,0,0,1,{outline},2,2,60,60,{cfg.margin_bottom},1"
    )
    head_outline = max(3, round(cfg.headline_size * 0.09))
    head_style = (
        f"Style: Headline,{cfg.font},{cfg.headline_size},&H00FFFFFF,&H00FFFFFF,&H00000000,"
        f"&H96000000,0,0,0,0,100,100,0,0,1,{head_outline},3,8,70,70,{cfg.headline_margin_top},1"
    )
    color = ass_color(cfg.highlight_color)
    pop = f"{{\\c{color}\\fscx112\\fscy112\\t(0,100,\\fscx100\\fscy100)}}"
    groups = group_words(words, cfg.words_per_line)
    events = []
    for g, group in enumerate(groups):
        texts = [_clean(w.text, cfg.uppercase) for w in group]
        next_start = groups[g + 1][0].start if g + 1 < len(groups) else None
        for i, word in enumerate(group):
            if i + 1 < len(group):
                end = group[i + 1].start
            else:
                end = word.end + LINGER
                if next_start is not None:
                    end = min(end, next_start)
            end = max(end, word.start + 0.05)
            line = " ".join(
                f"{pop}{text}{{\\r}}" if j == i else text for j, text in enumerate(texts)
            )
            events.append(
                f"Dialogue: 0,{ass_time(word.start)},{ass_time(end)},Short,,0,0,0,,{line}"
            )
    if headline.strip() and duration > 0:
        text = headline_markup(headline, color)
        span = f"0:00:00.00,{ass_time(duration)}"
        events.insert(0, f"Dialogue: 1,{span},Headline,,0,0,0,,{{\\fad(150,0)}}{text}")
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {width}\nPlayResY: {height}\n"
        "WrapStyle: 0\nScaledBorderAndShadow: yes\nYCbCr Matrix: TV.709\n\n"
        f"[V4+ Styles]\nFormat: {STYLE_FIELDS}\n{style}\n{head_style}\n\n"
        f"[Events]\nFormat: {EVENT_FIELDS}\n"
    )
    return header + "".join(f"{event}\n" for event in events)


def headline_markup(text: str, color: str) -> str:
    """Manchete em maiúsculas; a *palavra* entre asteriscos ganha a cor de destaque."""
    plain = _clean(text, uppercase=True)
    marked = re.sub(r"\*([^*]+)\*", lambda m: f"{{\\c{color}}}{m[1]}{{\\c&HFFFFFF&}}", plain)
    return marked.replace("*", "")


def ass_time(seconds: float) -> str:
    """Segundos -> H:MM:SS.cc (centésimos), o formato de tempo do .ass."""
    cs = max(0, round(seconds * 100))
    hours, cs = divmod(cs, 360_000)
    minutes, cs = divmod(cs, 6_000)
    secs, cs = divmod(cs, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def ass_color(hex_color: str) -> str:
    """'#RRGGBB' -> '&HBBGGRR&' (o .ass guarda as cores ao contrário)."""
    value = hex_color.lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", value):
        raise ValueError(f"cor inválida: {hex_color!r} (use #RRGGBB)")
    red, green, blue = value[0:2], value[2:4], value[4:6]
    return f"&H{blue}{green}{red}&".upper()


def _clean(text: str, uppercase: bool) -> str:
    """Tira o que o .ass interpretaria como comando: chaves e barras invertidas."""
    text = text.replace("\\", "").replace("{", "(").replace("}", ")")
    return text.upper() if uppercase else text
