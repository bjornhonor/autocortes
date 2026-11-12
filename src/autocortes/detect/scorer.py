"""Junta os sinais de cada janela num score e decide quando um pico vira candidato a corte."""

import math
from collections import deque
from dataclasses import dataclass

from autocortes.config import DetectConfig
from autocortes.detect.signals import RollingZ


@dataclass(frozen=True)
class Peak:
    t: float  # instante estimado do momento, em s desde o início da sessão
    score: float
    z_chat: float | None
    z_audio: float | None


class PeakDetector:
    """Recebe uma janela por vez, em ordem, e avisa quando ela é um pico."""

    def __init__(self, cfg: DetectConfig):
        size = max(12, round(cfg.baseline_minutes * 60 / cfg.window_seconds))
        warmup = max(6, size // 5)  # janelas de histórico antes de começar a disparar
        self.cfg = cfg
        self.chat = RollingZ(size, warmup, floor=1.0)
        self.audio = RollingZ(size, warmup, floor=1.5)
        self._last = -math.inf
        self._recent: deque[float] = deque()

    def update(self, index: int, chat: float | None, audio_db: float | None) -> Peak | None:
        """Processa a janela `index`. chat=None quando o canal está sem chat conectado."""
        z_chat = self.chat.push(chat) if chat is not None else None
        z_audio = self.audio.push(audio_db) if audio_db is not None else None
        weights = self.cfg.weights
        parts = [
            (weight, z)
            for weight, z in ((weights.chat, z_chat), (weights.audio, z_audio))
            if z is not None and weight > 0
        ]
        if not parts:
            return None
        score = sum(weight * z for weight, z in parts) / sum(weight for weight, _ in parts)
        if score < self.cfg.trigger_zscore:
            return None
        center = (index + 0.5) * self.cfg.window_seconds
        # O chat reage alguns segundos depois do que aconteceu na tela.
        t = center - (self.cfg.reaction_delay_seconds if z_chat is not None else 0.0)
        if t - self._last < self.cfg.min_gap_seconds:
            return None
        while self._recent and t - self._recent[0] >= 3600:
            self._recent.popleft()
        if len(self._recent) >= self.cfg.max_clips_per_hour:
            return None
        self._last = t
        self._recent.append(t)
        return Peak(t, score, z_chat, z_audio)
