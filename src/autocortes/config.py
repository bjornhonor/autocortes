"""Configuração do autocortes.

- config.yaml: parâmetros do pipeline (versionado no git).
- .env: segredos (fora do git). A CLI carrega o .env em os.environ, assim os SDKs
  (Anthropic, Google) também enxergam as variáveis.
"""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class _Section(BaseModel):
    # Chave desconhecida no config.yaml quase sempre é erro de digitação.
    model_config = ConfigDict(extra="forbid")


class CaptureConfig(_Section):
    quality: str = "best"
    segment_seconds: int = Field(10, gt=0)
    retention_hours: float = Field(2, gt=0)


class DetectWeights(_Section):
    chat: float = Field(0.3, ge=0)  # volume de mensagens
    chorus: float = Field(0.4, ge=0)  # muita gente diferente dizendo a mesma coisa
    audio: float = Field(0.3, ge=0)


class DetectConfig(_Section):
    window_seconds: int = Field(5, gt=0)
    baseline_minutes: int = Field(10, gt=0)
    trigger_zscore: float = 3.0
    reaction_delay_seconds: float = 8.0
    weights: DetectWeights = Field(default_factory=DetectWeights)
    min_gap_seconds: int = 90
    max_clips_per_hour: int = 6
    context_before_seconds: int = Field(90, gt=0)
    context_after_seconds: int = Field(30, gt=0)
    min_judge_score: int = Field(7, ge=0, le=10)
    twitch_clips: bool = True  # clipes dos viewers como pico (precisa de TWITCH_CLIENT_ID/SECRET)
    clips_poll_seconds: int = Field(60, ge=15)
    clips_min_count: int = Field(1, ge=1)  # clipes do mesmo momento para virar pico


class ClipConfig(_Section):
    min_seconds: int = Field(15, gt=0)
    max_seconds: int = Field(60, gt=0)
    pre_roll_seconds: int = 20
    post_roll_seconds: int = 8


class RenderConfig(_Section):
    layout: Literal["blur", "center", "split", "face", "gta"] = "blur"
    gta_crop: float = Field(0.8, gt=0.4, le=1.2)  # largura/altura do jogo no layout gta (4:5)
    width: int = 1080
    height: int = 1920
    fps: int = 30
    encoder: Literal["auto", "libx264", "h264_nvenc"] = "auto"
    loudness_lufs: float = -14.0
    facecam: tuple[int, int, int, int] | None = None  # x, y, w, h no vídeo original


class CaptionsConfig(_Section):
    whisper_model: str = "large-v3-turbo"
    device: Literal["auto", "cpu", "cuda"] = "auto"
    compute_type: str = "auto"
    words_per_line: int = Field(3, ge=1)
    uppercase: bool = True
    font: str = "Montserrat ExtraBold"
    font_size: int = 88
    highlight_color: str = "#FFE100"
    margin_bottom: int = 560
    headline_size: int = 80  # manchete no topo do vídeo
    headline_margin_top: int = 150


class LLMConfig(_Section):
    backend: Literal["claude-code"] = "claude-code"  # assinatura do Claude via `claude -p`
    model: str = "claude-opus-5-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    timeout_seconds: int = Field(180, gt=0)
    focus: str = ""  # o que é corte bom nestes canais (entra no prompt do juiz)
    frames: int = Field(5, ge=0, le=10)  # quadros do vídeo mandados ao juiz por pico


class PublishConfig(_Section):
    platforms: list[Literal["youtube", "instagram", "tiktok"]] = Field(
        default_factory=lambda: ["youtube", "instagram", "tiktok"]
    )
    manual_review: bool = True


class Config(_Section):
    language: str = "pt"
    data_dir: Path = Path("data")
    capture: CaptureConfig = Field(default_factory=CaptureConfig)
    detect: DetectConfig = Field(default_factory=DetectConfig)
    clip: ClipConfig = Field(default_factory=ClipConfig)
    render: RenderConfig = Field(default_factory=RenderConfig)
    captions: CaptionsConfig = Field(default_factory=CaptionsConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    publish: PublishConfig = Field(default_factory=PublishConfig)


class Secrets(BaseSettings):
    """Credenciais lidas do ambiente. Todas opcionais até a fase que as usa."""

    model_config = SettingsConfigDict(extra="ignore", env_ignore_empty=True)

    claude_code_oauth_token: str | None = None  # `claude setup-token`: juiz pela assinatura
    youtube_client_secrets: Path = Path("secrets/youtube_client_secret.json")
    instagram_user_id: str | None = None
    instagram_access_token: str | None = None
    tiktok_client_key: str | None = None
    tiktok_client_secret: str | None = None
    twitch_client_id: str | None = None
    twitch_client_secret: str | None = None


def load_config(path: Path) -> Config:
    """Lê o config.yaml; se o arquivo não existir, valem os padrões.

    Um arquivo pode começar com `base: outro.yaml`: carrega o outro primeiro e só
    sobrescreve o que estiver aqui (é assim que o ensaio.yaml herda o config.yaml).
    """
    return Config.model_validate(_read_yaml(path))


def _read_yaml(path: Path, depth: int = 0) -> dict:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    base = data.pop("base", None)
    if base and depth < 5:
        return _merge(_read_yaml(path.parent / base, depth + 1), data)
    return data


def _merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged
