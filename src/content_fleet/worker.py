"""Drives publications through their provider steps.

Retries are state transitions on the publication (`attempts`, `run_after`),
not in-memory loops, so they survive a restart and are visible to the
caller. The policy lives here; the classification of each failure lives in
the provider that produced it.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable
from typing import Any

import httpx2
import structlog

from content_fleet.config import AccountConfig, FleetConfig, Settings
from content_fleet.core.errors import ErrorCategory, PublishError
from content_fleet.core.models import ErrorInfo, PublicationStatus
from content_fleet.media import MediaStore
from content_fleet.observability import Metrics, log
from content_fleet.providers.base import (
    Continue,
    Done,
    ProviderContext,
    StepOutcome,
    Unknown,
)
from content_fleet.providers.registry import ProviderRegistry
from content_fleet.secrets import Secret, SecretError, SecretResolver, redactor
from content_fleet.staging import Stager
from content_fleet.store.db import Database, PublicationRecord

BACKOFF_BASE_SECONDS = 2.0
BACKOFF_CAP_SECONDS = 15 * 60.0
# Longer than any single provider step may take, so a live step is never
# mistaken for an interrupted one.
LEASE_MARGIN_SECONDS = 60.0
MEDIA_PURGE_INTERVAL_SECONDS = 600.0


def backoff_seconds(attempt: int, retry_after: float | None, rng: random.Random) -> float:
    """Delay before retry number `attempt` (1-based).

    A provider's own hint always wins: it knows when its limit resets.
    Otherwise exponential with jitter, so retries do not synchronise.
    """
    if retry_after is not None:
        return max(retry_after, 0.0) + rng.uniform(0, 1)
    ceiling = min(BACKOFF_BASE_SECONDS * 2 ** (attempt - 1), BACKOFF_CAP_SECONDS)
    return rng.uniform(ceiling / 2, ceiling)


def _error_info(exc: PublishError, *, retryable: bool) -> ErrorInfo:
    return ErrorInfo(
        category=exc.category,
        retryable=retryable,
        # Stored and returned to consumers, so scrubbed here and not only in logs.
        message=redactor.scrub(exc.message),
        provider_code=exc.provider_code,
        provider_request_id=exc.provider_request_id,
    )


class Worker:
    def __init__(
        self,
        *,
        settings: Settings,
        config: FleetConfig,
        db: Database,
        registry: ProviderRegistry,
        secrets: SecretResolver,
        http: httpx2.AsyncClient,
        media: MediaStore,
        metrics: Metrics,
        clock: Callable[[], float],
        stager: Stager | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._settings = settings
        self._config = config
        self._db = db
        self._registry = registry
        self._secrets = secrets
        self._http = http
        self._media = media
        self._metrics = metrics
        self._clock = clock
        self._stager = stager
        self._rng = rng or random.Random()  # noqa: S311 - jitter, not security
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._last_purge = 0.0

    # ---------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="content-fleet-worker")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def wake(self) -> None:
        self._wake.set()

    async def _run(self) -> None:
        while True:
            try:
                worked = await self.run_once()
                self._purge_media()
            except asyncio.CancelledError:
                raise
            except Exception:
                # The loop must outlive any single failure.
                log.exception("worker_loop_error")
                worked = False
            if worked:
                continue
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), self._settings.worker_poll_seconds)
            except TimeoutError:
                pass

    def _purge_media(self) -> None:
        now = self._clock()
        if now - self._last_purge >= MEDIA_PURGE_INTERVAL_SECONDS:
            self._last_purge = now
            removed = self._media.purge_expired()
            if removed:
                log.info("media_purged", count=removed)

    # ---------------------------------------------------------------- provider access

    def _context(
        self, account: AccountConfig, credentials: dict[str, Secret], publication_id: str
    ) -> ProviderContext:
        return ProviderContext(
            account=account,
            credentials=credentials,
            http=self._http,
            media=self._media,
            publication_id=publication_id,
            checkpoint=lambda state: self._db.checkpoint(publication_id, state),
            stager=self._stager,
        )

    def context_for(self, account: AccountConfig, publication_id: str) -> ProviderContext:
        """A provider context for work done outside the step loop (edit, delete)."""
        try:
            credentials = {
                role: self._secrets.get(name) for role, name in account.credentials.items()
            }
        except SecretError as exc:
            raise PublishError(ErrorCategory.AUTHENTICATION, str(exc)) from None
        return self._context(account, credentials, publication_id)

    # ---------------------------------------------------------------- one step

    async def run_once(self) -> bool:
        """Run one step of one due publication. Returns False when nothing was due."""
        lease = self._settings.provider_timeout_seconds + LEASE_MARGIN_SECONDS
        claimed = self._db.claim_due(self._clock(), lease)
        if claimed is None:
            return False
        record, interrupted = claimed
        with structlog.contextvars.bound_contextvars(
            publication_id=record.id,
            account=record.account,
            platform=record.platform,
            implementation=record.implementation,
        ):
            await self._step(record, interrupted)
        return True

    async def _step(self, record: PublicationRecord, interrupted: bool) -> None:
        if self._clock() > record.deadline:
            self._fail(
                record,
                PublishError(
                    ErrorCategory.TIMEOUT,
                    "the publication did not complete before its deadline",
                ),
            )
            return

        provider = self._registry.get(record.platform, record.implementation)
        account = self._config.accounts.get(record.account)
        if provider is None or account is None:
            self._fail(
                record,
                PublishError(
                    ErrorCategory.INTERNAL,
                    "the account or its provider is no longer configured",
                ),
            )
            return

        try:
            credentials = {
                role: self._secrets.get(name) for role, name in account.credentials.items()
            }
        except SecretError as exc:
            self._fail(record, PublishError(ErrorCategory.AUTHENTICATION, str(exc)))
            return

        ctx = self._context(account, credentials, record.id)

        started = time.perf_counter()
        try:
            outcome: StepOutcome | None
            if interrupted and not provider.step_is_repeatable(record.request, record.state):
                # The process died while a call that must not be repeated was
                # in flight. Ask the provider; never guess.
                log.warning("step_interrupted")
                outcome = await provider.reconcile(ctx, record.request, record.state)
                if outcome is None:
                    outcome = Unknown(
                        "the service restarted while the post was being created and the "
                        "provider offers no way to check whether it exists"
                    )
            else:
                try:
                    # A bound on the whole step, not per network operation:
                    # httpx timeouts restart with every chunk of an upload.
                    async with asyncio.timeout(self._settings.provider_timeout_seconds):
                        outcome = await provider.advance(ctx, record.request, record.state)
                except TimeoutError:
                    log.warning("step_timed_out")
                    if provider.step_is_repeatable(record.request, record.state):
                        raise PublishError(
                            ErrorCategory.TIMEOUT, "the provider step exceeded its time limit"
                        ) from None
                    # Cancelled mid-call: the post may exist.
                    outcome = await provider.reconcile(ctx, record.request, record.state)
                    if outcome is None:
                        outcome = Unknown(
                            "the provider did not answer in time and offers no way to "
                            "check whether the post exists"
                        )
        except PublishError as exc:
            self._handle_error(record, exc)
            return
        except Exception as exc:
            log.exception("provider_step_crashed")
            self._fail(
                record,
                PublishError(
                    ErrorCategory.INTERNAL,
                    f"unexpected {type(exc).__name__} in the provider implementation",
                ),
            )
            return
        finally:
            self._metrics.step_seconds.labels(record.platform, record.implementation).observe(
                time.perf_counter() - started
            )

        self._handle_outcome(record, outcome)

    # ---------------------------------------------------------------- transitions

    def _handle_outcome(self, record: PublicationRecord, outcome: StepOutcome) -> None:
        now = self._clock()
        if isinstance(outcome, Done):
            self._db.finish(
                record.id, now=now, status=PublicationStatus.PUBLISHED, result=outcome.result
            )
            self._finished(record, PublicationStatus.PUBLISHED, "")
            log.info("published", posts=[post.id for post in outcome.result.posts])
        elif isinstance(outcome, Continue):
            # Progress resets the attempt counter: it counts consecutive
            # failures of the current step, not polls.
            self._db.save_progress(
                record.id,
                now=now,
                state=outcome.state,
                run_after=now + max(outcome.delay, 0.0),
                attempts=0,
                error=None,
            )
            log.info("step_continue", delay=outcome.delay)
        else:
            assert isinstance(outcome, Unknown)
            self._db.finish(
                record.id,
                now=now,
                status=PublicationStatus.OUTCOME_UNKNOWN,
                state=outcome.state or None,
                error=ErrorInfo(
                    category=ErrorCategory.TIMEOUT, retryable=False, message=outcome.detail
                ),
            )
            self._finished(record, PublicationStatus.OUTCOME_UNKNOWN, ErrorCategory.TIMEOUT)
            log.warning("outcome_unknown", detail=outcome.detail)

    def _handle_error(self, record: PublicationRecord, exc: PublishError) -> None:
        attempt = record.attempts + 1
        if not exc.retryable or attempt >= self._settings.max_attempts:
            self._fail(record, exc)
            return
        now = self._clock()
        delay = backoff_seconds(attempt, exc.retry_after, self._rng)
        if now + delay > record.deadline:
            self._fail(record, exc)
            return
        self._db.save_progress(
            record.id,
            now=now,
            state=record.state,
            run_after=now + delay,
            attempts=attempt,
            error=_error_info(exc, retryable=True),
        )
        self._metrics.retries.labels(record.platform, exc.category).inc()
        log.warning(
            "step_retry_scheduled",
            category=exc.category,
            attempt=attempt,
            delay=round(delay, 2),
            provider_code=exc.provider_code,
            provider_request_id=exc.provider_request_id,
            error=exc.message,
        )

    def _fail(self, record: PublicationRecord, exc: PublishError) -> None:
        self._db.finish(
            record.id,
            now=self._clock(),
            status=PublicationStatus.FAILED,
            error=_error_info(exc, retryable=exc.retryable),
        )
        self._finished(record, PublicationStatus.FAILED, exc.category)
        log.error(
            "publication_failed",
            category=exc.category,
            retryable=exc.retryable,
            provider_code=exc.provider_code,
            provider_request_id=exc.provider_request_id,
            error=exc.message,
        )

    def _finished(
        self, record: PublicationRecord, status: PublicationStatus, category: Any
    ) -> None:
        self._metrics.finished.labels(record.platform, status, str(category)).inc()
