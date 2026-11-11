"""Tempo como texto (SS, MM:SS, HH:MM:SS) <-> segundos."""

import math

FORMATS = "use SS, MM:SS ou HH:MM:SS"


def parse_time(text: str) -> float:
    """'45', '1:15', '1:15,5' ou '01:02:03.5' -> segundos (vírgula decimal também vale)."""
    parts = text.strip().replace(",", ".").split(":")
    try:
        values = [float(part) for part in parts]
    except ValueError:
        raise ValueError(f"tempo inválido: {text!r} ({FORMATS})") from None
    if (
        len(values) > 3
        or not all(math.isfinite(v) and v >= 0 for v in values)
        or any(v >= 60 for v in values[1:])
    ):
        raise ValueError(f"tempo inválido: {text!r} ({FORMATS})")
    seconds = 0.0
    for value in values:
        seconds = seconds * 60 + value
    return seconds


def time_tag(seconds: float) -> str:
    """Rótulo curto para nome de arquivo: 75.4 -> '1m15s', 3723 -> '1h02m03s'."""
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}h{minutes:02d}m{secs:02d}s" if hours else f"{minutes}m{secs:02d}s"
