"""Typed configuration loaded from the environment (see CLAUDE.md §10).

All secrets and tunables come from environment variables, populated from a
local ``.env`` via python-dotenv. ``.env.example`` documents every key.
Telegram is optional: if its keys are absent the rest of the system still runs.

Never log secret values (tokens, proxy credentials). Redact at the edges.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Load .env once at import time; real env vars always take precedence.
load_dotenv()


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _get_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw


def _get_optional(name: str) -> str | None:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    return raw


@dataclass(frozen=True)
class Settings:
    """Resolved, typed application settings."""

    db_path: str
    poll_interval_hours: int
    web_host: str
    web_port: int
    proxy_url: str | None
    telegram_bot_token: str | None
    telegram_chat_id: str | None

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)


def load_settings() -> Settings:
    """Build a :class:`Settings` from the current environment."""
    return Settings(
        db_path=_get_str("DB_PATH", "data/tracker.db"),
        poll_interval_hours=_get_int("POLL_INTERVAL_HOURS", 6),
        web_host=_get_str("WEB_HOST", "127.0.0.1"),
        web_port=_get_int("WEB_PORT", 8000),
        proxy_url=_get_optional("PROXY_URL"),
        telegram_bot_token=_get_optional("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_get_optional("TELEGRAM_CHAT_ID"),
    )


# Module-level singleton for convenient import: ``from config import settings``.
settings = load_settings()
