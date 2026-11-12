"""Segmentos .ts gravados de uma live: lista, recorte por intervalo e junção."""

import csv
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

LIST_NAME = "segments.csv"
PATTERN = "seg_%06d.ts"


@dataclass(frozen=True)
class Session:
    """Uma gravação contínua de um canal; t=0 é o primeiro quadro gravado."""

    dir: Path
    t0: float  # horário (time.time()) do primeiro quadro


@dataclass(frozen=True)
class Segment:
    path: Path
    start: float  # segundos desde o início da sessão
    end: float


def read_segments(session_dir: Path) -> list[Segment]:
    """Segmentos já fechados, em ordem (o ffmpeg só lista um segmento depois de terminá-lo)."""
    try:
        rows = (session_dir / LIST_NAME).read_text(encoding="utf-8").splitlines()
    except OSError:  # ainda não existe, ou o ffmpeg está escrevendo nele agora
        return []
    segments = []
    for row in csv.reader(rows):
        try:
            segments.append(Segment(session_dir / row[0], float(row[1]), float(row[2])))
        except (IndexError, ValueError):
            continue  # linha pela metade
    if segments and segments[0].start:
        base = segments[0].start
        segments = [Segment(s.path, s.start - base, s.end - base) for s in segments]
    return segments


def covering(segments: list[Segment], start: float, end: float) -> list[Segment]:
    """Segmentos que tocam o intervalo [start, end]."""
    return [s for s in segments if s.end > start and s.start < end]


def join(segments: list[Segment], dest: Path) -> float:
    """Emenda os segmentos num .ts só e devolve o instante (s da sessão) em que ele começa.

    MPEG-TS pode ser emendado byte a byte: a gravação não zera os timestamps a cada
    segmento, então o arquivo emendado segue contínuo.
    """
    if not segments:
        raise ValueError("nenhum segmento gravado cobre esse trecho")
    for segment in segments:
        if not segment.path.exists():
            raise ValueError(f"o trecho já foi apagado da gravação ({segment.path.name})")
    with dest.open("wb") as out:
        for segment in segments:
            with segment.path.open("rb") as src:
                shutil.copyfileobj(src, out)
    return segments[0].start


def prune(segments: list[Segment], before: float) -> int:
    """Apaga os segmentos que terminaram antes de `before` (s da sessão); devolve quantos."""
    removed = 0
    for segment in segments:
        if segment.end < before and segment.path.exists():
            try:
                segment.path.unlink()
            except OSError:  # em uso por um render agora; fica para a próxima
                continue
            removed += 1
    return removed


def remove_old_sessions(channel_dir: Path, max_age_hours: float, keep: Path | None = None) -> int:
    """Apaga gravações antigas do canal (execuções anteriores, quedas da live), menos `keep`."""
    if not channel_dir.is_dir():
        return 0
    limit = time.time() - max_age_hours * 3600
    removed = 0
    for session_dir in channel_dir.iterdir():
        if session_dir.is_dir() and session_dir != keep and session_dir.stat().st_mtime < limit:
            shutil.rmtree(session_dir, ignore_errors=True)
            removed += 1
    return removed
