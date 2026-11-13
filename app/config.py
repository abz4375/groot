"""Runtime configuration.

Everything is overridable through environment variables (or a local ``.env``),
which is what the container uses.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_agy_home() -> Path:
    """Where agy keeps its credentials and settings.

    agy hard-codes ``$HOME/.gemini/antigravity-cli`` for its profile store, so
    the default must follow ``HOME`` rather than being a fixed literal.
    """
    home = os.environ.get("HOME") or str(Path.home())
    return Path(home) / ".gemini" / "antigravity-cli"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- agy CLI ---------------------------------------------------------
    agy_bin: str = "agy"
    agy_home: Path = Field(default_factory=_default_agy_home)
    agy_token_filename: str = "antigravity-oauth-token"
    #: Extra environment variables handed to every agy process (JSON object).
    agy_env: dict[str, str] = Field(default_factory=dict)

    # --- HTTP API --------------------------------------------------------
    api_bearer_token: str = ""
    cors_origins: str = ""
    log_level: str = "INFO"

    # --- workspace -------------------------------------------------------
    workspace_root: Path = Path("/workspace")
    default_workdir: str = "."

    # --- run limits ------------------------------------------------------
    max_concurrency: int = 2
    queue_wait_seconds: float = 1.0
    default_timeout_seconds: int = 900
    max_timeout_seconds: int = 3600
    timeout_grace_seconds: int = 30
    max_prompt_chars: int = 200_000
    max_stdout_bytes: int = 8_000_000
    max_stderr_bytes: int = 64_000
    max_line_bytes: int = 4_000_000
    models_cache_ttl_seconds: int = 300
    strip_ansi: bool = True
    heartbeat_seconds: float = 15.0

    @field_validator("max_concurrency")
    @classmethod
    def _at_least_one(cls, value: int) -> int:
        if value < 1:
            raise ValueError("MAX_CONCURRENCY must be >= 1")
        return value

    @field_validator("default_timeout_seconds", "max_timeout_seconds", "timeout_grace_seconds")
    @classmethod
    def _positive_timeout(cls, value: int) -> int:
        if value < 1:
            raise ValueError("timeouts must be >= 1 second")
        return value

    @field_validator("queue_wait_seconds", "heartbeat_seconds")
    @classmethod
    def _non_negative_float(cls, value: float) -> float:
        if value < 0:
            raise ValueError("durations must be >= 0")
        return value

    @property
    def token_path(self) -> Path:
        """Fallback credential file agy writes when no keyring is available."""
        return self.agy_home / self.agy_token_filename

    @property
    def auth_required(self) -> bool:
        return bool(self.api_bearer_token.strip())

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    def resolved_ws_root(self) -> Path:
        return self.workspace_root.expanduser().resolve()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
