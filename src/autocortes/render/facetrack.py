"""Layout `face`: acha o rosto ao longo do trecho e planeja um recorte que o acompanha."""

import hashlib
import os
import statistics
import subprocess
import tempfile
import urllib.request
from pathlib import Path

from autocortes.ffmpeg import FFmpegError, VideoInfo, require_tool
from autocortes.render.reframe import even

MODEL_NAME = "face_detection_yunet_2023mar.onnx"
MODEL_URL = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/" + MODEL_NAME
)
MODEL_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"

SAMPLE_FPS = 4  # quadros analisados por segundo
DETECT_WIDTH = 640  # a detecção roda numa cópia reduzida do quadro
DEAD_ZONE = 0.12  # quanto o rosto anda (fração da largura do recorte) antes de a câmera seguir
CUT_JUMP = 0.25  # salto (fração da largura da fonte) que vira corte seco em vez de panorâmica
PAN_SPEED = 1.2  # velocidade máxima da panorâmica, em larguras de recorte por segundo

Sample = tuple[float, float | None]  # (t, centro x do rosto em pixels da fonte, ou None)


def ensure_model(models_dir: Path) -> Path:
    """Baixa o detector de rosto YuNet (OpenCV Zoo, ~230 KB) na primeira vez que for usado."""
    path = models_dir / MODEL_NAME
    if path.is_file() and _sha256(path) == MODEL_SHA256:
        return path
    models_dir.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(MODEL_NAME + ".part")
    urllib.request.urlretrieve(MODEL_URL, partial)
    if _sha256(partial) != MODEL_SHA256:
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"o download de {MODEL_NAME} não bateu com o checksum esperado")
    partial.replace(path)
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def detect_centers(
    video: Path, start: float, duration: float, info: VideoInfo, model: Path
) -> list[Sample]:
    """Centro x do rosto principal a cada 1/SAMPLE_FPS s do trecho."""
    os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")  # o OpenCV 5 avisa coisas irrelevantes
    import cv2
    import numpy as np

    scale = DETECT_WIDTH / info.width
    width, height = DETECT_WIDTH, even(info.height * scale)
    detector = cv2.FaceDetectorYN.create(str(model), "", (width, height), 0.6)
    cmd = [
        require_tool("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(video),
        "-vf", f"fps={SAMPLE_FPS},scale={width}:{height}",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-",
    ]  # fmt: skip
    frame_size = width * height * 3
    samples: list[Sample] = []
    previous: float | None = None
    with tempfile.TemporaryFile() as errors:
        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errors) as proc:
            assert proc.stdout is not None
            while len(buffer := proc.stdout.read(frame_size)) == frame_size:
                frame = np.frombuffer(buffer, np.uint8).reshape(height, width, 3)
                _, faces = detector.detect(frame)
                center = pick_face(faces, previous)
                previous = center if center is not None else previous
                t = len(samples) / SAMPLE_FPS
                samples.append((t, None if center is None else center / scale))
        if proc.returncode != 0:
            errors.seek(0)
            message = errors.read().decode("utf-8", "replace").strip()[-2000:]
            raise FFmpegError(f"ffmpeg falhou ao ler os quadros:\n{message}")
    return samples


def pick_face(faces, previous: float | None) -> float | None:
    """Centro x do rosto a seguir: o maior ou, entre os de tamanho parecido, o mais perto do
    anterior (evita a câmera pular entre duas pessoas a cada quadro)."""
    if faces is None or len(faces) == 0:
        return None
    boxes = [(float(f[0]) + float(f[2]) / 2, float(f[2]) * float(f[3])) for f in faces]
    largest = max(area for _, area in boxes)
    similar = [box for box in boxes if box[1] >= 0.5 * largest]
    if previous is None:
        return max(similar, key=lambda box: box[1])[0]
    return min(similar, key=lambda box: abs(box[0] - previous))[0]


def plan_track(
    samples: list[Sample], src_w: int, crop_w: int, fps: float
) -> list[tuple[float, int]]:
    """Centros do rosto -> posições x do recorte ao longo do tempo (só os instantes de mudança).

    - Instantes sem rosto herdam a posição conhecida mais próxima, e uma mediana móvel
      descarta detecções erradas isoladas.
    - Zona morta: o recorte só anda quando o rosto se afasta do centro dele.
    - Salto grande que se mantém (troca de plano ou de pessoa) vira corte seco; o resto,
      panorâmica com velocidade limitada.
    """
    max_x = src_w - crop_w
    centers = _fill_gaps([center for _, center in samples])
    if max_x <= 0 or not centers:
        return [(0.0, max(0, max_x // 2))]
    targets = [min(max(c - crop_w / 2, 0.0), float(max_x)) for c in _median(centers, 5)]
    dead, jump = DEAD_ZONE * crop_w, CUT_JUMP * src_w
    max_step = PAN_SPEED * crop_w / SAMPLE_FPS

    keys: list[tuple[float, float, bool]] = []  # (t, x, é corte seco)
    position, moving = targets[0], False
    for i, ((t, _), target) in enumerate(zip(samples, targets, strict=True)):
        gap = target - position
        lasting = i + 1 == len(targets) or abs(targets[i + 1] - position) > jump
        if abs(gap) > jump and lasting:
            position, moving = target, False
            keys.append((t, position, True))
            continue
        if abs(gap) > dead:
            moving = True
        elif abs(gap) < dead / 4:
            moving = False
        if moving:
            position += max(-max_step, min(max_step, 0.6 * gap))
        keys.append((t, position, False))
    return _to_frames(keys, fps, max_x)


def sendcmd_script(track: list[tuple[float, int]]) -> str:
    """Roteiro do filtro sendcmd: a cada instante, a nova posição x do recorte `crop@face`."""
    return "".join(f"{t:.3f} crop@face x {x};\n" for t, x in track)


def _fill_gaps(values: list[float | None]) -> list[float]:
    known = [v for v in values if v is not None]
    if not known:
        return []
    filled, last = [], known[0]
    for value in values:
        last = value if value is not None else last
        filled.append(last)
    return filled


def _median(values: list[float], window: int) -> list[float]:
    half = window // 2
    return [statistics.median(values[max(0, i - half) : i + half + 1]) for i in range(len(values))]


def _to_frames(
    keys: list[tuple[float, float, bool]], fps: float, max_x: int
) -> list[tuple[float, int]]:
    """Posição em cada quadro (interpolada; corte seco não interpola), só onde ela muda."""
    track: list[tuple[float, int]] = []
    k, frame = 0, 0
    while (t := frame / fps) <= keys[-1][0] + 1e-9:
        while k + 1 < len(keys) and keys[k + 1][0] <= t + 1e-9:
            k += 1
        t0, x, _ = keys[k]
        if k + 1 < len(keys) and not keys[k + 1][2]:
            t1, x1, _ = keys[k + 1]
            x += (x1 - x) * (t - t0) / (t1 - t0)
        position = min(max(round(x / 2) * 2, 0), max_x - max_x % 2)
        if not track or track[-1][1] != position:
            track.append((round(t, 3), position))
        frame += 1
    return track
