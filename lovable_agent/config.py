"""설정 로딩 — TOML 파일에서 읽고 dataclass로 변환.

config.toml은 .gitignore에 막혀있으므로 실제 운영 환경에서만 존재.
config.example.toml 을 복사해서 채워 쓰기.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AnthropicConfig:
    api_key_env: str = "ANTHROPIC_API_KEY"
    model: str = "claude-sonnet-4-6"
    fallback_model: str = "claude-opus-4-7"


@dataclass(frozen=True)
class NotionConfig:
    api_token_env: str = "NOTION_API_TOKEN"
    tasks_db_id: str = ""
    whitelist_db_id: str = ""
    inbox_page_id: str = ""


@dataclass(frozen=True)
class PathsConfig:
    inbox_folder: str = "~/lovable-agent/inbox/"
    db_path: str = "~/lovable-agent/agent.db"
    screenshot_temp_dir: str = "~/lovable-agent/screenshots/"


@dataclass(frozen=True)
class SchedulingConfig:
    reminder_check_interval_seconds: int = 60
    notion_poll_interval_seconds: int = 300
    late_reminder_threshold_hours: int = 6
    default_reminder_offsets_hours: tuple[int, ...] = (24, 0)


@dataclass(frozen=True)
class KakaoConfig:
    max_send_retries: int = 3
    send_retry_delay_seconds: int = 30
    require_friends_tab_reset: bool = True
    require_hwnd_snapshot_diff: bool = True
    require_title_exact_match: bool = True
    inter_message_delay_min_seconds: int = 5
    inter_message_delay_max_seconds: int = 15
    rest_after_n_messages: int = 10
    rest_duration_min_seconds: int = 60
    rest_duration_max_seconds: int = 120
    detection_mode: str = "uia_first"


@dataclass(frozen=True)
class SafetyConfig:
    message_prefix: str = "[AI 자동 팔로우업] "
    require_status_confirmed: bool = True
    double_check_whitelist: bool = True


@dataclass(frozen=True)
class Config:
    anthropic: AnthropicConfig
    notion: NotionConfig
    paths: PathsConfig
    scheduling: SchedulingConfig
    kakao: KakaoConfig
    safety: SafetyConfig


def load_config(path: str | Path | None = None) -> Config:
    """TOML 파일에서 설정 로드. 파일 없으면 모든 필드 기본값.

    Args:
        path: config.toml 경로. None이면 프로젝트 루트의 config.toml 시도.
    """
    cfg_path = Path(path) if path else Path("config.toml")
    if not cfg_path.exists():
        return Config(
            anthropic=AnthropicConfig(),
            notion=NotionConfig(),
            paths=PathsConfig(),
            scheduling=SchedulingConfig(),
            kakao=KakaoConfig(),
            safety=SafetyConfig(),
        )

    with cfg_path.open("rb") as f:
        data = tomllib.load(f)

    return Config(
        anthropic=AnthropicConfig(**data.get("anthropic", {})),
        notion=NotionConfig(**data.get("notion", {})),
        paths=PathsConfig(**data.get("paths", {})),
        scheduling=SchedulingConfig(**data.get("scheduling", {})),
        kakao=KakaoConfig(**data.get("kakao", {})),
        safety=SafetyConfig(**data.get("safety", {})),
    )


def get_secret(env_name: str) -> str | None:
    """환경변수에서 시크릿 조회 — 평문 파일에 두지 않기 위한 단일 진입점."""
    return os.environ.get(env_name)
