"""Non-secret configuration.

Process settings come from environment variables (`CONTAVEX_*`). Accounts and
consumers come from a TOML file that names secrets but never contains them.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

_ID = r"^[a-z0-9][a-z0-9_-]{0,62}$"


class ConfigError(Exception):
    pass


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    data_dir: Path = Path("/data")
    config_file: Path = Path("/config/contavex.toml")
    secrets_dir: Path = Path("/run/secrets")
    host: str = "0.0.0.0"  # noqa: S104 - inside a container; never published to the host
    port: int = Field(default=8080, ge=1, le=65535)
    log_level: str = "INFO"
    max_upload_bytes: int = Field(default=64 * 1024 * 1024, gt=0)
    media_retention_seconds: int = Field(default=48 * 3600, gt=0)
    # A publication that has not reached a terminal state by then fails as a timeout.
    publication_deadline_seconds: int = Field(default=6 * 3600, gt=0)
    max_attempts: int = Field(default=5, ge=1)
    worker_poll_seconds: float = Field(default=1.0, gt=0)
    # Upper bound for one provider step, uploads included.
    provider_timeout_seconds: float = Field(default=120.0, gt=0)
    # Listener for the self-hosted media stager; the only port meant to be
    # reachable from the internet (through the operator's TLS proxy).
    public_port: int = Field(default=8081, ge=1, le=65535)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        values = {
            name: env[f"CONTAVEX_{name.upper()}"]
            for name in cls.model_fields
            if f"CONTAVEX_{name.upper()}" in env
        }
        try:
            return cls.model_validate(values)
        except ValidationError as exc:
            raise ConfigError(f"invalid CONTAVEX_* settings: {exc}") from None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "contavex.db"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"


class AccountConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=_ID)
    platform: str = Field(pattern=_ID)
    implementation: str = Field(pattern=_ID)
    # role -> secret name, e.g. {"token": "telegram_news_bot_token"}
    credentials: dict[str, str] = {}
    # Provider-specific, non-secret (channel id, account id, API version...).
    settings: dict[str, Any] = {}


class ConsumerConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=_ID)
    api_key: str  # a secret NAME


class StagingConfig(BaseModel):
    """How uploaded media is made publicly reachable for providers that fetch by URL."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy: str = Field(pattern=_ID)
    credentials: dict[str, str] = {}  # role -> secret name
    settings: dict[str, Any] = {}


class ServiceConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # Secret NAME of the key that encrypts refreshed credentials at rest.
    master_key: str | None = None


class ContavexConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    accounts: dict[str, AccountConfig]
    consumers: dict[str, ConsumerConfig]
    service: ServiceConfig = ServiceConfig()
    staging: StagingConfig | None = None

    @classmethod
    def load(cls, path: Path) -> ContavexConfig:
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ConfigError(f"config file not found: {path}") from None
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"config file {path} is not valid TOML: {exc}") from None
        return cls.parse(raw)

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> ContavexConfig:
        unknown = set(raw) - {"accounts", "consumers", "service", "staging"}
        if unknown:
            raise ConfigError(f"unknown top-level config sections: {sorted(unknown)}")
        try:
            accounts = {
                key: AccountConfig(id=key, **value)
                for key, value in raw.get("accounts", {}).items()
            }
            consumers = {
                key: ConsumerConfig(id=key, **value)
                for key, value in raw.get("consumers", {}).items()
            }
            service = ServiceConfig(**raw.get("service", {}))
            staging = StagingConfig(**raw["staging"]) if "staging" in raw else None
        except (ValidationError, TypeError) as exc:
            raise ConfigError(f"invalid contavex config: {exc}") from None
        if not consumers:
            raise ConfigError("at least one [consumers.<id>] entry is required")
        return cls(accounts=accounts, consumers=consumers, service=service, staging=staging)
