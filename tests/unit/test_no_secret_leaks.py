"""Canary test: a credential must not reach logs, responses or the database,
even when a provider misbehaves and puts it in an error message."""

from __future__ import annotations

import json
import sqlite3

import pytest

from content_fleet.core.errors import ErrorCategory, PublishError
from content_fleet.observability import configure_logging
from tests.conftest import CANARY_TOKEN, CONSUMER_KEY, Harness

pytestmark = [pytest.mark.unit, pytest.mark.anyio]


async def test_canary_credentials_never_leak(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("DEBUG")
    leaky_url = f"https://api.telegram.org/bot{CANARY_TOKEN}/sendMessage"
    harness.provider.script = [
        PublishError(ErrorCategory.PROVIDER_UNAVAILABLE, f"502 from {leaky_url}"),
        RuntimeError(f"connect failed: {leaky_url}"),
    ]

    responses = [await harness.submit(key="a"), await harness.submit(key="b")]
    for _ in range(4):
        await harness.drain()
        harness.clock.advance(60)
    for response in list(responses):
        responses.append(
            await harness.client.get(
                f"/v1/publications/{response.json()['id']}", headers=harness.auth()
            )
        )
    responses.append(await harness.client.get("/v1/accounts", headers=harness.auth()))
    responses.append(await harness.client.get("/readyz"))
    responses.append(await harness.client.get("/metrics"))

    logs = capsys.readouterr()
    events = [json.loads(line)["event"] for line in logs.out.splitlines() if line.startswith("{")]
    # The failure paths really ran and really logged.
    assert {"step_retry_scheduled", "provider_step_crashed", "publication_failed"} <= set(events)

    database = sqlite3.connect(harness.services.settings.db_path)
    stored = "\n".join(str(row) for row in database.execute("SELECT * FROM publications"))
    database.close()

    haystacks = {
        "logs": logs.out + logs.err,
        "responses": "\n".join(r.text + str(r.headers) for r in responses),
        "database": stored,
    }
    for where, text in haystacks.items():
        assert CANARY_TOKEN not in text, f"provider credential leaked into {where}"
        assert CONSUMER_KEY not in text, f"consumer key leaked into {where}"
