"""The second Instagram implementation: "Instagram API with Facebook Login".

Same publishing flow as the Instagram Login variant, on graph.facebook.com,
plus the one thing that variant cannot do: delete a published post.
"""

from __future__ import annotations

from typing import Any

import httpx2
import pytest

from content_fleet.config import FleetConfig
from tests.conftest import Harness, HarnessFactory
from tests.fakes.instagram import IG_USER_ID, TOKEN, FakeInstagram, graph_error

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

IMAGE = {"type": "image", "url": "https://cdn.example.com/a.jpg"}
VIDEO = {"type": "video", "url": "https://cdn.example.com/a.mp4"}


def config(implementation: str = "facebook-login") -> FleetConfig:
    raw: dict[str, Any] = {
        "consumers": {"tester": {"api_key": "consumer_key"}},
        "accounts": {
            "main": {
                "platform": "instagram",
                "implementation": implementation,
                "credentials": {"access_token": "fb_token"},
                "settings": {"ig_user_id": IG_USER_ID},
            }
        },
    }
    return FleetConfig.parse(raw)


@pytest.fixture
def instagram() -> FakeInstagram:
    return FakeInstagram(host="graph.facebook.com")


@pytest.fixture
async def h(harness_factory: HarnessFactory, instagram: FakeInstagram) -> Harness:
    # No [service] master_key: this variant's token is not refreshed, so
    # nothing has to be stored encrypted.
    return await harness_factory(
        config=config(), transport=instagram.transport(), secrets={"fb_token": TOKEN}
    )


async def publish(h: Harness, **body: Any) -> dict[str, Any]:
    response = await h.submit(body)
    assert response.status_code == 202, response.text
    for _ in range(60):
        await h.drain()
        status = await h.status(response.json()["id"])
        if status["next_attempt_at"] is None:
            return status
        h.clock.advance(60)
    raise AssertionError("did not settle")


async def test_publishes_through_graph_facebook_com(h: Harness, instagram: FakeInstagram) -> None:
    publication = await publish(h, text="Hello", media=[IMAGE])
    assert publication["status"] == "published"
    assert instagram.operations() == [
        "create_container",
        "container_status",
        "publishing_limit",
        "media_publish",
        "get_media",
    ]


async def test_capabilities_differ_from_the_other_variant_only_in_delete(
    h: Harness, harness_factory: HarnessFactory
) -> None:
    caps = (await h.client.get("/v1/accounts/main", headers=h.auth())).json()["capabilities"]
    assert caps["deletable"] is True
    assert caps["editable"] == []  # no Instagram API can edit a published post
    assert set(caps["kinds"]) == {"post", "reel", "story"}


@pytest.mark.parametrize(
    "body",
    [
        {"text": "An image", "media": [IMAGE]},
        {"text": "A carousel", "media": [IMAGE, VIDEO, IMAGE]},
        {"kind": "reel", "text": "A reel", "media": [VIDEO]},
        {"kind": "story", "media": [IMAGE]},
    ],
    ids=["image", "carousel", "reel", "story"],
)
async def test_published_content_can_be_deleted(
    h: Harness, instagram: FakeInstagram, body: dict[str, Any]
) -> None:
    publication = await publish(h, **body)
    assert len(instagram.media) + len(instagram.stories) == 1
    url = f"/v1/publications/{publication['id']}"

    response = await h.client.delete(url, headers=h.auth())
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "deleted"
    assert response.json()["result"]["posts"]  # the record keeps what was published
    assert instagram.media == [] and instagram.stories == []
    # A carousel is removed with one call, through the carousel's own id.
    assert instagram.count("delete_media") == 1

    again = await h.client.delete(url, headers=h.auth())
    assert again.status_code == 200
    assert instagram.count("delete_media") == 1


@pytest.mark.parametrize(
    ("reply", "status", "category"),
    [
        (graph_error(190, "Token expired", subcode=463), 502, "authentication"),
        (graph_error(10, "Requires instagram_manage_contents", status=403), 502, "authorization"),
        (graph_error(-1, "Fatal", subcode=2207073), 502, "provider_rejected"),
        (graph_error(100, "Unsupported delete request", subcode=33), 422, None),
        (httpx2.ReadTimeout("no answer"), 503, "timeout"),
        (httpx2.Response(200, json={"success": False}), 503, "malformed_provider_response"),
    ],
    ids=["token", "permission", "fatal", "gone-or-unsupported", "timeout", "not-confirmed"],
)
async def test_failed_delete_leaves_the_publication_published(
    h: Harness,
    instagram: FakeInstagram,
    reply: httpx2.Response | Exception,
    status: int,
    category: str | None,
) -> None:
    publication = await publish(h, text="x", media=[IMAGE])
    instagram.plan("delete_media", reply)
    response = await h.client.delete(f"/v1/publications/{publication['id']}", headers=h.auth())

    assert response.status_code == status, response.text
    assert response.json().get("category") == category
    assert (await h.status(publication["id"]))["status"] == "published"
    assert len(instagram.media) == 1


async def test_token_is_never_refreshed(h: Harness, instagram: FakeInstagram) -> None:
    h.clock.advance(30 * 86400)
    await h.services.worker.maintain_credentials(force=True)
    assert instagram.calls == []
    account = (await h.client.get("/v1/accounts/main", headers=h.auth())).json()
    assert account["credential_status"] == "ok" and account["credential_expires_at"] is None


async def test_the_instagram_login_variant_answers_as_the_real_api_did(
    harness_factory: HarnessFactory,
) -> None:
    """Guards the reason this variant exists: on graph.instagram.com a delete
    is refused (observed live), and the service never even sends it."""
    instagram = FakeInstagram()
    h = await harness_factory(
        config=FleetConfig.parse(
            {
                "consumers": {"tester": {"api_key": "consumer_key"}},
                "service": {"master_key": "master_key"},
                "accounts": {
                    "main": {
                        "platform": "instagram",
                        "implementation": "instagram-login",
                        "credentials": {"access_token": "fb_token"},
                        "settings": {"ig_user_id": IG_USER_ID},
                    }
                },
            }
        ),
        transport=instagram.transport(),
        secrets={"fb_token": TOKEN, "master_key": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="},
    )
    publication = await publish(h, text="x", media=[IMAGE])
    response = await h.client.delete(f"/v1/publications/{publication['id']}", headers=h.auth())
    assert response.status_code == 422 and response.json()["capability"] == "delete"
    assert instagram.count("delete_media") == 0
