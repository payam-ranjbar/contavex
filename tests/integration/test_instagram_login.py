"""The Instagram (Instagram Login) implementation, end to end through the
service, against a faked graph.instagram.com."""

from __future__ import annotations

import base64
import json
import sqlite3
from typing import Any

import httpx2
import pytest

from contavex.config import ConfigError, ContavexConfig
from contavex.credentials import CredentialStore
from contavex.observability import configure_logging
from contavex.secrets import FileSecretResolver
from tests.conftest import Harness, HarnessFactory
from tests.fakes.instagram import (
    EXPIRES_IN,
    IG_USER_ID,
    REFRESHED_TOKEN,
    TOKEN,
    FakeInstagram,
    graph_error,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

MASTER_KEY = base64.b64encode(bytes(range(32))).decode()
JPEG = b"\xff\xd8\xff\xe0 not really a jpeg"
IMAGE = {"type": "image", "url": "https://cdn.example.com/a.jpg"}
VIDEO = {"type": "video", "url": "https://cdn.example.com/a.mp4"}
DAY = 86400


def instagram_config(*, staging: bool = True, master_key: bool = True) -> ContavexConfig:
    raw: dict[str, Any] = {
        "consumers": {"tester": {"api_key": "consumer_key"}, "other": {"api_key": "other_key"}},
        "accounts": {
            "main": {
                "platform": "instagram",
                "implementation": "instagram-login",
                "credentials": {"access_token": "ig_token"},
                "settings": {"ig_user_id": IG_USER_ID},
            }
        },
    }
    if master_key:
        raw["service"] = {"master_key": "master_key"}
    if staging:
        raw["staging"] = {
            "strategy": "self-hosted",
            "settings": {"public_base_url": "https://media.test"},
        }
    return ContavexConfig.parse(raw)


SECRETS = {"ig_token": TOKEN, "master_key": MASTER_KEY}


@pytest.fixture
def instagram() -> FakeInstagram:
    return FakeInstagram()


@pytest.fixture
async def h(harness_factory: HarnessFactory, instagram: FakeInstagram) -> Harness:
    return await harness_factory(
        config=instagram_config(),
        transport=instagram.transport(),
        secrets=SECRETS,
        max_upload_bytes=100_000,
    )


async def submit(h: Harness, **body: Any) -> str:
    response = await h.submit(body)
    assert response.status_code == 202, response.text
    return response.json()["id"]  # type: ignore[no-any-return]


async def settle(h: Harness, publication_id: str, *, rounds: int = 60) -> dict[str, Any]:
    """Run the worker through polls and backoffs until the publication is final."""
    for _ in range(rounds):
        await h.drain()
        status = await h.status(publication_id)
        if status["next_attempt_at"] is None:
            return status
        h.clock.advance(60)
    raise AssertionError(f"did not settle: {status}")


async def publish(h: Harness, **body: Any) -> dict[str, Any]:
    return await settle(h, await submit(h, **body))


async def state_of(h: Harness, publication_id: str) -> dict[str, Any]:
    record = h.services.db.get_publication(publication_id)
    assert record is not None
    return record.state


async def run_until_step(h: Harness, publication_id: str, step: str) -> None:
    for _ in range(40):
        if (await state_of(h, publication_id)).get("step") == step:
            return
        if not await h.services.worker.run_once():
            h.clock.advance(60)
    raise AssertionError(f"never reached step {step}")


class TestPublishing:
    async def test_image_goes_through_container_poll_publish(
        self, h: Harness, instagram: FakeInstagram
    ) -> None:
        publication_id = await submit(
            h, text="Launch day. #news", media=[IMAGE | {"alt_text": "Team photo"}]
        )
        assert instagram.calls == []  # accepted, nothing sent yet

        await h.services.worker.run_once()  # stage
        await h.services.worker.run_once()  # create container
        waiting = await h.status(publication_id)
        assert waiting["status"] == "processing"
        assert instagram.operations() == ["create_container"]

        publication = await settle(h, publication_id)
        assert publication["status"] == "published"
        (post,) = publication["result"]["posts"]
        assert post["url"] == f"https://www.instagram.com/p/{post['id']}/"
        assert instagram.operations() == [
            "create_container",
            "container_status",
            "publishing_limit",
            "media_publish",
            "get_media",
        ]
        assert instagram.bodies("create_container") == [
            {
                "image_url": "https://cdn.example.com/a.jpg",
                "alt_text": "Team photo",
                "caption": "Launch day. #news",
            }
        ]

    async def test_reel_waits_while_instagram_processes_the_video(
        self, h: Harness, instagram: FakeInstagram
    ) -> None:
        instagram.video_polls = 3
        publication_id = await submit(
            h,
            kind="reel",
            text="Watch this",
            media=[VIDEO],
            options={"instagram": {"share_to_feed": False, "thumb_offset_ms": 1500}},
        )
        await run_until_step(h, publication_id, "wait")
        for _ in range(2):
            h.clock.advance(60)
            await h.drain()
            assert (await h.status(publication_id))["status"] == "processing"
        assert instagram.count("media_publish") == 0

        publication = await settle(h, publication_id)
        assert publication["status"] == "published"
        assert instagram.count("container_status") == 3
        assert instagram.count("media_publish") == 1
        assert instagram.bodies("create_container") == [
            {
                "video_url": "https://cdn.example.com/a.mp4",
                "media_type": "REELS",
                "share_to_feed": False,
                "thumb_offset": 1500,
                "caption": "Watch this",
            }
        ]

    async def test_story(self, h: Harness, instagram: FakeInstagram) -> None:
        publication = await publish(h, kind="story", media=[IMAGE])
        assert publication["status"] == "published"
        assert instagram.bodies("create_container") == [
            {"image_url": "https://cdn.example.com/a.jpg", "media_type": "STORIES"}
        ]
        assert publication["result"]["posts"][0]["url"] is None  # stories have no permalink here

    async def test_carousel_builds_children_then_a_parent(
        self, h: Harness, instagram: FakeInstagram
    ) -> None:
        publication = await publish(
            h, text="Three things", media=[IMAGE | {"alt_text": "first"}, VIDEO, IMAGE]
        )
        assert publication["status"] == "published"
        assert len(publication["result"]["posts"]) == 1  # a carousel is one post

        children = instagram.bodies("create_container")[:3]
        assert children == [
            {
                "is_carousel_item": True,
                "image_url": "https://cdn.example.com/a.jpg",
                "alt_text": "first",
            },
            {
                "is_carousel_item": True,
                "media_type": "VIDEO",
                "video_url": "https://cdn.example.com/a.mp4",
            },
            {"is_carousel_item": True, "image_url": "https://cdn.example.com/a.jpg"},
        ]
        parent = instagram.bodies("create_container")[3]
        assert parent["media_type"] == "CAROUSEL"
        assert parent["caption"] == "Three things"
        assert len(parent["children"].split(",")) == 3
        assert instagram.count("media_publish") == 1
        # The parent is only created once the video child has finished processing.
        operations = instagram.operations()
        assert operations.index("container_status") < operations.index("media_publish")


class TestStagedUploads:
    async def test_uploaded_image_is_public_only_while_publishing(
        self, h: Harness, instagram: FakeInstagram
    ) -> None:
        upload = await h.client.post(
            "/v1/media?filename=été photo.jpg",
            content=JPEG,
            headers={**h.auth(), "content-type": "image/jpeg"},
        )
        media_id = upload.json()["id"]
        publication_id = await submit(h, text="x", media=[{"type": "image", "media_id": media_id}])
        await run_until_step(h, publication_id, "wait")

        url = instagram.bodies("create_container")[0]["image_url"]
        assert url.startswith("https://media.test/m/") and url.isascii()
        assert h.services.stager is not None
        public = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=h.services.stager.app()),  # type: ignore[attr-defined]
            base_url="https://media.test",
        )
        async with public:
            path = url.removeprefix("https://media.test")
            fetched = await public.get(path)
            assert fetched.status_code == 200
            assert fetched.content == JPEG
            assert fetched.headers["content-type"] == "image/jpeg"
            # The public listener serves staged files and nothing else.
            assert (await public.get("/v1/accounts")).status_code == 404
            assert (await public.get("/m/wrong-token/x.jpg")).status_code == 404
            assert (await public.post(path)).status_code == 405

            assert (await settle(h, publication_id))["status"] == "published"
            assert (await public.get(path)).status_code == 404  # withdrawn

    async def test_without_staging_only_public_urls_are_accepted(
        self, harness_factory: HarnessFactory, instagram: FakeInstagram
    ) -> None:
        h = await harness_factory(
            config=instagram_config(staging=False),
            transport=instagram.transport(),
            secrets=SECRETS,
        )
        caps = (await h.client.get("/v1/accounts/main", headers=h.auth())).json()["capabilities"]
        assert caps["media"]["image"]["sources"] == ["url"]

        upload = await h.client.post(
            "/v1/media", content=JPEG, headers={**h.auth(), "content-type": "image/jpeg"}
        )
        refused = await h.submit(media=[{"type": "image", "media_id": upload.json()["id"]}])
        assert refused.status_code == 422
        assert refused.json()["capability"] == "media_source:upload"
        assert (await publish(h, text="x", media=[IMAGE]))["status"] == "published"


