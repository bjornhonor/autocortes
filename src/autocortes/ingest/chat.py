"""Chat ao vivo com horário. Twitch: IRC anônimo, sem login nem chave."""

import random
import re
import socket
import threading
import time
from collections.abc import Callable
from urllib.parse import urlparse

HOST, PORT = "irc.chat.twitch.tv", 6667
SILENCE_LIMIT = 360  # s sem nada do servidor (a Twitch manda PING a cada ~5 min): reconecta
PRIVMSG = re.compile(r"^(?:@\S+ )?:(?P<user>[^!\s]+)!\S+ PRIVMSG #\S+ :(?P<text>.*)$")


def twitch_channel(url: str) -> str | None:
    """'https://www.twitch.tv/Gaules' -> 'gaules'; None se não for um canal da Twitch."""
    parsed = urlparse(url if "://" in url else "https://" + url)
    if (parsed.hostname or "").lower() not in ("twitch.tv", "www.twitch.tv", "m.twitch.tv"):
        return None
    name = parsed.path.strip("/").split("/")[0].lower()
    return name if re.fullmatch(r"[a-z0-9_]{3,25}", name) else None


def parse_line(line: str) -> tuple[str, str] | None:
    """Linha do IRC -> (autor, mensagem), ou None se não for mensagem de chat."""
    match = PRIVMSG.match(line)
    return (match["user"], match["text"]) if match else None


class TwitchChat(threading.Thread):
    """Lê o chat de um canal e chama on_message(horário, autor, texto) a cada mensagem."""

    def __init__(self, channel: str, on_message: Callable[[float, str, str], None]):
        super().__init__(name=f"chat-{channel}", daemon=True)
        self.channel, self.on_message = channel, on_message
        self.connected = False
        self._halt = threading.Event()

    def stop(self) -> None:
        self._halt.set()

    def run(self) -> None:
        backoff = 2.0
        while not self._halt.is_set():
            try:
                self._listen()
                backoff = 2.0
            except OSError:
                self.connected = False
                self._halt.wait(backoff)
                backoff = min(backoff * 2, 60.0)

    def _listen(self) -> None:
        with socket.create_connection((HOST, PORT), timeout=10) as sock:
            sock.settimeout(1.0)
            nick = f"justinfan{random.randint(10000, 99999)}"
            sock.sendall(f"NICK {nick}\r\nJOIN #{self.channel}\r\n".encode())
            pending, last_data = b"", time.monotonic()
            while not self._halt.is_set():
                try:
                    data = sock.recv(65536)
                except TimeoutError:
                    if time.monotonic() - last_data > SILENCE_LIMIT:
                        raise OSError("chat sem resposta") from None
                    continue
                if not data:
                    raise OSError("o servidor fechou a conexão")
                last_data, now = time.monotonic(), time.time()
                *lines, pending = (pending + data).split(b"\r\n")
                for raw in lines:
                    line = raw.decode("utf-8", "replace")
                    if line.startswith("PING"):
                        sock.sendall(line.replace("PING", "PONG", 1).encode() + b"\r\n")
                    elif " 366 " in line:  # fim da lista de nomes: entrou no canal
                        self.connected = True
                    elif message := parse_line(line):
                        self.connected = True
                        self.on_message(now, *message)
