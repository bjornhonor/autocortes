"""Sinais por janela de tempo: atividade do chat e volume do áudio."""

import math
import re
import statistics
import subprocess
import threading
from collections import defaultdict, deque
from pathlib import Path

from autocortes.ffmpeg import require_tool

HYPE = re.compile(
    r"(?i)\b(kekw|omegalul|lulw?|pog\w*|pepelaugh|monkas|clip(a|ou|e|em)?|k{3,}|(ha){2,}h?"
    r"|(rs){2,}|a{4,}h*|n[aã]o{3,}|wtf|mds|caralho|nossa|socorro)\b"
)
MAX_PER_AUTHOR = 2  # mensagens de um mesmo autor contadas por janela: spam não pesa
AUDIO_RATE = 8000  # Hz; para medir volume não precisa de mais


def chat_weight(text: str) -> float:
    """Mensagem de risada ou hype vale o dobro."""
    return 2.0 if HYPE.search(text) else 1.0


class ChatWindows:
    """Peso das mensagens do chat em cada janela de tempo."""

    def __init__(self, window: float):
        self.window = window
        self._lock = threading.Lock()
        self._weight: dict[int, float] = defaultdict(float)
        self._authors: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    def add(self, t: float, author: str, text: str) -> None:
        index = int(t // self.window)
        with self._lock:
            if self._authors[index][author] >= MAX_PER_AUTHOR:
                return
            self._authors[index][author] += 1
            self._weight[index] += chat_weight(text)

    def value(self, index: int) -> float:
        with self._lock:
            return self._weight.get(index, 0.0)

    def forget_before(self, index: int) -> None:
        with self._lock:
            for old in [k for k in self._weight if k < index]:
                del self._weight[old]
            for old in [k for k in self._authors if k < index]:
                del self._authors[old]


def audio_levels(segment: Path, start: float, window: float) -> dict[int, tuple[float, int]]:
    """Volume de um segmento por janela: {janela: (soma dos dB de cada segundo, nº de segundos)}.

    Devolve soma e contagem para que janelas divididas entre dois segmentos virem média certa.
    """
    import numpy as np

    cmd = [
        require_tool("ffmpeg"), "-v", "error", "-nostdin", "-i", str(segment),
        "-vn", "-ac", "1", "-ar", str(AUDIO_RATE), "-f", "s16le", "-",
    ]  # fmt: skip
    raw = subprocess.run(cmd, capture_output=True).stdout
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    levels: dict[int, tuple[float, int]] = {}
    for second in range(len(samples) // AUDIO_RATE):
        chunk = samples[second * AUDIO_RATE : (second + 1) * AUDIO_RATE]
        db = 20 * math.log10(float(np.sqrt(np.mean(chunk**2))) + 1e-6)
        index = int((start + second + 0.5) // window)
        total, count = levels.get(index, (0.0, 0))
        levels[index] = (total + db, count + 1)
    return levels


class RollingZ:
    """z-score robusto (mediana e MAD) de cada valor contra os anteriores."""

    def __init__(self, size: int, min_history: int, floor: float):
        self.values: deque[float] = deque(maxlen=size)
        self.min_history = min_history
        self.floor = floor  # escala mínima: evita z enorme quando o histórico é quase constante

    def push(self, value: float) -> float | None:
        """z do valor (None enquanto o histórico é curto); depois o guarda no histórico."""
        z = None
        if len(self.values) >= self.min_history:
            median = statistics.median(self.values)
            mad = statistics.median(abs(v - median) for v in self.values)
            z = (value - median) / max(1.4826 * mad, self.floor)
        self.values.append(value)
        return z