class TestValidation:
    @pytest.mark.parametrize(
        ("body", "detail"),
        [
            ({"text": "words only"}, "takes 1 to 10 media items"),
            ({"kind": "post", "media": [VIDEO]}, "use kind 'reel'"),
            ({"kind": "reel", "media": [IMAGE]}, "image is not supported for kind 'reel'"),
            ({"kind": "story", "text": "hi", "media": [IMAGE]}, "stories cannot have a caption"),
            ({"text": "x" * 2201, "media": [IMAGE]}, "limit is 2200"),
            ({"text": " ".join(f"#t{i}" for i in range(31)), "media": [IMAGE]}, "30 hashtags"),
            ({"text": " ".join(f"@u{i}" for i in range(21)), "media": [IMAGE]}, "20 mentions"),
            ({"text": "x", "text_format": "html", "media": [IMAGE]}, "text_format"),
            ({"media": [IMAGE] * 11}, "takes 1 to 10 media items"),
            ({"media": [{"type": "document", "url": "https://x/a.pdf"}]}, "document is not"),
            ({"media": [IMAGE], "options": {"instagram": {"filter": "x"}}}, "options.instagram"),
        ],
    )
    async def test_refused_before_instagram_is_called(
        self, h: Harness, instagram: FakeInstagram, body: dict[str, Any], detail: str
    ) -> None:
        response = await h.client.post(
            "/v1/publications",
            json={"account": "main", **body},
            headers={**h.auth(), "idempotency-key": "k"},
        )
        assert response.status_code == 422, response.text
        assert detail in response.json()["detail"]
        assert instagram.calls == []

    async def test_only_jpeg_uploads(self, h: Harness) -> None:
        upload = await h.client.post(
            "/v1/media", content=b"png", headers={**h.auth(), "content-type": "image/png"}
        )
        response = await h.submit(media=[{"type": "image", "media_id": upload.json()["id"]}])
        assert response.json()["capability"] == "media_content_type"

    async def test_edit_and_delete_are_declared_unsupported(self, h: Harness) -> None:
        publication = await publish(h, text="x", media=[IMAGE])
        caps = (await h.client.get("/v1/accounts/main", headers=h.auth())).json()["capabilities"]
        assert caps["editable"] == [] and caps["deletable"] is False
        url = f"/v1/publications/{publication['id']}"
        edit = await h.client.patch(url, json={"text": "y"}, headers=h.auth())
        delete = await h.client.delete(url, headers=h.auth())
        assert edit.status_code == delete.status_code == 422
        assert edit.json()["capability"] == "edit:text"
        assert delete.json()["capability"] == "delete"

    async def test_refreshed_tokens_require_a_master_key(
        self, harness_factory: HarnessFactory
    ) -> None:
        with pytest.raises(ConfigError, match="master_key"):
            await harness_factory(config=instagram_config(master_key=False), secrets=SECRETS)

    @pytest.mark.parametrize("key", ["not base64!", base64.b64encode(b"short").decode()])
    async def test_bad_master_key_stops_startup(
        self, harness_factory: HarnessFactory, key: str
    ) -> None:
        with pytest.raises(Exception, match="master key"):
            await harness_factory(config=instagram_config(), secrets=SECRETS | {"master_key": key})


