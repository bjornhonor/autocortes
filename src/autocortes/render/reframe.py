"""Filtros do ffmpeg que levam o quadro para 9:16, um por layout.

Cada função devolve um trecho de filtergraph que lê o rótulo [src] e escreve [framed].
"""

from autocortes.ffmpeg import VideoInfo

FacecamBox = tuple[int, int, int, int]  # x, y, largura, altura no vídeo original


def even(value: float) -> int:
    """Arredonda para o par mais próximo: H.264 em yuv420p só aceita dimensões pares."""
    return max(2, round(value / 2) * 2)


def fit_crop(src_w: int, src_h: int, aspect: float) -> tuple[int, int]:
    """Maior recorte com proporção `aspect` (largura/altura) que cabe no quadro."""
    max_w, max_h = src_w - src_w % 2, src_h - src_h % 2
    if max_w / max_h > aspect:  # fonte mais larga que o alvo: usa a altura toda
        return min(even(max_h * aspect), max_w), max_h
    return max_w, min(even(max_w / aspect), max_h)


def blur(width: int, height: int) -> str:
    """Vídeo inteiro no meio; o fundo é ele mesmo ampliado e desfocado."""
    small_w, small_h = even(width / 4), even(height / 4)  # desfocar pequeno é bem mais rápido
    return (
        "[src]split=2[bgsrc][fgsrc];"
        f"[bgsrc]scale={small_w}:{small_h}:force_original_aspect_ratio=increase,"
        f"crop={small_w}:{small_h},boxblur=10:2,scale={width}:{height}[bg];"
        f"[fgsrc]scale={width}:{height}:force_original_aspect_ratio=decrease"
        ":force_divisible_by=2[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2[framed]"
    )


def center(info: VideoInfo, width: int, height: int) -> str:
    """Recorte 9:16 do meio do quadro."""
    crop_w, crop_h = fit_crop(info.width, info.height, width / height)
    x, y = (info.width - crop_w) // 2, (info.height - crop_h) // 2
    return f"[src]crop={crop_w}:{crop_h}:{x}:{y},scale={width}:{height}[framed]"


def split(info: VideoInfo, width: int, height: int, facecam: FacecamBox) -> str:
    """Webcam em cima (entre 25% e 50% da altura) e o meio do gameplay embaixo."""
    fx, fy, fw, fh = facecam
    inside = fw > 0 and fh > 0 and fx >= 0 and fy >= 0
    if not inside or fx + fw > info.width or fy + fh > info.height:
        raise ValueError(
            f"facecam {fx},{fy},{fw},{fh} não cabe no vídeo {info.width}x{info.height} "
            "(use x,y,largura,altura em pixels do vídeo original)"
        )
    top_h = even(min(max(width * fh / fw, height * 0.25), height * 0.5))
    bottom_h = height - top_h
    game_w, game_h = fit_crop(info.width, info.height, width / bottom_h)
    gx, gy = (info.width - game_w) // 2, (info.height - game_h) // 2
    return (
        "[src]split=2[cam][game];"
        f"[cam]crop={fw}:{fh}:{fx}:{fy},"
        f"scale={width}:{top_h}:force_original_aspect_ratio=increase,crop={width}:{top_h}[top];"
        f"[game]crop={game_w}:{game_h}:{gx}:{gy},scale={width}:{bottom_h}[bottom];"
        "[top][bottom]vstack=inputs=2[framed]"
    )


def face(info: VideoInfo, width: int, height: int, sendcmd_file: str, first_x: int) -> str:
    """Recorte 9:16 que anda na horizontal; as posições vêm do roteiro do sendcmd."""
    crop_w, crop_h = fit_crop(info.width, info.height, width / height)
    y = (info.height - crop_h) // 2
    return (
        f"[src]sendcmd=f={sendcmd_file},crop@face={crop_w}:{crop_h}:{first_x}:{y},"
        f"scale={width}:{height}[framed]"
    )
