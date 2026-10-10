"""The publication lifecycle: what the worker does with each provider outcome."""

from __future__ import annotations

import asyncio
import random
from typing import Any

import pytest

from content_fleet.core.errors import ErrorCategory, PublishError
from content_fleet.providers.base import Continue, ProviderContext, StepOutcome, Unknown
from content_fleet.worker import BACKOFF_CAP_SECONDS, backoff_seconds
from tests.conftest import Harness, HarnessFactory, published

pytestmark = [pytest.mark.unit, pytest.mark.anyio]


async def submit(h: Harness) -> str:
    response = await h.submit()
    assert response.status_code == 202, response.text
    return response.json()["id"]  # type: ignore[no-any-return]


async def test_one_step_provider_publishes(harness: Harness) -> None:
    harness.provider.script = [published("msg-42")]
    publication_id = await submit(harness)
    assert await harness.drain() == 1
    status = await harness.status(publication_id)
    assert status["status"] == "published"
    assert status["result"] == {"posts": [{"id": "msg-42", "url": None}]}
    assert status["error"] is None and status["next_attempt_at"] is None


async def test_multi_step_provider_is_resumed_with_its_own_state(harness: Harness) -> None:
    harness.provider.script = [
        Continue({"container": "c1"}, delay=30),
        Continue({"container": "c1", "polls": 1}, delay=30),
        published(),
    ]
    publication_id = await submit(harness)

    assert await harness.drain() == 1  # second step is not due for 30 s
    status = await harness.status(publication_id)
    assert status["status"] == "processing"
    assert status["attempts"] == 0

    harness.clock.advance(30)
    assert await harness.drain() == 1
    harness.clock.advance(30)
    assert await harness.drain() == 1
    assert (await harness.status(publication_id))["status"] == "published"
    assert harness.provider.calls == [{}, {"container": "c1"}, {"container": "c1", "polls": 1}]


async def test_transient_error_is_retried_and_visible_meanwhile(harness: Harness) -> None:
    harness.provider.script = [
        PublishError(
            ErrorCategory.PROVIDER_UNAVAILABLE,
            "upstream 502",
            provider_code="502",
            provider_request_id="req-9",
        ),
        published(),
    ]
    publication_id = await submit(harness)
    await harness.drain()

    status = await harness.status(publication_id)
    assert status["status"] == "processing"
    assert status["attempts"] == 1
    assert status["next_attempt_at"] is not None
    assert status["error"] == {
        "category": "provider_unavailable",
        "retryable": True,
        "message": "upstream 502",
        "provider_code": "502",
        "provider_request_id": "req-9",
    }

    assert await harness.drain() == 0  # backing off
    harness.clock.advance(5)
    await harness.drain()
    status = await harness.status(publication_id)
    assert status["status"] == "published" and status["error"] is None


async def test_rate_limit_waits_for_the_providers_hint(harness: Harness) -> None:
    harness.provider.script = [
        PublishError(ErrorCategory.RATE_LIMITED, "too many requests", retry_after=120),
        published(),
    ]
    publication_id = await submit(harness)
    await harness.drain()

    harness.clock.advance(119)
    assert await harness.drain() == 0
    harness.clock.advance(3)
    assert await harness.drain() == 1
    assert (await harness.status(publication_id))["status"] == "published"


@pytest.mark.parametrize(
    "category",
    [
        ErrorCategory.AUTHENTICATION,
        ErrorCategory.AUTHORIZATION,
        ErrorCategory.INVALID_REQUEST,
        ErrorCategory.MEDIA_REJECTED,
        ErrorCategory.PROVIDER_REJECTED,
        ErrorCategory.UNSUPPORTED,
    ],
)
async def test_permanent_errors_fail_immediately(harness: Harness, category: ErrorCategory) -> None:
    harness.provider.script = [PublishError(category, "no"), published()]
    publication_id = await submit(harness)
    await harness.drain()
    harness.clock.advance(3600)
    await harness.drain()

    status = await harness.status(publication_id)
    assert status["status"] == "failed"
    assert status["error"]["category"] == category.value
    assert status["error"]["retryable"] is False
    assert len(harness.provider.calls) == 1


