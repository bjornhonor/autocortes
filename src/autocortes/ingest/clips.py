"""Clipes que os viewers criam na Twitch: o sinal mais forte de que algo aconteceu na live.

Usa a API oficial da Twitch (Helix) com um token de app (client credentials): basta registrar
um app em dev.twitch.tv/console e pôr TWITCH_CLIENT_ID e TWITCH_CLIENT_SECRET no .env. Não
precisa de login de ninguém, só lê clipes públicos.
"""

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
API_URL = "https://api.twitch.tv/helix"
TIMEOUT = 15  # s por requisição
MAX_PAGES = 3  # até 300 clipes por consulta; a sessão de uma live raramente passa disso


class TwitchAuthError(Exception):
    """A Twitch recusou o client id/secret: não adianta tentar de novo."""


@dataclass(frozen=True)
class Clip:
    id: str
    created: float  # instante da criação, em segundos desde 1970 (como time.time())
    duration: float
    title: str
    views: int
    url: str


def parse_time(text: str) -> float:
    """RFC 3339 da Twitch ('2026-11-19T22:15:03Z') -> segundos desde 1970."""
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def rfc3339(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_clip(data: dict) -> Clip:
    return Clip(
        id=str(data["id"]),
        created=parse_time(data["created_at"]),
        duration=float(data.get("duration") or 30.0),
        title=str(data.get("title") or "").strip(),
        views=int(data.get("view_count") or 0),
        url=str(data.get("url") or ""),
    )


Opener = Callable[[urllib.request.Request], bytes]


def _open(request: urllib.request.Request) -> bytes:
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read()


class TwitchApp:
    """Token de app compartilhado por todos os canais; pede outro quando vence ou é recusado."""

    def __init__(self, client_id: str, client_secret: str, opener: Opener = _open):
        # Sem espaços: a cópia do painel da Twitch às vezes traz um no fim.
        self.client_id, self._secret = client_id.strip(), client_secret.strip()
        self._open = opener
        self._lock = threading.Lock()
        self._token = ""
        self._expires = 0.0

    def _fetch_token(self) -> None:
        body = urllib.parse.urlencode(
            {
                "client_id": self.client_id,
                "client_secret": self._secret,
                "grant_type": "client_credentials",
            }
        ).encode()
        try:
            data = json.loads(self._open(urllib.request.Request(TOKEN_URL, data=body)))
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 401, 403):
                raise TwitchAuthError("client id ou secret da Twitch recusado") from exc
            raise
        self._token = data["access_token"]
        self._expires = time.monotonic() + float(data.get("expires_in", 3600)) - 60

    def _bearer(self, renew: bool = False) -> str:
        with self._lock:
            if renew or not self._token or time.monotonic() >= self._expires:
                self._fetch_token()
            return self._token

    def authorize(self) -> None:
        """Pede um token agora: TwitchAuthError se o client id/secret não valem."""
        self._bearer(renew=True)

    def get(self, path: str, params: list[tuple[str, str]]) -> dict:
        url = f"{API_URL}/{path}?{urllib.parse.urlencode(params)}"
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {self._bearer(renew=attempt > 0)}"}
            headers["Client-Id"] = self.client_id
            try:
                return json.loads(self._open(urllib.request.Request(url, headers=headers)))
            except urllib.error.HTTPError as exc:
                if exc.code != 401 or attempt:
                    raise
        raise AssertionError("inalcançável")

    def user_id(self, login: str) -> str | None:
        users = self.get("users", [("login", login)]).get("data", [])
        return users[0]["id"] if users else None

    def clips(self, broadcaster_id: str, since: float) -> list[Clip]:
        """Clipes do canal criados desde `since` (a API arredonda para o minuto)."""
        params = [("broadcaster_id", broadcaster_id), ("started_at", rfc3339(since))]
        params.append(("first", "100"))
        found: list[Clip] = []
        cursor = ""
        for _ in range(MAX_PAGES):
            page = self.get("clips", params + ([("after", cursor)] if cursor else []))
            found += [parse_clip(item) for item in page.get("data", [])]
            cursor = (page.get("pagination") or {}).get("cursor", "")
            if not cursor:
                break
        return [clip for clip in found if clip.created >= since]


class ClipWatcher(threading.Thread):
    """Consulta os clipes de um canal a cada `every` segundos e avisa os novos."""

    def __init__(
        self,
        app: TwitchApp,
        login: str,
        since: Callable[[], float | None],
        on_clips: Callable[[list[Clip]], None],
        on_status: Callable[[str], None],
        every: float = 60.0,
    ):
        super().__init__(name=f"clipes-{login}", daemon=True)
        self.app, self.login, self.since = app, login, since
        self.on_clips, self.on_status, self.every = on_clips, on_status, every
        self._halt = threading.Event()
        self._seen: set[str] = set()
        self._failing = False

    def stop(self) -> None:
        self._halt.set()

    def run(self) -> None:
        broadcaster = None
        while not self._halt.is_set():
            try:
                if broadcaster is None:
                    broadcaster = self.app.user_id(self.login)
                    if broadcaster is None:
                        self.on_status(f"clipes: canal '{self.login}' não existe na Twitch")
                        return
                    self.on_status("clipes da Twitch ligados")
                self.check(broadcaster)
                if self._failing:
                    self.on_status("clipes da Twitch de volta")
                self._failing = False
            except TwitchAuthError as exc:
                self.on_status(f"clipes desligados: {exc}")
                return
            except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
                if not self._failing:  # avisa uma vez, não a cada minuto
                    self.on_status(f"clipes da Twitch falharam ({exc}); tentando de novo")
                self._failing = True
            self._halt.wait(self.every)

    def check(self, broadcaster: str) -> None:
        since = self.since()
        if since is None:  # ainda não começou a gravar
            return
        new = [c for c in self.app.clips(broadcaster, since) if c.id not in self._seen]
        self._seen.update(c.id for c in new)
        if new:
            self.on_clips(sorted(new, key=lambda c: c.created))
