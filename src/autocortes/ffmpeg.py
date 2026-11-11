"""ffmpeg e ffprobe: achar os executáveis, inspecionar vídeos e rodar comandos."""

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from functools import cache
from pathlib import Path

INSTALL_HINT = "winget install Gyan.FFmpeg"


class FFmpegError(RuntimeError):
    """ffmpeg/ffprobe ausente ou terminou com erro."""


def find_tool(name: str) -> str | None:
    """Acha `ffmpeg`/`ffprobe` no PATH ou na pasta onde o winget instala.

    Logo depois de instalar pelo winget, programas já abertos (terminal, editor) continuam
    com o PATH antigo; procurar na pasta do winget evita ter de reiniciar tudo.
    """
    found = shutil.which(name)
    if found:
        return found
    local_appdata = os.environ.get("LOCALAPPDATA")
    if not local_appdata:
        return None
    winget = Path(local_appdata) / "Microsoft" / "WinGet"
    candidates = [winget / "Links" / f"{name}.exe"]
    candidates += sorted(winget.glob(f"Packages/Gyan.FFmpeg*/*/bin/{name}.exe"))
    return next((str(path) for path in candidates if path.is_file()), None)


def require_tool(name: str) -> str:
    path = find_tool(name)
    if path is None:
        raise FFmpegError(f"{name} não encontrado. Instale com: {INSTALL_HINT}")
    return path


def run_ffmpeg(args: list[str], cwd: Path | None = None) -> None:
    """Roda o ffmpeg; se falhar, levanta FFmpegError com o fim do stderr."""
    cmd = [require_tool("ffmpeg"), "-hide_banner", "-nostdin", "-y", "-loglevel", "error", *args]
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-15:])
        raise FFmpegError(f"ffmpeg falhou (código {proc.returncode}):\n{tail}")


@cache
def encoder_error(encoder: str) -> str | None:
    """Testa de verdade, com um quadro, se o encoder roda aqui; devolve o erro, se houver.

    O h264_nvenc, por exemplo, depende da GPU e de um driver NVIDIA novo o bastante.
    """
    try:
        run_ffmpeg(
            ["-f", "lavfi", "-i", "color=black:s=256x256:d=0.1", "-frames:v", "1"]
            + ["-c:v", encoder, "-f", "null", "-"]
        )
    except FFmpegError as exc:
        return str(exc)
    return None


def encoder_works(encoder: str) -> bool:
    return encoder_error(encoder) is None


@dataclass(frozen=True)
class VideoInfo:
    width: int
    height: int
    fps: float
    duration: float  # 0 quando o arquivo não informa
    has_audio: bool


def probe(path: Path) -> VideoInfo:
    cmd = [
        require_tool("ffprobe"),
        "-v", "error",
        "-show_entries", "stream=codec_type,width,height,avg_frame_rate:format=duration",
        "-of", "json",
        str(path),
    ]  # fmt: skip
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise FFmpegError(f"ffprobe não conseguiu ler {path}:\n{proc.stderr.strip()}")
    data = json.loads(proc.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise FFmpegError(f"{path} não tem faixa de vídeo")
    return VideoInfo(
        width=int(video["width"]),
        height=int(video["height"]),
        fps=_rate(video.get("avg_frame_rate", "0/1")),
        duration=float(data.get("format", {}).get("duration") or 0),
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
    )


def _rate(text: str) -> float:
    num, _, den = text.partition("/")
    try:
        return float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return 0.0