class TestFailures:
    @pytest.mark.parametrize(
        ("error", "category", "code"),
        [
            (graph_error(190, "Token expired", subcode=463), "authentication", "190/463"),
            (graph_error(10, "Permission denied", status=403), "authorization", "10"),
            (
                graph_error(200, "Requires instagram_business_content_publish"),
                "authorization",
                "200",
            ),
            (
                graph_error(100, "Invalid parameter", subcode=2207023),
                "invalid_request",
                "100/2207023",
            ),
            (
                graph_error(36001, "Unsupported format", subcode=2207005),
                "media_rejected",
                "36001/2207005",
            ),
            (
                graph_error(36003, "Bad aspect ratio", subcode=2207009),
                "media_rejected",
                "36003/2207009",
            ),
            (graph_error(352, "Video format", subcode=2207026), "media_rejected", "352/2207026"),
            (graph_error(4, "Spam", subcode=2207051), "provider_rejected", "4/2207051"),
            (
                graph_error(25, "Account restricted", subcode=2207050),
                "provider_rejected",
                "25/2207050",
            ),
            (graph_error(368, "Blocked for policy"), "provider_rejected", "368"),
            (graph_error(99999, "Something new"), "provider_rejected", "99999"),
        ],
    )
    async def test_permanent_errors_fail_once(
        self, h: Harness, instagram: FakeInstagram, error: httpx2.Response, category: str, code: str
    ) -> None:
        instagram.plan("create_container", error)
        publication = await publish(h, text="x", media=[IMAGE])
        assert publication["status"] == "failed"
        assert publication["error"]["category"] == category
        assert publication["error"]["retryable"] is False
        assert publication["error"]["provider_code"] == code
        assert publication["error"]["provider_request_id"] == "TRACE123"
        assert instagram.count("create_container") == 1

    @pytest.mark.parametrize(
        "failure",
        [
            graph_error(2, "Service temporarily unavailable", status=500),
            graph_error(1, "Unknown error", is_transient=True),
            graph_error(-2, "Timed out downloading media", subcode=2207003),
            httpx2.ReadTimeout("slow"),
            httpx2.ConnectError("dns"),
            httpx2.Response(502, text="<html>Bad Gateway</html>"),
            httpx2.Response(200, text="not json"),
        ],
        ids=["code-2", "transient", "download-timeout", "timeout", "connect", "502", "bad-json"],
    )
    async def test_creating_a_container_is_safe_to_retry(
        self, h: Harness, instagram: FakeInstagram, failure: httpx2.Response | Exception
    ) -> None:
        instagram.plan("create_container", failure)
        publication = await publish(h, text="x", media=[IMAGE])
        assert publication["status"] == "published"
        assert instagram.count("create_container") == 2
        assert instagram.count("media_publish") == 1

    async def test_processing_error_fails_the_publication(
        self, h: Harness, instagram: FakeInstagram
    ) -> None:
        instagram.plan(
            "container_status",
            httpx2.Response(
                200, json={"status_code": "ERROR", "status": "Error: unsupported video (2207026)"}
            ),
        )
        publication = await publish(h, kind="reel", media=[VIDEO])
        assert publication["status"] == "failed"
        assert publication["error"]["category"] == "media_rejected"
        assert "2207026" in publication["error"]["message"]
        assert instagram.count("media_publish") == 0

    async def test_expired_container_is_rebuilt_once(
        self, h: Harness, instagram: FakeInstagram
    ) -> None:
        expired = httpx2.Response(200, json={"status_code": "EXPIRED"})
        instagram.plan("container_status", expired)
        publication = await publish(h, text="x", media=[IMAGE])
        assert publication["status"] == "published"
        assert instagram.count("create_container") == 2

        instagram.plan("container_status", expired, expired)
        again = await settle(h, (await h.submit(text="y", media=[IMAGE], key="k2")).json()["id"])
        assert again["status"] == "failed"
        assert again["error"]["category"] == "timeout"
        assert again["error"]["retryable"] is True  # a new submission may work

    async def test_throttling_waits_for_the_time_meta_reports(
        self, h: Harness, instagram: FakeInstagram
    ) -> None:
        usage = {IG_USER_ID: [{"type": "instagram", "estimated_time_to_regain_access": 12}]}
        throttled = graph_error(80002, "Too many calls")
        throttled.headers["x-business-use-case-usage"] = json.dumps(usage)
        instagram.plan("create_container", throttled)
        publication_id = await submit(h, text="x", media=[IMAGE])
        await run_until_step(h, publication_id, "create")
        await h.drain()

        waiting = await h.status(publication_id)
        assert waiting["status"] == "processing"
        assert waiting["error"]["category"] == "rate_limited"
        assert instagram.count("create_container") == 1
        h.clock.advance(11 * 60)
        assert await h.drain() == 0
        h.clock.advance(2 * 60)
        assert (await settle(h, publication_id))["status"] == "published"

    async def test_exhausted_quota_defers_instead_of_burning_a_publish(
        self, h: Harness, instagram: FakeInstagram
    ) -> None:
        instagram.quota_usage = instagram.quota_total = 50
        publication_id = await submit(h, text="x", media=[IMAGE])
        await run_until_step(h, publication_id, "wait")
        h.clock.advance(5)
        await h.drain()

        waiting = await h.status(publication_id)
        assert waiting["error"]["category"] == "rate_limited"
        assert "50 of 50" in waiting["error"]["message"]
        assert instagram.count("media_publish") == 0

        instagram.quota_usage = 10  # the rolling window moved on
        assert (await settle(h, publication_id))["status"] == "published"

    async def test_publish_before_ready_goes_back_to_waiting(
        self, h: Harness, instagram: FakeInstagram
    ) -> None:
        instagram.plan(
            "media_publish", graph_error(9007, "Media ID is not available", subcode=2207027)
        )
        publication = await publish(h, text="x", media=[IMAGE])
        assert publication["status"] == "published"
        assert instagram.count("media_publish") == 2


