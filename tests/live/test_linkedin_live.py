"""Live test against the real LinkedIn API: posts to connections only, edits,
then deletes. It also settles an open research question: whether a
self-serve "Share on LinkedIn" app may call the versioned /rest/posts API.

    $env:CF_LIVE_TESTS = "1"
    $env:CF_LIVE_LINKEDIN_TOKEN_FILE = "secrets\\linkedin_founder_access_token"
    $env:CF_LIVE_LINKEDIN_AUTHOR = "urn:li:person:..."     # or urn:li:organization:...
    uv run pytest -m live tests/live/test_linkedin_live.py -s
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest

from content_fleet.config import FleetConfig
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
    token_file = os.environ.get("CF_LIVE_LINKEDIN_TOKEN_FILE")
    author = os.environ.get("CF_LIVE_LINKEDIN_AUTHOR")
    if not token_file or not author:
        pytest.skip("set CF_LIVE_LINKEDIN_TOKEN_FILE and CF_LIVE_LINKEDIN_AUTHOR")
    config = FleetConfig.parse(
        {
            "consumers": {"tester": {"api_key": "consumer_key"}},
            "service": {"master_key": "master_key"},
            "accounts": {
                "main": {
                    "platform": "linkedin",
                    "implementation": "rest",
                    "credentials": {"access_token": "li_token"},
                    "settings": {"author": author},
                }
            },
        }
    )
    return await harness_factory(
        config=config,
        secrets={
            "li_token": _read(token_file),
            "master_key": base64.b64encode(os.urandom(32)).decode(),
        },
    )


async def test_publish_edit_delete_round_trip(h: Harness) -> None:
    response = await h.submit(
        text="content-fleet live test (will be deleted) #test",
        options={"linkedin": {"visibility": "CONNECTIONS"}},
        key="live-1",
    )
    assert response.status_code == 202, response.text
    for _ in range(5):
        await h.drain()
        h.clock.advance(5)
    publication = await h.status(response.json()["id"])
    # 403 here means this app may not use /rest/posts; see docs/INTEGRATIONS/linkedin.md.
    assert publication["status"] == "published", publication
    print("published:", publication["result"])

    url = f"/v1/publications/{publication['id']}"
    edited = await h.client.patch(url, json={"text": "edited (still a test)"}, headers=h.auth())
    assert edited.status_code == 200, edited.text
    deleted = await h.client.delete(url, headers=h.auth())
    assert deleted.status_code == 200, deleted.text
