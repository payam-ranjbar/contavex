from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2
import pytest
import structlog

from content_fleet.api.app import create_app
from content_fleet.config import AccountConfig, FleetConfig, Settings
from content_fleet.core.capabilities import Capabilities, KindCapability, MediaTypeCapability
from content_fleet.core.models import (
    MediaSource,
    MediaType,
    PublicationRequest,
    PublicationResult,
    PublishedPost,
)
from content_fleet.providers.base import Done, Provider, ProviderContext, StepOutcome
from content_fleet.providers.registry import ProviderRegistry
from content_fleet.services import Services, build_services

CONSUMER_KEY = "test-consumer-key-0123456789abcdef"
OTHER_KEY = "other-consumer-key-0123456789abcdef"
# Shaped like a real bot token so pattern-based redaction is exercised too.
CANARY_TOKEN = "123456789:CANARYcanaryCANARYcanaryCANARYcanary"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _default_logging() -> Iterator[None]:
    """Logging configuration is process-wide; do not let one test's leak into the next."""
    yield
    structlog.reset_defaults()


class FakeClock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


Step = StepOutcome | Exception | Callable[[ProviderContext, dict[str, Any]], StepOutcome]


def published(post_id: str = "post-1") -> Done:
    return Done(PublicationResult(posts=[PublishedPost(id=post_id)]))


@dataclass
class ScriptedProvider(Provider):
    """A provider whose steps are scripted by the test."""

    script: list[Step] = field(default_factory=list)
    repeatable: bool = False
    reconciled: StepOutcome | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)
    reconcile_calls: int = 0
    platform: str = "fake"
    implementation: str = "scripted"

    def capabilities(self, account: AccountConfig) -> Capabilities:
        return Capabilities(
            kinds={
                "post": KindCapability(max_media=2, media_types=[MediaType.IMAGE, MediaType.VIDEO])
            },
            text_formats=["plain", "html"],
            max_text_length=100,
            max_caption_length=50,
            media={
                MediaType.IMAGE: MediaTypeCapability(
                    sources=[MediaSource.UPLOAD], max_bytes=1000, content_types=["image/jpeg"]
                ),
                MediaType.VIDEO: MediaTypeCapability(sources=[MediaSource.URL]),
            },
        )

    def validate_account(self, account: AccountConfig) -> None:
        if account.settings.get("broken"):
            raise ValueError("settings.broken is set")

    async def advance(
        self, ctx: ProviderContext, request: PublicationRequest, state: dict[str, Any]
    ) -> StepOutcome:
        self.calls.append(dict(state))
        step = self.script.pop(0) if self.script else published()
        if isinstance(step, Exception):
            raise step
        if callable(step):
            return step(ctx, state)
        return step

    def step_is_repeatable(self, request: PublicationRequest, state: dict[str, Any]) -> bool:
        return self.repeatable

    async def reconcile(
        self, ctx: ProviderContext, request: PublicationRequest, state: dict[str, Any]
    ) -> StepOutcome | None:
        self.reconcile_calls += 1
        return self.reconciled


@dataclass
class Harness:
    services: Services
    client: httpx2.AsyncClient
    provider: ScriptedProvider
    clock: FakeClock
    secrets_dir: Path

    def auth(self, key: str = CONSUMER_KEY) -> dict[str, str]:
        return {"authorization": f"Bearer {key}"}

    async def submit(
        self, body: dict[str, Any] | None = None, *, key: str = "key-1", **overrides: Any
    ) -> httpx2.Response:
        payload = {"account": "main", **(body or {}), **overrides}
        if "text" not in payload and "media" not in payload:
            payload["text"] = "hello"
        return await self.client.post(
            "/v1/publications", json=payload, headers={**self.auth(), "idempotency-key": key}
        )

    async def status(self, publication_id: str) -> dict[str, Any]:
        response = await self.client.get(f"/v1/publications/{publication_id}", headers=self.auth())
        assert response.status_code == 200, response.text
        return response.json()  # type: ignore[no-any-return]

    async def drain(self, limit: int = 50) -> int:
        """Run worker steps until nothing is due at the current fake time."""
        steps = 0
        while steps < limit and await self.services.worker.run_once():
            steps += 1
        return steps


def write_secrets(directory: Path, **values: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, value in values.items():
        (directory / name).write_text(value, encoding="utf-8")


def fleet_config(**account_settings: Any) -> FleetConfig:
    return FleetConfig.parse(
        {
            "consumers": {
                "tester": {"api_key": "consumer_key"},
                "other": {"api_key": "other_key"},
            },
            "accounts": {
                "main": {
                    "platform": "fake",
                    "implementation": "scripted",
                    "credentials": {"token": "fake_token"},
                    "settings": account_settings,
                }
            },
        }
    )


HarnessFactory = Callable[..., Awaitable[Harness]]


@pytest.fixture
async def harness_factory(tmp_path: Path) -> AsyncIterator[HarnessFactory]:
    """Build a running service around any config, registry and vendor transport."""
    async with AsyncExitStack() as stack:

        async def build(
            *,
            config: FleetConfig | None = None,
            registry: ProviderRegistry | None = None,
            transport: httpx2.AsyncBaseTransport | None = None,
            secrets: dict[str, str] | None = None,
            **settings: Any,
        ) -> Harness:
            secrets_dir = tmp_path / "secrets"
            write_secrets(
                secrets_dir,
                consumer_key=CONSUMER_KEY,
                other_key=OTHER_KEY,
                fake_token=CANARY_TOKEN,
                **(secrets or {}),
            )
            defaults: dict[str, Any] = {
                "max_upload_bytes": 2000,
                "max_attempts": 3,
                "publication_deadline_seconds": 3600,
            }
            resolved = Settings(
                data_dir=tmp_path / "data", secrets_dir=secrets_dir, **(defaults | settings)
            )
            provider = ScriptedProvider()
            clock = FakeClock()
            services = build_services(
                resolved,
                config=config or fleet_config(),
                registry=registry or ProviderRegistry([provider]),
                transport=transport,
                clock=clock,
            )
            stack.push_async_callback(services.aclose)
            client = await stack.enter_async_context(
                httpx2.AsyncClient(
                    transport=httpx2.ASGITransport(app=create_app(services, start_worker=False)),
                    base_url="http://fleet",
                )
            )
            return Harness(services, client, provider, clock, secrets_dir)

        yield build


@pytest.fixture
async def harness(harness_factory: HarnessFactory) -> Harness:
    return await harness_factory()