class TestNoDuplicatePosts:
    """`media_publish` has no idempotency key. These are the cases the brief
    calls out: the post is created, but the service never hears about it."""

    @pytest.mark.parametrize(
        "lost",
        [
            httpx2.ReadTimeout("response lost"),
            httpx2.RemoteProtocolError("connection reset"),
            httpx2.Response(502, text="<html>Bad Gateway</html>"),
            graph_error(2, "Service temporarily unavailable", status=500),
            httpx2.Response(200, json={}),
        ],
        ids=["timeout", "reset", "502", "code-2", "empty-ok"],
    )
    async def test_published_but_response_lost_is_found_not_repeated(
        self, h: Harness, instagram: FakeInstagram, lost: httpx2.Response | Exception
    ) -> None:
        instagram.plan("media_publish", lost, apply=True)
        publication = await publish(h, text="Only once", media=[IMAGE])

        assert publication["status"] == "published"
        assert len(instagram.media) == 1, "the post must exist exactly once"
        assert instagram.count("media_publish") == 1
        assert publication["result"]["posts"] == [
            {"id": instagram.media[0]["id"], "url": instagram.media[0]["permalink"]}
        ]

    async def test_not_published_is_confirmed_twice_then_retried(
        self, h: Harness, instagram: FakeInstagram
    ) -> None:
        instagram.plan("media_publish", httpx2.ReadTimeout("request never arrived"))
        publication = await publish(h, text="Eventually", media=[IMAGE])

        assert publication["status"] == "published"
        assert len(instagram.media) == 1
        assert instagram.count("media_publish") == 2
        # Between the two attempts the container was checked more than once.
        operations = instagram.operations()
        first, second = (i for i, op in enumerate(operations) if op == "media_publish")
        assert operations[first:second].count("container_status") >= 2

    async def test_unknown_when_instagram_cannot_be_asked(
        self, h: Harness, instagram: FakeInstagram
    ) -> None:
        instagram.plan("media_publish", httpx2.ReadTimeout("lost"), apply=True)
        publication_id = await submit(h, text="x", media=[IMAGE])
        await run_until_step(h, publication_id, "publish")
        instagram.plan("container_status", *(httpx2.ReadTimeout("down") for _ in range(20)))
        publication = await settle(h, publication_id)

        assert publication["status"] == "outcome_unknown"
        assert instagram.count("media_publish") == 1
        assert len(instagram.media) == 1

    async def test_crash_during_publish_is_reconciled_after_restart(
        self, h: Harness, instagram: FakeInstagram
    ) -> None:
        publication_id = await submit(h, text="Survives a crash", media=[IMAGE])
        await run_until_step(h, publication_id, "publish")
        # The worker claims the publish step, Meta performs it, the process dies.
        claimed = h.services.db.claim_due(h.clock(), lease_seconds=90)
        assert claimed is not None
        instagram.publish_out_of_band((await state_of(h, publication_id))["container"])
        h.clock.advance(91)

        publication = await settle(h, publication_id)
        assert publication["status"] == "published"
        assert instagram.count("media_publish") == 0  # never sent again
        assert publication["result"]["posts"][0]["id"] == instagram.media[0]["id"]

    async def test_crash_before_publish_resumes_with_the_same_container(
        self, h: Harness, instagram: FakeInstagram
    ) -> None:
        publication_id = await submit(h, text="x", media=[IMAGE])
        await run_until_step(h, publication_id, "wait")
        assert h.services.db.claim_due(h.clock() + 5, lease_seconds=90) is not None
        h.clock.advance(120)  # process died mid-poll; polling is repeatable

        assert (await settle(h, publication_id))["status"] == "published"
        assert instagram.count("create_container") == 1


