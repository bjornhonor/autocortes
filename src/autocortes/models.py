"""Tipos que passam de uma etapa do pipeline para a outra.

Tempos sempre em segundos desde o início da gravação (ver docs/ARQUITETURA.md).
"""

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class Platform(StrEnum):
    YOUTUBE = "youtube"
    INSTAGRAM = "instagram"
    TIKTOK = "tiktok"


@dataclass
class Moment:
    """Trecho candidato a virar short."""

    start: float
    end: float
    score: float
    signals: dict[str, float] = field(default_factory=dict)  # ex.: {"chat": 4.2, "audio": 1.8}
    reason: str = ""

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class PostText:
    """Texto do post. O Claude sugere; a revisão pode editar."""

    title: str
    description: str = ""
    hashtags: list[str] = field(default_factory=list)


@dataclass
class Short:
    """Vídeo 9:16 renderizado, pronto para publicar."""

    path: Path
    moment: Moment
    text: PostText


@dataclass
class PostResult:
    """Resultado da publicação em uma plataforma."""

    platform: Platform
    ok: bool
    post_id: str | None = None
    url: str | None = None
    error: str | None = None
