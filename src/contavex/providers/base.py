"""The boundary between the service and a vendor API.

A provider implementation publishes by *steps*. `advance` performs the next
step and says what happened; the worker persists the returned state and
calls again later. A one-call platform finishes on its first step; a
platform with server-side processing walks through several, with a
checkpoint between each. `advance` never sleeps or loops. See ARCHITECTURE D-4.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx2

from contavex.config import AccountConfig
from contavex.core.capabilities import Capabilities
from contavex.core.errors import ErrorCategory, PublishError
from contavex.core.models import PublicationRequest, PublicationResult
from contavex.credentials import CredentialState, RefreshedCredential
from contavex.media import MediaStore
from contavex.secrets import Secret
from contavex.staging import Stager


@dataclass(frozen=True)
class Done:
    """The post exists at the provider."""

    result: PublicationResult


@dataclass(frozen=True)
class Continue:
    """Progress was made (or the provider is still processing); call again."""

    state: dict[str, Any]
    delay: float = 0.0


@dataclass(frozen=True)
class Unknown:
    """The provider may or may not have created the post, and cannot tell us."""

    detail: str
    state: dict[str, Any] = field(default_factory=dict)


StepOutcome = Done | Continue | Unknown


@dataclass(frozen=True)
class ProviderContext:
    """Everything a provider may touch. It gets nothing else."""

    account: AccountConfig
    credentials: dict[str, Secret]
    http: httpx2.AsyncClient
    media: MediaStore
    publication_id: str
    # Persist state in the middle of a step, before a call that must not be
    # repeated, so the identifiers gathered so far survive a crash.
    checkpoint: Callable[[dict[str, Any]], None]
    stager: Stager | None = None


class Provider(ABC):
    """One implementation of one platform."""

    platform: str
    implementation: str
    # Credential roles this provider replaces at runtime (refreshed tokens).
    # Non-empty means the service needs a master key to store them.
    mutable_credentials: frozenset[str] = frozenset()

    @abstractmethod
    def capabilities(self, account: AccountConfig) -> Capabilities: ...

    def validate_account(self, account: AccountConfig) -> None:  # noqa: B027
        """Raise `ValueError` if the account's settings or credential roles are wrong."""

    def validate(self, account: AccountConfig, request: PublicationRequest) -> None:  # noqa: B027
        """Raise `PublishError(INVALID_REQUEST | UNSUPPORTED)` for rules that
        capabilities cannot express. Called before the request is accepted."""

    @abstractmethod
    async def advance(
        self, ctx: ProviderContext, request: PublicationRequest, state: dict[str, Any]
    ) -> StepOutcome:
        """Perform the next step. Raise `PublishError` for classified failures."""

    def step_is_repeatable(self, request: PublicationRequest, state: dict[str, Any]) -> bool:
        """Whether the step that `state` leads to can be run twice without
        risking a duplicate post. Consulted after a crash mid-step."""
        return False

    async def reconcile(
        self, ctx: ProviderContext, request: PublicationRequest, state: dict[str, Any]
    ) -> StepOutcome | None:
        """After an interrupted non-repeatable step, find out what happened.
        Return None when the provider offers no way to tell."""
        return None

    async def refresh_credentials(
        self, ctx: ProviderContext, states: dict[str, CredentialState], now: float
    ) -> list[RefreshedCredential]:
        """Called about hourly. Return replacements for credentials that are
        due for refresh; an empty list when nothing is due."""
        return []

    async def delete(self, ctx: ProviderContext, result: PublicationResult) -> None:
        raise PublishError(ErrorCategory.UNSUPPORTED, "this account cannot delete posts")

    async def edit(
        self,
        ctx: ProviderContext,
        request: PublicationRequest,
        result: PublicationResult,
        changes: dict[str, Any],
    ) -> None:
        raise PublishError(ErrorCategory.UNSUPPORTED, "this account cannot edit posts")