class TestTokenRefresh:
    async def test_token_is_refreshed_stored_encrypted_and_used(
        self, h: Harness, instagram: FakeInstagram
    ) -> None:
        worker = h.services.worker
        await worker.maintain_credentials(force=True)
        assert instagram.count("refresh_token") == 0  # younger than 24 hours: Meta would refuse

        h.clock.advance(25 * 3600)
        await worker.maintain_credentials(force=True)
        assert instagram.count("refresh_token") == 1

        # Meta now only accepts the refreshed token; publishing still works.
        assert (await publish(h, text="x", media=[IMAGE]))["status"] == "published"

        account = (await h.client.get("/v1/accounts/main", headers=h.auth())).json()
        assert account["credential_status"] == "ok"
        assert account["credential_expires_at"] is not None

        database = sqlite3.connect(h.services.settings.db_path)
        (blob,) = database.execute("SELECT ciphertext FROM credentials").fetchone()
        database.close()
        assert REFRESHED_TOKEN.encode() not in blob and TOKEN.encode() not in blob

        # Not refreshed again while it has more than 30 days left.
        h.clock.advance(20 * DAY)
        await worker.maintain_credentials(force=True)
        assert instagram.count("refresh_token") == 1
        h.clock.advance(15 * DAY)
        await worker.maintain_credentials(force=True)
        assert instagram.count("refresh_token") == 2

    async def test_refreshed_token_survives_restart_and_a_new_seed_replaces_it(
        self, h: Harness, instagram: FakeInstagram
    ) -> None:
        await h.services.worker.maintain_credentials(force=True)
        h.clock.advance(25 * 3600)
        await h.services.worker.maintain_credentials(force=True)
        assert instagram.count("refresh_token") == 1
        account = h.services.config.accounts["main"]

        def fresh_store(master_key: str = MASTER_KEY) -> CredentialStore:
            secrets = FileSecretResolver(h.secrets_dir)
            (h.secrets_dir / "mk").write_text(master_key)
            return CredentialStore(h.services.db, secrets, h.clock, secrets.get("mk"))

        assert fresh_store().resolve(account)["access_token"].reveal() == REFRESHED_TOKEN

        # The stale seed file is still on disk and must not win...
        assert (h.secrets_dir / "ig_token").read_text() == TOKEN
        # ...but a seed the operator replaces does.
        (h.secrets_dir / "ig_token").write_text("IGAAbrandNEWseedTOKENfromTHEdashboard")
        assert fresh_store().resolve(account)["access_token"].reveal().startswith("IGAAbrandNEW")

        (h.secrets_dir / "ig_token").write_text(TOKEN)
        wrong_key = base64.b64encode(bytes(32)).decode()
        with pytest.raises(Exception, match="cannot be decrypted"):
            fresh_store(wrong_key).resolve(account)

    async def test_failing_refresh_is_surfaced_before_the_token_lapses(
        self, h: Harness, instagram: FakeInstagram
    ) -> None:
        worker = h.services.worker
        await worker.maintain_credentials(force=True)  # first sight of the seed
        h.clock.advance(25 * 3600)
        await worker.maintain_credentials(force=True)  # refreshed; expiry is now known
        assert instagram.count("refresh_token") == 1
        instagram.plan(
            "refresh_token", *(httpx2.Response(503, text="unavailable") for _ in range(10))
        )

        async def status() -> str:
            account = await h.client.get("/v1/accounts/main", headers=h.auth())
            return account.json()["credential_status"]  # type: ignore[no-any-return]

        h.clock.advance(EXPIRES_IN - 5 * DAY)
        await worker.maintain_credentials(force=True)
        assert await status() == "expiring"
        h.clock.advance(6 * DAY)
        assert await status() == "reauthorization_required"

    async def test_rejected_token_takes_the_account_out_of_service(
        self, h: Harness, instagram: FakeInstagram
    ) -> None:
        instagram.valid_token = "revoked-elsewhere"
        publication = await publish(h, text="x", media=[IMAGE])
        assert publication["error"]["category"] == "authentication"
        refused = await h.submit(text="y", media=[IMAGE], key="k2")
        assert refused.status_code == 409


