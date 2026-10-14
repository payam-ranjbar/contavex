"""Live test against the real Instagram API. It publishes a REAL post that
this API cannot delete; remove it by hand afterwards. Use a throw-away
professional account.

    $env:CONTAVEX_LIVE_TESTS = "1"
    $env:CONTAVEX_LIVE_INSTAGRAM_TOKEN_FILE = "secrets\\instagram_brand_access_token"
    $env:CONTAVEX_LIVE_INSTAGRAM_USER_ID = "1784..."
    $env:CONTAVEX_LIVE_INSTAGRAM_IMAGE_URL = "https://.../public.jpg"   # a public JPEG
    uv run pytest -m live tests/live/test_instagram_live.py -s
"""

from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path

import pytest

from contavex.config import ContavexConfig
from tests.conftest import Harness, HarnessFactory

pytestmark = [
    pytest.mark.live,
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.environ.get("CONTAVEX_LIVE_TESTS") != "1", reason="live tests need CONTAVEX_LIVE_TESTS=1"
    ),
]


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


@pytest.fixture
async def h(harness_factory: HarnessFactory) -> Harness:
    token_file = os.environ.get("CONTAVEX_LIVE_INSTAGRAM_TOKEN_FILE")
    user_id = os.environ.get("CONTAVEX_LIVE_INSTAGRAM_USER_ID")
    if not token_file or not user_id or not os.environ.get("CONTAVEX_LIVE_INSTAGRAM_IMAGE_URL"):
        pytest.skip("set CONTAVEX_LIVE_INSTAGRAM_TOKEN_FILE, _USER_ID and _IMAGE_URL")
    settings = {"ig_user_id": user_id}
    if version := os.environ.get("CONTAVEX_LIVE_INSTAGRAM_API_VERSION"):
        settings["api_version"] = version  # e.g. v26.0, to settle which versions are served
    config = ContavexConfig.parse(
        {
            "consumers": {"tester": {"api_key": "consumer_key"}},
            "service": {"master_key": "master_key"},
            "accounts": {
                "main": {
                    "platform": "instagram",
                    "implementation": "instagram-login",
                    "credentials": {"access_token": "ig_token"},
                    "settings": settings,
                }
            },
        }
    )
    return await harness_factory(
        config=config,
        secrets={
            "ig_token": _read(token_file),
            "master_key": base64.b64encode(os.urandom(32)).decode(),
        },
    )


async def test_publish_an_image(h: Harness) -> None:
    response = await h.submit(
        text="contavex live test (delete me)",
        media=[{"type": "image", "url": os.environ["CONTAVEX_LIVE_INSTAGRAM_IMAGE_URL"]}],
        key="live-1",
    )
    assert response.status_code == 202, response.text
    publication: dict[str, object] = {}
    for _ in range(60):  # real time: Meta needs a moment to fetch and process
        await h.drain()
        h.clock.advance(5)
        publication = await h.status(response.json()["id"])
        if publication["next_attempt_at"] is None:
            break
        await asyncio.sleep(2)
    assert publication["status"] == "published", publication
    print("published:", publication["result"])
