"""`autocortes doctor`: confere se o ambiente está pronto para cada fase do roadmap."""

import importlib.util
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from autocortes.config import Secrets
from autocortes.ffmpeg import INSTALL_HINT, encoder_error, find_tool

# extra do pyproject -> (rótulo, [(módulo importável, pacote no PyPI)])
STAGE_PACKAGES: dict[str, tuple[str, list[tuple[str, str]]]] = {
    "render": (
        "Render",
        [("faster_whisper", "faster-whisper"), ("cv2", "opencv-python")],
    ),
    "detect": ("Detecção", [("numpy", "numpy")]),
    "ingest": ("Captura da live", [("streamlink", "streamlink")]),
    "publish": (
        "Publicação",
        [
            ("googleapiclient", "google-api-python-client"),
            ("google_auth_oauthlib", "google-auth-oauthlib"),
            ("httpx", "httpx"),
        ],
    ),
}


@dataclass
class Check:
    group: str
    name: str
    ok: bool
    detail: str = ""
    required: bool = False


def _run(*cmd: str) -> str:
    """Roda um comando e devolve stdout + stderr ("" se ele não existir ou falhar)."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout + proc.stderr if proc.returncode == 0 else ""


def _listed(output: str, name: str) -> bool:
    """True se `name` é um item da listagem de `ffmpeg -filters` ou `ffmpeg -encoders`."""
    return re.search(rf"^\s*\S+\s+{re.escape(name)}\s", output, re.MULTILINE) is not None


def system_checks() -> list[Check]:
    group = "Sistema"
    v = sys.version_info
    python = f"{v.major}.{v.minor}.{v.micro}"
    checks = [Check(group, "Python >= 3.11", v >= (3, 11), python, required=True)]

    ffmpeg = find_tool("ffmpeg")
    if ffmpeg:
        version = _run(ffmpeg, "-hide_banner", "-version").partition("\n")[0]
        filters = _run(ffmpeg, "-hide_banner", "-filters")
        encoders = _run(ffmpeg, "-hide_banner", "-encoders")
        has_ass = _listed(filters, "ass")
        nvenc_error = (
            encoder_error("h264_nvenc") if _listed(encoders, "h264_nvenc") else "ausente"
        )
        nvenc = nvenc_error is None
        checks += [
            Check(group, "ffmpeg", True, version.split(" Copyright")[0], required=True),
            Check(group, "ffprobe", find_tool("ffprobe") is not None, required=True),
            Check(
                group,
                "ffmpeg com libass",
                has_ass,
                "legendas" if has_ass else "use um build 'full' do ffmpeg",
                required=True,
            ),
            Check(group, "encoder libx264", _listed(encoders, "libx264"), required=True),
            Check(
                group,
                "encoder h264_nvenc",
                nvenc,
                "render na GPU" if nvenc_error is None else _nvenc_hint(nvenc_error),
            ),
        ]
    else:
        checks.append(Check(group, "ffmpeg", False, INSTALL_HINT, required=True))

    gpu = _run(
        "nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"
    )
    gpu = gpu.strip().partition("\n")[0]
    checks.append(
        Check(group, "GPU NVIDIA", bool(gpu), gpu or "sem GPU: Whisper roda na CPU, mais devagar")
    )
    return checks


def _nvenc_hint(error: str) -> str:
    driver = re.search(r"driver for nvenc is ([\d.]+)", error)
    if driver:
        return f"pede driver NVIDIA {driver.group(1)}+; até lá, render na CPU (libx264)"
    return "render fica na CPU (libx264)"


def package_checks() -> list[Check]:
    checks: list[Check] = []
    for extra, (label, packages) in STAGE_PACKAGES.items():
        for module, dist in packages:
            ok = importlib.util.find_spec(module) is not None
            checks.append(Check(label, dist, ok, "" if ok else f'pip install -e ".[{extra}]"'))
    return checks


MIN_CLAUDE_CODE = (2, 1, 280)  # versão da CLI que o Opus 5.5 (modelo padrão do juiz) exige


def claude_version(claude: str) -> tuple[int, int, int] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", _run(claude, "--version"))
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def _claude_check(group: str) -> Check:
    claude = shutil.which("claude")
    if claude is None:
        return Check(group, "CLI claude (juiz)", False, "instale o Claude Code")
    version = claude_version(claude)
    if version is None:
        return Check(group, "CLI claude (juiz)", False, "não deu para ler `claude --version`")
    text = ".".join(map(str, version))
    if version < MIN_CLAUDE_CODE:
        hint = f"{text}: o Opus 5.5 pede 2.1.280+ (`claude update`)"
        return Check(group, "CLI claude (juiz)", False, hint)
    return Check(group, "CLI claude (juiz)", True, text)


def _twitch_check(group: str, s: Secrets) -> Check:
    """Pede um token de app à Twitch: confirma que o client id/secret funcionam."""
    from autocortes.ingest.clips import TwitchApp, TwitchAuthError

    item = "TWITCH_CLIENT_ID + SECRET"
    if not (s.twitch_client_id and s.twitch_client_secret):
        return Check(group, item, False, "clipes dos viewers como pico - app em dev.twitch.tv")
    try:
        TwitchApp(s.twitch_client_id, s.twitch_client_secret).authorize()
    except TwitchAuthError as exc:
        return Check(group, item, False, str(exc))
    except OSError as exc:
        return Check(group, item, False, f"sem resposta da Twitch ({exc})")
    return Check(group, item, True, "clipes dos viewers ligados")


def credential_checks() -> list[Check]:
    s = Secrets()
    group = "Credenciais (.env)"
    youtube = s.youtube_client_secrets
    token = s.claude_code_oauth_token or ""
    compact = "".join(token.split())
    if not compact:
        token_detail = "rode `claude setup-token`"
    elif not compact.startswith("sk-ant-oat"):
        token_detail = "não parece um token do `claude setup-token`"
    elif compact != token:
        token_detail = "ok (o espaço que veio na cópia é ignorado)"
    else:
        token_detail = "assinatura ok"
    return [
        _claude_check(group),
        Check(
            group,
            "CLAUDE_CODE_OAUTH_TOKEN",
            compact.startswith("sk-ant-oat"),
            token_detail,
        ),
        _twitch_check(group, s),
        Check(group, "YOUTUBE_CLIENT_SECRETS", youtube.is_file(), f"publicação - {youtube}"),
        Check(
            group,
            "INSTAGRAM_USER_ID + ACCESS_TOKEN",
            bool(s.instagram_user_id and s.instagram_access_token),
            "publicação",
        ),
        Check(
            group,
            "TIKTOK_CLIENT_KEY + SECRET",
            bool(s.tiktok_client_key and s.tiktok_client_secret),
            "publicação",
        ),
    ]


def run_checks() -> list[Check]:
    return system_checks() + package_checks() + credential_checks()


def print_report(checks: list[Check], console: Console) -> bool:
    """Imprime a tabela de checagens. Devolve False se faltar algo obrigatório."""
    table = Table(title="autocortes doctor")
    for column in ("Grupo", "Item", "Status", "Detalhe / como resolver"):
        table.add_column(column)

    for i, check in enumerate(checks):
        new_group = i == 0 or checks[i - 1].group != check.group
        last_in_group = i == len(checks) - 1 or checks[i + 1].group != check.group
        if check.ok:
            status = "[green]ok[/]"
        elif check.required:
            status = "[red]falta[/]"
        else:
            status = "[yellow]pendente[/]"
        table.add_row(
            escape(check.group) if new_group else "",
            escape(check.name),
            status,
            escape(check.detail),
            end_section=last_in_group,
        )
    console.print(table)

    missing = [c.name for c in checks if c.required and not c.ok]
    if missing:
        console.print(f"[red]Falta o obrigatório:[/] {escape(', '.join(missing))}")
        return False
    console.print("[green]Base pronta.[/] Itens pendentes só são necessários na fase indicada.")
    return True
