"""Linha de comando do autocortes."""

import json
import sys
import time
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from dotenv import find_dotenv, load_dotenv
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from autocortes import __version__, live
from autocortes.config import Config, load_config
from autocortes.doctor import print_report, run_checks
from autocortes.render.pipeline import render_short
from autocortes.timecode import parse_time, time_tag

app = typer.Typer(
    help="Assiste lives, encontra os melhores momentos e publica shorts.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


class Layout(StrEnum):
    blur = "blur"
    center = "center"
    split = "split"
    face = "face"


@app.callback()
def main(
    ctx: typer.Context,
    config: Annotated[
        Path, typer.Option("--config", "-c", help="Arquivo de parâmetros do pipeline.")
    ] = Path("config.yaml"),
) -> None:
    # Carrega o .env e valida o config.yaml logo na largada; os comandos leem ctx.obj.
    load_dotenv(find_dotenv(usecwd=True))
    ctx.obj = load_config(config)


@app.command()
def doctor() -> None:
    """Confere ffmpeg, GPU, pacotes de cada fase e credenciais."""
    ok = print_report(run_checks(), console)
    raise typer.Exit(code=0 if ok else 1)


@app.command()
def watch(
    ctx: typer.Context,
    urls: Annotated[list[str], typer.Argument(help="Lives para acompanhar, ex.: twitch.tv/canal.")],
    judge: Annotated[bool, typer.Option(help="Pedir ao Claude para julgar cada pico.")] = True,
    minutes: Annotated[
        float | None, typer.Option(help="Parar sozinho depois de N minutos (testes).")
    ] = None,
) -> None:
    """Acompanha uma ou mais lives ao mesmo tempo; os shorts saem em data/fila."""
    cfg: Config = ctx.obj

    def log(channel: str, message: str, style: str = "") -> None:
        line = f"[dim]{time.strftime('%H:%M:%S')}[/] [bold]{escape(channel)}[/] {escape(message)}"
        console.print(f"[{style}]{line}[/]" if style else line)

    folder = escape(str(cfg.data_dir / live.QUEUE_DIR))
    console.print(f"Acompanhando {len(urls)} live(s); shorts em {folder}. Ctrl+C para parar.")
    try:
        live.watch(urls, cfg, use_claude=judge, minutes=minutes, log=log)
    except KeyboardInterrupt:
        console.print("Parado.")


@app.command("queue")
def show_queue(
    ctx: typer.Context, limit: Annotated[int, typer.Option(help="Quantos mostrar.")] = 20
) -> None:
    """Lista os shorts prontos na fila de revisão, do mais novo para o mais antigo."""
    cfg: Config = ctx.obj
    folder = cfg.data_dir / live.QUEUE_DIR
    items = sorted(folder.glob("*.json"), reverse=True)[:limit]
    if not items:
        console.print(f"Fila vazia ({escape(str(folder))}).")
        return
    table = Table(title=f"Fila de revisão: {folder}")
    for column in ("Quando", "Canal", "Nota", "Título", "Arquivo"):
        table.add_column(column)
    for path in items:
        data = json.loads(path.read_text(encoding="utf-8"))
        score = f"{data['score']}" + ("" if data.get("judged") else "*")
        when = data["created"][5:16].replace("T", " ")
        table.add_row(
            when, escape(data["channel"]), score, escape(data["title"]), escape(data["video"])
        )
    console.print(table)
    console.print("* sem juiz: corte só pelos sinais de chat e áudio")


@app.command()
def render(
    ctx: typer.Context,
    video: Annotated[Path, typer.Argument(help="Vídeo de origem.", exists=True, dir_okay=False)],
    start: Annotated[str, typer.Option("--start", "-s", help="Início: SS, MM:SS ou HH:MM:SS.")],
    end: Annotated[str, typer.Option("--end", "-e", help="Fim: SS, MM:SS ou HH:MM:SS.")],
    layout: Annotated[
        Layout | None, typer.Option("--layout", "-l", help="Padrão: render.layout do config.")
    ] = None,
    out: Annotated[Path | None, typer.Option("--out", "-o", help="Arquivo .mp4 de saída.")] = None,
    facecam: Annotated[
        str | None, typer.Option(help="Webcam no vídeo original (layout split): x,y,w,h.")
    ] = None,
    captions: Annotated[bool, typer.Option(help="Gerar legenda.")] = True,
) -> None:
    """Gera um short 9:16 legendado a partir de um trecho do vídeo."""
    cfg: Config = ctx.obj
    try:
        t_start, t_end = parse_time(start), parse_time(end)
        box = _parse_facecam(facecam) if facecam else None
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    chosen = layout.value if layout else cfg.render.layout
    name = f"{video.stem}_{time_tag(t_start)}-{time_tag(t_end)}_{chosen}.mp4"
    target = out or cfg.data_dir / "shorts" / name

    try:
        with console.status("Preparando...") as status:
            result = render_short(
                video,
                t_start,
                t_end,
                target,
                cfg,
                layout=chosen,
                facecam=box,
                with_captions=captions,
                on_step=lambda step: status.update(f"{step}..."),
            )
    except (RuntimeError, ValueError) as exc:  # FFmpegError é um RuntimeError
        console.print(f"[red]Erro:[/] {escape(str(exc))}")
        raise typer.Exit(1) from None

    console.print(f"[green]Short pronto:[/] {escape(str(result.path))}")
    details = [f"{result.duration:.1f} s", f"layout {result.layout}", f"encoder {result.encoder}"]
    if result.transcript is not None:
        words = len(result.transcript.words)
        details.append(f"{words} palavras (Whisper na {result.transcript.device.upper()})")
    console.print("  " + " | ".join(details))
    for note in result.notes:
        console.print(f"  [yellow]{escape(note)}[/]")


@app.command()
def version() -> None:
    """Mostra a versão instalada."""
    console.print(f"autocortes {__version__}")


def _parse_facecam(text: str) -> tuple[int, int, int, int]:
    try:
        x, y, w, h = (int(part) for part in text.split(","))
    except ValueError:
        raise ValueError(f"facecam inválida: {text!r} (use x,y,w,h em pixels)") from None
    return x, y, w, h


def run() -> None:
    """Ponto de entrada do executável `autocortes`."""
    # Fora de um console (pipe, arquivo, Git Bash) o Python no Windows escreveria em cp1252
    # e os acentos chegariam quebrados. No console isto não muda nada: ele já é UTF-8.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    app(prog_name="autocortes")
