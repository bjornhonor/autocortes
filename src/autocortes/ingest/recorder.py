"""Gravação de uma live: streamlink -> ffmpeg em segmentos .ts curtos, sem reencode."""

import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from autocortes.ffmpeg import require_tool
from autocortes.ingest.segments import LIST_NAME, PATTERN, Session

RETRY_SECONDS = 15  # espera antes de tentar de novo quando a live cai
# Twitch em baixa latência: vídeo e chat ficam mais alinhados. (O --twitch-disable-ads não
# existe mais no streamlink 8.)
STREAMLINK_OPTIONS = ["--twitch-low-latency"]


class Recorder(threading.Thread):
    """Mantém um canal gravando; se a live cair, espera voltar e abre uma sessão nova."""

    def __init__(
        self,
        url: str,
        channel_dir: Path,
        quality: str,
        segment_seconds: int,
        on_session: Callable[[Session], None],
        on_status: Callable[[str], None] = lambda _: None,
    ):
        super().__init__(name=f"gravador-{channel_dir.name}", daemon=True)
        self.url, self.channel_dir, self.quality = url, channel_dir, quality
        self.segment_seconds = segment_seconds
        self.on_session, self.on_status = on_session, on_status
        self._halt = threading.Event()
        self._source: subprocess.Popen | None = None

    def stop(self) -> None:
        self._halt.set()
        source = self._source
        if source is not None and source.poll() is None:
            source.terminate()  # sem entrada, o ffmpeg fecha o último segmento e sai

    def run(self) -> None:
        while not self._halt.is_set():
            self._record()
            if self._halt.wait(RETRY_SECONDS):
                break

    def _record(self) -> None:
        session_dir = self.channel_dir / time.strftime("%Y%m%d-%H%M%S")
        session_dir.mkdir(parents=True, exist_ok=True)
        streamlink = [
            sys.executable, "-m", "streamlink", "--stdout", "--loglevel", "error",
            *STREAMLINK_OPTIONS, "--retry-streams", "30", "--retry-open", "3",
            self.url, self.quality,
        ]  # fmt: skip
        ffmpeg = [
            require_tool("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error",
            "-i", "pipe:0", "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
            "-f", "segment", "-segment_time", str(self.segment_seconds),
            "-segment_list", LIST_NAME, "-segment_list_type", "csv", PATTERN,
        ]  # fmt: skip
        first = session_dir / (PATTERN % 0)
        started = False
        self.on_status("esperando a live (o streamlink tenta de 30 em 30 s)")
        log_path = session_dir / "gravacao.log"
        with log_path.open("ab") as log:
            source = subprocess.Popen(streamlink, stdout=subprocess.PIPE, stderr=log)
            sink = subprocess.Popen(ffmpeg, stdin=source.stdout, stderr=log, cwd=session_dir)
            assert source.stdout is not None
            source.stdout.close()  # o ffmpeg é o único leitor do pipe
            self._source = source
            try:
                while sink.poll() is None and not self._halt.is_set():
                    if not started and first.exists():
                        started = True
                        self.on_session(Session(session_dir, first.stat().st_ctime))
                        self.on_status("gravando")
                    time.sleep(0.5)
            finally:
                if source.poll() is None:
                    source.terminate()
                try:
                    sink.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    sink.kill()
                source.wait()
        if started:
            self.on_status("a gravação parou (live caiu ou acabou)")
        else:  # nada gravado: conta o porquê e não deixa pasta vazia para trás
            lines = log_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
            if lines and not self._halt.is_set():
                self.on_status(f"não gravou: {lines[-1][:200]}")
            shutil.rmtree(session_dir, ignore_errors=True)
