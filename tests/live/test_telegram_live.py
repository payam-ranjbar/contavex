"""Live test against the real Telegram Bot API. It posts to a real channel.

Never runs by accident: it needs the `live` marker selected explicitly AND
the environment opt-in below.

    $env:CF_LIVE_TESTS = "1"
    $env:CF_LIVE_TELEGRAM_TOKEN_FILE = "secrets\\telegram_news_bot_token"
    $env:CF_LIVE_TELEGRAM_CHAT_ID = "-100..."
    uv run pytest -m live tests/live
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from content_fleet.config import FleetConfig
from content_fleet.providers.registry import default_registry
from tests.conftest import Harness, HarnessFactory

pytestmark = [
    pytest.mark.live,
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.environ.get("CF_LIVE_TESTS") != "1", reason="live tests need CF_LIVE_TESTS=1"
    ),
]


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


@pytest.fixture
async def h(harness_factory: HarnessFactory) -> Harness:
    token_file = os.environ.get("CF_LIVE_TELEGRAM_TOKEN_FILE")
    chat_id = os.environ.get("CF_LIVE_TELEGRAM_CHAT_ID")
    if not token_file or not chat_id:
        pytest.skip("set CF_LIVE_TELEGRAM_TOKEN_FILE and CF_LIVE_TELEGRAM_CHAT_ID")
    config = FleetConfig.parse(
        {
            "consumers": {"tester": {"api_key": "consumer_key"}},
            "accounts": {
                "main": {
                    "platform": "telegram",
                    "implementation": "bot-api",
                    "credentials": {"token": "telegram_token"},
                    "settings": {"chat_id": chat_id},
                }
            },
        }
    )
    return await harness_factory(
        config=config,
        registry=default_registry(),
        secrets={"telegram_token": _read(token_file)},
        max_upload_bytes=20 * 1024 * 1024,
    )


async def test_publish_edit_delete_round_trip(h: Harness) -> None:
    response = await h.submit(text="content-fleet live test (will be deleted)", key="live-1")
    assert response.status_code == 202, response.text
    await h.drain()
    publication = await h.status(response.json()["id"])
    assert publication["status"] == "published", publication
    print("published:", publication["result"]["posts"])

    await asyncio.sleep(1)  # stay under one message per second per chat
    edited = await h.client.patch(
        f"/v1/publications/{publication['id']}",
        json={"text": "content-fleet live test, <b>edited</b>", "text_format": "html"},
        headers=h.auth(),
    )
    # Settles the open research question: can a bot with only
    # can_post_messages edit its own channel posts?
    assert edited.status_code == 200, edited.text

    await asyncio.sleep(1)
    deleted = await h.client.delete(f"/v1/publications/{publication['id']}", headers=h.auth())
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["status"] == "deleted"
