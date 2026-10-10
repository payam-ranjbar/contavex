"""Which stager serves which `[staging] strategy`. The counterpart of the
provider registry: the only place that names the concrete stagers."""

from __future__ import annotations

from collections.abc import Callable

import httpx2

from content_fleet.config import ConfigError, StagingConfig
from content_fleet.media import MediaStore
from content_fleet.secrets import SecretResolver
from content_fleet.staging import Stager
from content_fleet.staging.object_storage import ObjectStorageStager
from content_fleet.staging.self_hosted import SelfHostedStager
from content_fleet.store.db import Database

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