async def test_gives_up_after_max_attempts_and_says_it_is_retryable(harness: Harness) -> None:
    harness.provider.script = [
        PublishError(ErrorCategory.TIMEOUT, f"timed out #{n}") for n in range(10)
    ]
    publication_id = await submit(harness)
    for _ in range(10):
        await harness.drain()
        harness.clock.advance(60)

    status = await harness.status(publication_id)
    assert status["status"] == "failed"
    assert status["error"]["category"] == "timeout"
    assert status["error"]["retryable"] is True
    assert len(harness.provider.calls) == harness.services.settings.max_attempts


async def test_progress_resets_the_attempt_counter(harness: Harness) -> None:
    def flaky(n: int) -> PublishError:
        return PublishError(ErrorCategory.PROVIDER_UNAVAILABLE, f"flake {n}")

    # Two failures, progress, two more failures: never three in a row.
    harness.provider.script = [
        flaky(1),
        flaky(2),
        Continue({"s": 1}),
        flaky(3),
        flaky(4),
        published(),
    ]
    publication_id = await submit(harness)
    for _ in range(8):
        await harness.drain()
        harness.clock.advance(60)
    assert (await harness.status(publication_id))["status"] == "published"


async def test_ambiguous_outcome_is_reported_not_retried(harness: Harness) -> None:
    harness.provider.script = [Unknown("no response after the request was sent"), published()]
    publication_id = await submit(harness)
    await harness.drain()
    harness.clock.advance(3600)
    await harness.drain()

    status = await harness.status(publication_id)
    assert status["status"] == "outcome_unknown"
    assert status["error"]["message"] == "no response after the request was sent"
    assert status["next_attempt_at"] is None
    assert len(harness.provider.calls) == 1


async def test_deadline_fails_a_publication_that_never_completes(harness: Harness) -> None:
    harness.provider.script = [Continue({"n": n}, delay=600) for n in range(20)]
    publication_id = await submit(harness)
    for _ in range(10):
        await harness.drain()
        harness.clock.advance(600)

    status = await harness.status(publication_id)
    assert status["status"] == "failed"
    assert status["error"]["category"] == "timeout"
    assert len(harness.provider.calls) == 7  # ten-minute polls from 0:00 to the 1:00 deadline


async def test_a_bug_in_a_provider_fails_the_publication_not_the_worker(harness: Harness) -> None:
    harness.provider.script = [KeyError("oops")]
    first = await submit(harness)
    second = (await harness.submit(key="key-2")).json()["id"]
    await harness.drain()

    status = await harness.status(first)
    assert status["status"] == "failed"
    assert status["error"]["category"] == "internal"
    assert "oops" not in status["error"]["message"]
    assert (await harness.status(second))["status"] == "published"


class TestStepTimeout:
    """A step that outlives its time limit is cancelled mid-call."""

    @staticmethod
    async def _hang(ctx: ProviderContext, request: object, state: dict[str, Any]) -> StepOutcome:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    async def test_non_repeatable_step_becomes_outcome_unknown(
        self, harness_factory: HarnessFactory
    ) -> None:
        harness = await harness_factory(provider_timeout_seconds=0.05)
        harness.provider.advance = self._hang  # type: ignore[method-assign]
        publication_id = await submit(harness)
        await harness.drain()

        status = await harness.status(publication_id)
        assert status["status"] == "outcome_unknown"
        assert harness.provider.reconcile_calls == 1

    async def test_repeatable_step_is_retried(self, harness_factory: HarnessFactory) -> None:
        harness = await harness_factory(provider_timeout_seconds=0.05)
        harness.provider.advance = self._hang  # type: ignore[method-assign]
        harness.provider.repeatable = True
        publication_id = await submit(harness)
        await harness.drain()

        status = await harness.status(publication_id)
        assert status["status"] == "processing"
        assert status["error"]["category"] == "timeout"
        assert status["attempts"] == 1


