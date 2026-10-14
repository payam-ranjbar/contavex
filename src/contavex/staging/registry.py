"""Which stager serves which `[staging] strategy`. The counterpart of the
provider registry: the only place that names the concrete stagers."""

from __future__ import annotations

from collections.abc import Callable

import httpx2

from contavex.config import ConfigError, StagingConfig
from contavex.media import MediaStore
from contavex.secrets import SecretResolver
from contavex.staging import Stager
from contavex.staging.object_storage import ObjectStorageStager
from contavex.staging.self_hosted import SelfHostedStager
from contavex.store.db import Database

STRATEGIES = ("self-hosted", "object-storage")


def build_stager(
    config: StagingConfig | None,
    *,
    db: Database,
    media: MediaStore,
    http: httpx2.AsyncClient,
    secrets: SecretResolver,
    clock: Callable[[], float],
) -> Stager | None:
    if config is None:
        return None
    try:
        if config.strategy == "self-hosted":
            if config.credentials:
                raise ValueError("the self-hosted stager takes no credentials")
            return SelfHostedStager(db, media, clock, config.settings)
        if config.strategy == "object-storage":
            credentials = {role: secrets.get(name) for role, name in config.credentials.items()}
            return ObjectStorageStager(http, media, clock, config.settings, credentials)
    except ValueError as exc:
        raise ConfigError(f"[staging]: {exc}") from None
    raise ConfigError(
        f"[staging]: unknown strategy {config.strategy!r}; available: {list(STRATEGIES)}"
    )
