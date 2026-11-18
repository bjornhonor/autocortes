"""Sinais por janela de tempo: atividade do chat, chat em coro e volume do áudio."""

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
MIN_CHORUS = 3  # autores diferentes dizendo a mesma coisa para contar como coro
LAUGH = re.compile(
    r"^(k{2,}[kj]*|(ha){2,}h?|(he){2,}|(rs){2,}|kekw\w*|lulw?|omegalul|pepelaugh|icant|lmao|lol)$"
)
# Palavras que aparecem em todo chat e não dizem nada sobre o momento.
STOPWORDS = frozenset(
    "que não nao com pra para uma uns umas ele ela eles isso esse essa este esta mas tem tá "
    "vai foi era ser ter mano cara mesmo muito mais aqui ali quem como porque pq por dos das "
    "nos nas num numa você vc voce the and you that this what".split()
)
AUDIO_RATE = 8000  # Hz; para medir volume não precisa de mais


def chat_weight(text: str) -> float:
    """Mensagem de risada ou hype vale o dobro."""
    return 2.0 if HYPE.search(text) else 1.0


def chorus_tokens(text: str) -> set[str]:
    """O que uma mensagem "grita": palavras normalizadas e, se curta, a frase inteira.

    Letras repetidas viram no máximo duas ("NAAAAO" -> "naao") e toda risada vira "kkk",
    para que "kkkkk", "KEKW" e "hahaha" contem como a mesma reação.
    """
    words = [re.sub(r"(.)\1{2,}", r"\1\1", w) for w in re.findall(r"\w+", text.lower())]
    words = ["kkk" if LAUGH.match(w) else w for w in words]
    tokens = {
        w for w in words if w == "kkk" or (len(w) >= 3 and w not in STOPWORDS and not w.isdigit())
    }
    phrase = " ".join(words)
    if 2 <= len(words) <= 6 and len(phrase) <= 40:
        tokens.add(phrase)
    return tokens


class ChatWindows:
    """Por janela de tempo: peso das mensagens e o "coro" (quantos autores dizem o mesmo)."""

    def __init__(self, window: float):
        self.window = window
        self._lock = threading.Lock()
        self._weight: dict[int, float] = defaultdict(float)
        self._authors: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._voices: dict[int, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))

    def add(self, t: float, author: str, text: str) -> None:
        index = int(t // self.window)
        with self._lock:
            for token in chorus_tokens(text):  # um autor conta uma vez por palavra
                self._voices[index][token].add(author)
            if self._authors[index][author] >= MAX_PER_AUTHOR:
                return
            self._authors[index][author] += 1
            self._weight[index] += chat_weight(text)

    def value(self, index: int) -> float:
        with self._lock:
            return self._weight.get(index, 0.0)

    def chorus(self, index: int) -> tuple[int, str]:
        """Maior coro da janela: (nº de autores diferentes, o que eles disseram).

        Menos de MIN_CHORUS pessoas não é coro (num chat pequeno, 2 risadas são acaso).
        """
        with self._lock:
            voices = self._voices.get(index)
            if not voices:
                return 0, ""
            token = max(voices, key=lambda k: (len(voices[k]), len(k)))
            count = len(voices[token])
            return (count, token) if count >= MIN_CHORUS else (0, "")

    def forget_before(self, index: int) -> None:
        with self._lock:
            for table in (self._weight, self._authors, self._voices):
                for old in [k for k in table if k < index]:
                    del table[old]


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