class TestCrashRecovery:
    """The process died while a provider step was in flight."""

    async def _crash_mid_step(self, harness: Harness) -> str:
        publication_id = await submit(harness)
        claimed = harness.services.db.claim_due(harness.clock(), lease_seconds=90)
        assert claimed is not None and claimed[1] is False
        # ...the process dies here; on restart the lease runs out.
        assert await harness.drain() == 0
        harness.clock.advance(91)
        return publication_id

    async def test_non_repeatable_step_is_never_blindly_rerun(self, harness: Harness) -> None:
        publication_id = await self._crash_mid_step(harness)
        await harness.drain()

        status = await harness.status(publication_id)
        assert status["status"] == "outcome_unknown"
        assert harness.provider.calls == []
        assert harness.provider.reconcile_calls == 1

    async def test_provider_that_can_reconcile_settles_it(self, harness: Harness) -> None:
        harness.provider.reconciled = published("found-it")
        publication_id = await self._crash_mid_step(harness)
        await harness.drain()

        status = await harness.status(publication_id)
        assert status["status"] == "published"
        assert status["result"]["posts"][0]["id"] == "found-it"
        assert harness.provider.calls == []

    async def test_repeatable_step_is_simply_run_again(self, harness: Harness) -> None:
        harness.provider.repeatable = True
        publication_id = await self._crash_mid_step(harness)
        await harness.drain()

        assert (await harness.status(publication_id))["status"] == "published"
        assert len(harness.provider.calls) == 1
        assert harness.provider.reconcile_calls == 0

    async def test_checkpoint_survives_the_crash(self, harness: Harness) -> None:
        def step(ctx: ProviderContext, state: dict[str, Any]) -> StepOutcome:
            ctx.checkpoint({"container": "c-77"})
            raise SystemExit  # stand-in for the process dying after the checkpoint

        harness.provider.script = [step]
        publication_id = await submit(harness)
        with pytest.raises(SystemExit):
            await harness.drain()

        record = harness.services.db.get_publication(publication_id)
        assert record is not None and record.state == {"container": "c-77"}


class TestResolution:
    async def _unknown(self, harness: Harness) -> str:
        harness.provider.script = [Unknown("lost response")]
        publication_id = await submit(harness)
        await harness.drain()
        return publication_id

    async def test_confirmed_published(self, harness: Harness) -> None:
        publication_id = await self._unknown(harness)
        response = await harness.client.post(
            f"/v1/publications/{publication_id}/resolution",
            json={"published": True, "post_id": "777"},
            headers=harness.auth(),
        )
        assert response.status_code == 200
        assert response.json()["status"] == "published"
        assert response.json()["result"]["posts"] == [{"id": "777", "url": None}]

    async def test_confirmed_not_published_becomes_retryable_failure(
        self, harness: Harness
    ) -> None:
        publication_id = await self._unknown(harness)
        response = await harness.client.post(
            f"/v1/publications/{publication_id}/resolution",
            json={"published": False},
            headers=harness.auth(),
        )
        assert response.json()["status"] == "failed"
        assert response.json()["error"]["retryable"] is True
        # The service still never resends by itself.
        harness.clock.advance(3600)
        assert await harness.drain() == 0

    async def test_only_unknown_publications_can_be_resolved(self, harness: Harness) -> None:
        publication_id = await submit(harness)
        await harness.drain()
        response = await harness.client.post(
            f"/v1/publications/{publication_id}/resolution",
            json={"published": False},
            headers=harness.auth(),
        )
        assert response.status_code == 409


class TestBackoff:
    def test_grows_exponentially_with_jitter_up_to_a_cap(self) -> None:
        rng = random.Random(1)
        for attempt, ceiling in [(1, 2), (2, 4), (3, 8), (4, 16)]:
            delays = [backoff_seconds(attempt, None, rng) for _ in range(200)]
            assert ceiling / 2 <= min(delays) and max(delays) <= ceiling
            assert len({round(d, 3) for d in delays}) > 100
        assert backoff_seconds(40, None, rng) <= BACKOFF_CAP_SECONDS

    def test_provider_hint_is_a_floor(self) -> None:
        rng = random.Random(1)
        assert 300 <= backoff_seconds(1, 300, rng) <= 301
        assert 0 <= backoff_seconds(1, -5, rng) <= 1