async def test_access_token_never_leaks(
    h: Harness, instagram: FakeInstagram, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("DEBUG")
    echo = f"bad request to /me?access_token={TOKEN} with Bearer {TOKEN}"
    instagram.plan("create_container", graph_error(2, echo, status=500), graph_error(100, echo))
    instagram.plan("media_publish", httpx2.ReadTimeout(echo), apply=True)
    responses = [await h.submit(text="a", media=[IMAGE], key="a")]
    await settle(h, responses[0].json()["id"])
    responses.append(await h.submit(text="b", media=[IMAGE], key="b"))
    await settle(h, responses[1].json()["id"])
    await h.services.worker.maintain_credentials(force=True)
    h.clock.advance(25 * 3600)
    await h.services.worker.maintain_credentials(force=True)
    for response in list(responses):
        responses.append(
            await h.client.get(f"/v1/publications/{response.json()['id']}", headers=h.auth())
        )
    responses.append(await h.client.get("/v1/accounts", headers=h.auth()))

    logs = capsys.readouterr()
    assert '"event": "instagram_request"' in logs.out
    assert '"event": "credential_refreshed"' in logs.out
    database = sqlite3.connect(h.services.settings.db_path)
    stored = "\n".join(
        str(row)
        for table in ("publications", "credentials")
        for row in database.execute(f"SELECT * FROM {table}")  # noqa: S608
    )
    database.close()
    for where, text in {
        "logs": logs.out + logs.err,
        "responses": "\n".join(r.text + str(r.headers) for r in responses),
        "database": stored,
    }.items():
        for secret in (TOKEN, REFRESHED_TOKEN, MASTER_KEY):
            assert secret not in text, f"a credential leaked into {where}"
