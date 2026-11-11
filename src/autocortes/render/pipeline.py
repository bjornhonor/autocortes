"""Do vídeo original ao short: uma passada de ffmpeg com corte, 9:16, legenda e áudio.

O ffmpeg roda com a pasta de trabalho temporária como diretório atual, e a legenda, as
fontes e o roteiro do recorte entram por caminho relativo: caminho do Windows (C:\\...)
dentro de um filtergraph exige escapes frágeis.
"""

import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from autocortes.config import Config
from autocortes.ffmpeg import VideoInfo, encoder_works, probe, run_ffmpeg
from autocortes.render import captions, facetrack, reframe

SHORTS_MAX_SECONDS = 180  # acima disso o YouTube não trata o vídeo como Short

VIDEO_CODECS = {
    "libx264": ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-profile:v", "high"],
    "h264_nvenc": [
        "-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "23", "-b:v", "0",
        "-profile:v", "high",
    ],
}  # fmt: skip
AUDIO_CODEC = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000"]


@dataclass
class RenderResult:
    path: Path
    duration: float
    layout: str
    encoder: str
    transcript: captions.Transcript | None
    captions_file: Path | None
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Clip:
    video: Path
    start: float
    duration: float
    info: VideoInfo

    def input_args(self) -> list[str]:
        return ["-ss", f"{self.start:.3f}", "-t", f"{self.duration:.3f}", "-i", str(self.video)]


def render_short(
    video: Path,
    start: float,
    end: float,
    out: Path,
    cfg: Config,
    *,
    layout: str | None = None,
    facecam: reframe.FacecamBox | None = None,
    with_captions: bool = True,
    on_step: Callable[[str], None] = lambda _: None,
) -> RenderResult:
    """Corta [start, end] de `video`, leva para 9:16, legenda e grava `out` (.mp4)."""
    info = probe(video)
    if info.duration:
        if start >= info.duration:
            raise ValueError(
                f"o início ({start:.1f} s) passa do fim do vídeo ({info.duration:.1f} s)"
            )
        end = min(end, info.duration)
    if end <= start:
        raise ValueError("o fim do trecho precisa vir depois do início")
    clip = _Clip(video.resolve(), start, end - start, info)
    layout = layout or cfg.render.layout
    notes: list[str] = []
    if clip.duration > SHORTS_MAX_SECONDS:
        notes.append(f"Trecho de {clip.duration:.0f} s: acima de 3 min não conta como Short.")

    out = out.resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="autocortes-") as tmp:
        work = Path(tmp)
        transcript = None
        if with_captions and info.has_audio:
            on_step("Transcrevendo o áudio")
            transcript = _transcribe(clip, work, cfg)
            if transcript.gpu_error:
                notes.append(f"Whisper rodou na CPU; a GPU falhou: {transcript.gpu_error}")
            if not transcript.words:
                notes.append("Nenhuma fala reconhecida: short sem legenda.")
        elif with_captions:
            notes.append("O vídeo não tem áudio: short sem legenda.")
        subtitled = transcript is not None and bool(transcript.words)
        if subtitled:
            ass = captions.build_ass(
                transcript.words, cfg.captions, cfg.render.width, cfg.render.height
            )
            (work / "captions.ass").write_text(ass, encoding="utf-8")
            _copy_fonts(work / "fonts")

        framing = _framing(layout, clip, cfg, facecam or cfg.render.facecam, work, notes, on_step)

        on_step("Renderizando")
        encoder = _pick_encoder(cfg.render.encoder)
        finish = "setsar=1" + (",ass=captions.ass:fontsdir=fonts" if subtitled else "")
        graph = (
            f"[0:v]fps={cfg.render.fps},format=yuv420p[src];{framing};"
            f"[framed]{finish},format=yuv420p[v]"
        )
        maps, codecs = ["-map", "[v]"], list(VIDEO_CODECS[encoder])
        if info.has_audio:
            lufs = cfg.render.loudness_lufs
            graph += f";[0:a]loudnorm=I={lufs}:TP=-1.5:LRA=11,aresample=48000[a]"
            maps += ["-map", "[a]"]
            codecs += AUDIO_CODEC
        args = [*clip.input_args(), "-filter_complex", graph, *maps, *codecs]
        run_ffmpeg([*args, "-movflags", "+faststart", str(out)], cwd=work)

        captions_file = None
        if subtitled:
            captions_file = out.with_suffix(".ass")
            shutil.copyfile(work / "captions.ass", captions_file)
    return RenderResult(out, clip.duration, layout, encoder, transcript, captions_file, notes)


def _framing(
    layout: str,
    clip: _Clip,
    cfg: Config,
    facecam: reframe.FacecamBox | None,
    work: Path,
    notes: list[str],
    on_step: Callable[[str], None],
) -> str:
    """Trecho do filtergraph que leva [src] para 9:16 em [framed]."""
    width, height = cfg.render.width, cfg.render.height
    if layout == "blur":
        return reframe.blur(width, height)
    if layout == "center":
        return reframe.center(clip.info, width, height)
    if layout == "split":
        if facecam is None:
            raise ValueError(
                "o layout split precisa saber onde está a webcam: "
                "render.facecam no config.yaml ou --facecam x,y,w,h"
            )
        return reframe.split(clip.info, width, height, facecam)
    if layout == "face":
        on_step("Procurando o rosto")
        model = facetrack.ensure_model(cfg.data_dir / "models")
        samples = facetrack.detect_centers(clip.video, clip.start, clip.duration, clip.info, model)
        found = sum(center is not None for _, center in samples)
        if not found:
            notes.append("Nenhum rosto encontrado: ficou o recorte central.")
        elif found < len(samples) / 2:
            notes.append(f"O rosto só apareceu em {100 * found // len(samples)}% do trecho.")
        crop_w, _ = reframe.fit_crop(clip.info.width, clip.info.height, width / height)
        track = facetrack.plan_track(samples, clip.info.width, crop_w, cfg.render.fps)
        (work / "face.cmd").write_text(facetrack.sendcmd_script(track), encoding="utf-8")
        return reframe.face(clip.info, width, height, "face.cmd", track[0][1])
    raise ValueError(f"layout desconhecido: {layout!r} (use blur, center, split ou face)")


def _transcribe(clip: _Clip, work: Path, cfg: Config) -> captions.Transcript:
    wav = work / "audio.wav"
    pcm = ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le"]
    run_ffmpeg([*clip.input_args(), *pcm, str(wav)])
    return captions.transcribe(wav, cfg.captions, cfg.language)


def _copy_fonts(dest: Path) -> None:
    """Copia as fontes que vêm no pacote (assets/fonts) para a pasta de trabalho."""
    dest.mkdir(exist_ok=True)
    fonts = resources.files("autocortes").joinpath("assets").joinpath("fonts")
    for entry in fonts.iterdir():
        if entry.name.lower().endswith((".ttf", ".otf")):
            (dest / entry.name).write_bytes(entry.read_bytes())


def _pick_encoder(choice: str) -> str:
    if choice != "auto":
        return choice
    return "h264_nvenc" if encoder_works("h264_nvenc") else "libx264"
