"""Wires the service together. Fails fast on anything misconfigured."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx2

from contavex.config import ConfigError, ContavexConfig, Settings
from contavex.credentials import CredentialStore
from contavex.media import MediaStore
from contavex.observability import Metrics
from contavex.providers.registry import ProviderRegistry, default_registry
from contavex.secrets import FileSecretResolver, Secret, SecretResolver
from contavex.staging import Stager
from contavex.staging.registry import build_stager
from contavex.store.db import Database
from contavex.worker import Worker


@dataclass
class Services:
    settings: Settings
    config: ContavexConfig
    db: Database
    media: MediaStore
    registry: ProviderRegistry
    secrets: SecretResolver
    consumer_keys: dict[str, Secret]
    http: httpx2.AsyncClient
    metrics: Metrics
    worker: Worker
    clock: Callable[[], float]
    credentials: CredentialStore
    stager: Stager | None

    async def aclose(self) -> None:
        await self.worker.stop()
        await self.http.aclose()
        self.db.close()


def build_services(
    settings: Settings,
    *,
    config: ContavexConfig | None = None,
    registry: ProviderRegistry | None = None,
    secrets: SecretResolver | None = None,
    transport: httpx2.AsyncBaseTransport | None = None,
    clock: Callable[[], float] = time.time,
    stager: Stager | None = None,
) -> Services:
    config = config or ContavexConfig.load(settings.config_file)
    secrets = secrets or FileSecretResolver(settings.secrets_dir)
    registry = registry or default_registry(staging_available=config.staging is not None)

    # Every problem an operator can cause is reported at startup, not on the
    # first publish: unknown providers, bad account settings, missing secrets.
    for account in config.accounts.values():
        provider = registry.get(account.platform, account.implementation)
        if provider is None:
            raise ConfigError(
                f"account {account.id!r}: no provider for platform {account.platform!r} "
                f"implementation {account.implementation!r}; available: {registry.keys()}"
            )
        try:
            provider.validate_account(account)
        except ValueError as exc:
            raise ConfigError(f"account {account.id!r}: {exc}") from None
        for name in account.credentials.values():
            secrets.get(name)
        if provider.mutable_credentials and config.service.master_key is None:
            raise ConfigError(
                f"account {account.id!r}: its credentials are refreshed at runtime and must "
                "be stored encrypted; set [service] master_key to the name of a secret "
                "holding 32 random bytes, base64-encoded"
            )
    master_key = secrets.get(config.service.master_key) if config.service.master_key else None
    consumer_keys = {
        consumer.id: secrets.get(consumer.api_key) for consumer in config.consumers.values()
    }

    db = Database(settings.db_path)
    try:
        return _assemble(
            settings,
            config,
            registry,
            secrets,
            consumer_keys,
            master_key,
            db,
            transport,
            clock,
            stager,
        )
    except BaseException:
        # Startup failed after the database was opened; do not leave it open.
        db.close()
        raise


def _assemble(
    settings: Settings,
    config: ContavexConfig,
    registry: ProviderRegistry,
    secrets: SecretResolver,
    consumer_keys: dict[str, Secret],
    master_key: Secret | None,
    db: Database,
    transport: httpx2.AsyncBaseTransport | None,
    clock: Callable[[], float],
    stager: Stager | None,
) -> Services:
    media = MediaStore(
        db,
        settings.media_dir,
        max_bytes=settings.max_upload_bytes,
        retention_seconds=settings.media_retention_seconds,
        clock=clock,
    )
    # No redirects: a provider API has no reason to redirect, and following
    # one could carry a credential to another host.
    http = httpx2.AsyncClient(
        timeout=httpx2.Timeout(settings.provider_timeout_seconds),
        follow_redirects=False,
        transport=transport,
    )
    metrics = Metrics()
    credentials = CredentialStore(db, secrets, clock, master_key)
    if stager is None:
        stager = build_stager(
            config.staging, db=db, media=media, http=http, secrets=secrets, clock=clock
        )
    worker = Worker(
        settings=settings,
        config=config,
        db=db,
        registry=registry,
        credentials=credentials,
        http=http,
        media=media,
        metrics=metrics,
        clock=clock,
        stager=stager,
    )
    return Services(
        settings=settings,
        config=config,
        db=db,
        media=media,
        registry=registry,
        secrets=secrets,
        consumer_keys=consumer_keys,
        http=http,
        metrics=metrics,
        worker=worker,
        clock=clock,
        credentials=credentials,
        stager=stager,
    )
