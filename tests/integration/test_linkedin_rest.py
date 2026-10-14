"""The LinkedIn REST implementation, end to end through the service,
against a faked LinkedIn."""

from __future__ import annotations

import base64
import sqlite3
from typing import Any

import httpx2
import pytest

from content_fleet.config import ConfigError, FleetConfig
from content_fleet.observability import configure_logging
from content_fleet.providers.linkedin.rest import escape_little
from tests.conftest import Harness, HarnessFactory
from tests.fakes.linkedin import (
    ACCESS_EXPIRES_IN,
    CLIENT_ID,
    CLIENT_SECRET,
    ORGANIZATION,
    PERSON,
    REFRESH_TOKEN,
    REFRESHED_TOKEN,
    TOKEN,
    FakeLinkedIn,
    li_error,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

MASTER_KEY = base64.b64encode(bytes(range(32))).decode()
DAY = 86400
PNG = b"\x89PNG\r\n not really a png"
SECRETS = {
    "li_token": TOKEN,
    "li_refresh": REFRESH_TOKEN,
    "li_client_id": CLIENT_ID,
    "li_client_secret": CLIENT_SECRET,
    "master_key": MASTER_KEY,
}
FULL_CREDENTIALS = {
    "access_token": "li_token",
    "refresh_token": "li_refresh",
    "client_id": "li_client_id",
    "client_secret": "li_client_secret",
}


def linkedin_config(
    author: str = ORGANIZATION, credentials: dict[str, str] | None = None, **settings: Any
) -> FleetConfig:
    return FleetConfig.parse(
        {
            "consumers": {"tester": {"api_key": "consumer_key"}},
            "service": {"master_key": "master_key"},
            "accounts": {
                "main": {
                    "platform": "linkedin",
                    "implementation": "rest",
                    "credentials": credentials or {"access_token": "li_token"},
                    "settings": {"author": author, **settings},
                }
            },
        }
    )


@pytest.fixture
def linkedin() -> FakeLinkedIn:
    return FakeLinkedIn()


@pytest.fixture
async def h(harness_factory: HarnessFactory, linkedin: FakeLinkedIn) -> Harness:
    return await harness_factory(
        config=linkedin_config(),
        transport=linkedin.transport(),
        secrets=SECRETS,
        max_upload_bytes=20 * 1024 * 1024,
    )


@pytest.fixture
async def member(harness_factory: HarnessFactory, linkedin: FakeLinkedIn) -> Harness:
    """A personal-profile account: it may not read assets or list posts."""
    linkedin.can_read_assets = linkedin.can_list_posts = False
    return await harness_factory(
        config=linkedin_config(PERSON), transport=linkedin.transport(), secrets=SECRETS
    )


async def upload(h: Harness, data: bytes = PNG, content_type: str = "image/png") -> dict[str, Any]:
    response = await h.client.post(
        "/v1/media", content=data, headers={**h.auth(), "content-type": content_type}
    )
    assert response.status_code == 201, response.text
    return {"media_id": response.json()["id"]}


async def settle(h: Harness, publication_id: str, *, rounds: int = 60) -> dict[str, Any]:
    for _ in range(rounds):
        await h.drain()
        status = await h.status(publication_id)
        if status["next_attempt_at"] is None:
            return status
        h.clock.advance(60)
    raise AssertionError(f"did not settle: {status}")


async def publish(h: Harness, **body: Any) -> dict[str, Any]:
    response = await h.submit(body)
    assert response.status_code == 202, response.text
    return await settle(h, response.json()["id"])


async def run_until_step(h: Harness, publication_id: str, step: str) -> dict[str, Any]:
    for _ in range(40):
        record = h.services.db.get_publication(publication_id)
        assert record is not None
        if record.state.get("step") == step:
            return record.state
        if not await h.services.worker.run_once():
            h.clock.advance(60)
    raise AssertionError(f"never reached step {step}")


class TestPublishing:
    async def test_text_post_names_its_author_and_escapes_plain_text(
        self, h: Harness, linkedin: FakeLinkedIn
    ) -> None:
        text = "Q3 results (up 12%) are out_now! #earnings via @acme [link] 50 # 2"
        publication = await publish(h, text=text)

        assert publication["status"] == "published"
        (post,) = publication["result"]["posts"]
        assert post["id"].startswith("urn:li:share:")
        assert post["url"] == f"https://www.linkedin.com/feed/update/{post['id']}/"
        (body,) = linkedin.bodies("create_post")
        assert body == {
            "author": ORGANIZATION,
            "commentary": (
                r"Q3 results \(up 12%\) are out\_now! #earnings via \@acme \[link\] 50 \# 2"
            ),
            "visibility": "PUBLIC",
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        headers = linkedin.calls[-1].headers
        assert headers["linkedin-version"] == "202609"
        assert headers["x-restli-protocol-version"] == "2.0.0"

    async def test_little_format_is_sent_as_written(
        self, h: Harness, linkedin: FakeLinkedIn
    ) -> None:
        text = "Thanks @[Acme Corp](urn:li:organization:2414183)"
        await publish(h, text=text, text_format="little")
        assert linkedin.bodies("create_post")[0]["commentary"] == text

    async def test_image_is_uploaded_as_bytes_then_referenced(
        self, h: Harness, linkedin: FakeLinkedIn
    ) -> None:
        image = await upload(h)
        publication = await publish(
            h,
            text="Look",
            media=[{"type": "image", "alt_text": "A chart", **image}],
            options={"linkedin": {"visibility": "CONNECTIONS", "disable_reshare": True}},
        )

        assert publication["status"] == "published"
        assert linkedin.operations() == [
            "initialize_upload",
            "upload_media",
            "asset_status",
            "create_post",
        ]
        assert linkedin.bodies("initialize_upload") == [
            {"initializeUploadRequest": {"owner": ORGANIZATION}}
        ]
        ((urn, asset),) = linkedin.assets.items()
        assert asset["bytes"] == PNG
        body = linkedin.bodies("create_post")[0]
        assert body["content"] == {"media": {"id": urn, "altText": "A chart"}}
        assert body["visibility"] == "CONNECTIONS" and body["isReshareDisabledByAuthor"] is True

    async def test_several_images_become_one_multi_image_post(
        self, h: Harness, linkedin: FakeLinkedIn
    ) -> None:
        images = [{"type": "image", **(await upload(h, PNG + bytes([i])))} for i in range(3)]
        images[0]["alt_text"] = "first"
        publication = await publish(h, text="Three", media=images)

        assert len(publication["result"]["posts"]) == 1
        content = linkedin.bodies("create_post")[0]["content"]
        urns = list(linkedin.assets)
        assert content == {
            "multiImage": {
                "images": [{"id": urns[0], "altText": "first"}, {"id": urns[1]}, {"id": urns[2]}]
            }
        }

    async def test_video_goes_up_in_parts_and_is_awaited(
        self, h: Harness, linkedin: FakeLinkedIn
    ) -> None:
        data = bytes(range(256)) * (9 * 1024 * 1024 // 256 + 1)  # just over two 4 MB parts
        video = await upload(h, data, "video/mp4")
        publication = await publish(
            h,
            text="Demo",
            media=[{"type": "video", **video}],
            options={"linkedin": {"media_title": "Product demo"}},
        )

        assert publication["status"] == "published"
        ((urn, asset),) = linkedin.assets.items()
        assert asset["bytes"] == data
        assert len(asset["etags"]) == 3 == linkedin.count("upload_media")
        assert linkedin.bodies("initialize_upload")[0]["initializeUploadRequest"] == {
            "owner": ORGANIZATION,
            "fileSizeBytes": len(data),
            "uploadCaptions": False,
            "uploadThumbnail": False,
        }
        assert linkedin.count("asset_status") == 2  # processing, then available
        assert linkedin.bodies("create_post")[0]["content"] == {
            "media": {"id": urn, "title": "Product demo"}
        }

    async def test_document_gets_the_title_linkedin_requires(
        self, h: Harness, linkedin: FakeLinkedIn
    ) -> None:
        document = await upload(h, b"%PDF-1.7", "application/pdf")
        await publish(h, text="Report", media=[{"type": "document", **document}])
        content = linkedin.bodies("create_post")[0]["content"]
        assert content["media"]["title"] == "Document"

    async def test_member_account_publishes_without_asset_read_permission(
        self, member: Harness, linkedin: FakeLinkedIn
    ) -> None:
        image = await upload(member)
        publication = await publish(member, text="Mine", media=[{"type": "image", **image}])
        assert publication["status"] == "published"
        assert linkedin.bodies("create_post")[0]["author"] == PERSON


class TestValidation:
    @pytest.mark.parametrize(
        ("body", "detail"),
        [
            ({"text": "x" * 3001}, "limit is 3000"),
            ({"text": "x", "text_format": "html"}, "text_format"),
            ({"kind": "reel", "text": "x"}, "kind 'reel'"),
            (
                {"media": [{"type": "image", "url": "https://cdn.example/a.png"}]},
                "cannot be supplied by url",
            ),
            ({"text": "x", "options": {"linkedin": {"visibility": "EVERYONE"}}}, "visibility"),
        ],
    )
    async def test_refused_before_linkedin_is_called(
        self, h: Harness, linkedin: FakeLinkedIn, body: dict[str, Any], detail: str
    ) -> None:
        response = await h.client.post(
            "/v1/publications",
            json={"account": "main", **body},
            headers={**h.auth(), "idempotency-key": "k"},
        )
        assert response.status_code == 422, response.text
        assert detail in response.json()["detail"]
        assert linkedin.calls == []

    async def test_only_images_can_be_combined(self, h: Harness) -> None:
        image = await upload(h)
        document = await upload(h, b"%PDF", "application/pdf")
        response = await h.submit(
            media=[{"type": "image", **image}, {"type": "document", **document}]
        )
        assert response.status_code == 422
        assert "one video or one document, or up to 20 images" in response.json()["detail"]

    @pytest.mark.parametrize(
        ("author", "credentials"),
        [
            ("acme-page", {"access_token": "li_token"}),
            ("urn:li:group:123", {"access_token": "li_token"}),
            (ORGANIZATION, {"refresh_token": "li_refresh"}),
            (ORGANIZATION, {"access_token": "li_token", "refresh_token": "li_refresh"}),
            (ORGANIZATION, {"access_token": "li_token", "client_id": "li_client_id"}),
        ],
    )
    async def test_misconfigured_account_stops_startup(
        self, harness_factory: HarnessFactory, author: str, credentials: dict[str, str]
    ) -> None:
        with pytest.raises(ConfigError, match="account 'main'"):
            await harness_factory(config=linkedin_config(author, credentials), secrets=SECRETS)


class TestFailures:
    @pytest.mark.parametrize(
        ("error", "category"),
        [
            (li_error(401, "The token has expired or been revoked"), "authentication"),
            (li_error(403, "Not enough permissions", "ACCESS_DENIED"), "authorization"),
            (li_error(400, "commentary is too long", "FIELD_LENGTH_TOO_LONG"), "invalid_request"),
            (li_error(422, "Content is a duplicate"), "provider_rejected"),
            (li_error(404, "Not found", "NOT_FOUND"), "provider_rejected"),
        ],
    )
    async def test_permanent_errors_fail_once(
        self, h: Harness, linkedin: FakeLinkedIn, error: httpx2.Response, category: str
    ) -> None:
        linkedin.plan("create_post", error)
        publication = await publish(h, text="x")
        assert publication["status"] == "failed"
        assert publication["error"]["category"] == category
        assert publication["error"]["retryable"] is False
        assert publication["error"]["provider_request_id"] == "uuid-123"
        assert linkedin.count("create_post") == 1

    async def test_sunset_api_version_tells_the_operator_what_to_do(
        self, h: Harness, linkedin: FakeLinkedIn
    ) -> None:
        linkedin.version = "202712"
        publication = await publish(h, text="x")
        assert publication["status"] == "failed"
        assert "settings.api_version" in publication["error"]["message"]

    async def test_configured_api_version_is_sent(
        self, harness_factory: HarnessFactory, linkedin: FakeLinkedIn
    ) -> None:
        linkedin.version = "202712"
        h = await harness_factory(
            config=linkedin_config(api_version="202712"),
            transport=linkedin.transport(),
            secrets=SECRETS,
        )
        assert (await publish(h, text="x"))["status"] == "published"

    async def test_rate_limit_backs_off_for_minutes_not_seconds(
        self, h: Harness, linkedin: FakeLinkedIn
    ) -> None:
        linkedin.plan("create_post", li_error(429, "Resource level throttle limit reached"))
        response = await h.submit(text="x")
        await run_until_step(h, response.json()["id"], "create")
        await h.drain()
        waiting = await h.status(response.json()["id"])
        assert waiting["error"]["category"] == "rate_limited"
        h.clock.advance(14 * 60)
        assert await h.drain() == 0
        h.clock.advance(2 * 60)
        assert (await settle(h, response.json()["id"]))["status"] == "published"
        assert len(linkedin.posts) == 1

    @pytest.mark.parametrize(
        "failure",
        [li_error(503, "unavailable"), httpx2.ReadTimeout("slow"), httpx2.ConnectError("dns")],
    )
    async def test_uploads_are_safe_to_retry(
        self, h: Harness, linkedin: FakeLinkedIn, failure: httpx2.Response | Exception
    ) -> None:
        linkedin.plan("upload_media", failure)
        image = await upload(h)
        publication = await publish(h, text="x", media=[{"type": "image", **image}])
        assert publication["status"] == "published"
        assert len(linkedin.posts) == 1

    async def test_failed_media_processing_fails_the_publication(
        self, h: Harness, linkedin: FakeLinkedIn
    ) -> None:
        linkedin.plan(
            "asset_status",
            httpx2.Response(
                200, json={"status": "PROCESSING_FAILED", "processingFailureReason": "bad codec"}
            ),
        )
        image = await upload(h)
        publication = await publish(h, text="x", media=[{"type": "image", **image}])
        assert publication["status"] == "failed"
        assert publication["error"]["category"] == "media_rejected"
        assert "bad codec" in publication["error"]["message"]
        assert linkedin.count("create_post") == 0

    async def test_upload_url_outside_linkedin_is_refused(
        self, h: Harness, linkedin: FakeLinkedIn
    ) -> None:
        linkedin.plan(
            "initialize_upload",
            httpx2.Response(
                200,
                json={
                    "value": {
                        "image": "urn:li:image:C1",
                        "uploadUrl": "https://evil.example/collect?linkedin.com",
                    }
                },
            ),
        )
        image = await upload(h)
        publication = await publish(h, text="x", media=[{"type": "image", **image}])
        assert publication["status"] == "failed"
        assert "outside linkedin.com" in publication["error"]["message"]
        assert linkedin.count("upload_media") == 0  # neither bytes nor token left the service


class TestNoDuplicatePosts:
    @pytest.mark.parametrize(
        "lost",
        [
            httpx2.ReadTimeout("response lost"),
            li_error(500, "Internal server error"),
            li_error(504, "Gateway timeout"),
            li_error(409, "Conflict", "CONFLICT"),
            httpx2.Response(201),  # created, but the id header is missing
        ],
        ids=["timeout", "500", "504", "409", "no-id"],
    )
    async def test_page_post_created_but_unconfirmed_is_found_not_repeated(
        self, h: Harness, linkedin: FakeLinkedIn, lost: httpx2.Response | Exception
    ) -> None:
        linkedin.plan("create_post", lost, apply=True)
        publication = await publish(h, text="Only once (really) #news")

        assert publication["status"] == "published"
        assert len(linkedin.posts) == 1
        assert linkedin.count("create_post") == 1
        assert publication["result"]["posts"][0]["id"] == linkedin.posts[0]["id"]

    async def test_page_post_not_created_is_checked_twice_then_retried(
        self, h: Harness, linkedin: FakeLinkedIn
    ) -> None:
        linkedin.plan("create_post", httpx2.ReadTimeout("never arrived"))
        publication = await publish(h, text="Eventually")
        assert publication["status"] == "published"
        assert len(linkedin.posts) == 1
        assert linkedin.count("create_post") == 2
        assert linkedin.count("list_posts") == 2

    async def test_member_post_cannot_be_checked_so_it_is_unknown(
        self, member: Harness, linkedin: FakeLinkedIn
    ) -> None:
        linkedin.plan("create_post", httpx2.ReadTimeout("lost"), apply=True)
        publication = await publish(member, text="Mine")

        assert publication["status"] == "outcome_unknown"
        assert "cannot list its posts" in publication["error"]["message"]
        assert linkedin.count("create_post") == 1
        assert linkedin.count("list_posts") == 0  # not even attempted for a member

    async def test_page_without_read_permission_is_unknown(
        self, h: Harness, linkedin: FakeLinkedIn
    ) -> None:
        linkedin.can_list_posts = False
        linkedin.plan("create_post", httpx2.ReadTimeout("lost"), apply=True)
        publication = await publish(h, text="x")
        assert publication["status"] == "outcome_unknown"
        assert len(linkedin.posts) == 1

    async def test_duplicate_refusal_after_an_unconfirmed_attempt_is_unknown(
        self, h: Harness, linkedin: FakeLinkedIn
    ) -> None:
        # The first create got through but is not visible in the listing yet;
        # the retry is then refused as duplicate content.
        linkedin.plan("create_post", httpx2.ReadTimeout("lost"))
        linkedin.plan("create_post", li_error(422, "Content is a duplicate"))
        publication = await publish(h, text="x")
        assert publication["status"] == "outcome_unknown"
        assert "duplicate" in publication["error"]["message"]

    async def test_crash_during_create_is_reconciled_after_restart(
        self, h: Harness, linkedin: FakeLinkedIn
    ) -> None:
        response = await h.submit(text="Survives a crash")
        publication_id = response.json()["id"]
        await run_until_step(h, publication_id, "create")
        assert h.services.db.claim_due(h.clock(), lease_seconds=90) is not None
        linkedin.create_out_of_band(
            {
                "author": ORGANIZATION,
                "commentary": "Survives a crash",
                "visibility": "PUBLIC",
                "distribution": {},
                "lifecycleState": "PUBLISHED",
            }
        )
        h.clock.advance(91)

        publication = await settle(h, publication_id)
        assert publication["status"] == "published"
        assert linkedin.count("create_post") == 0
        assert publication["result"]["posts"][0]["id"] == linkedin.posts[0]["id"]

    async def test_uploaded_assets_survive_a_retry_of_a_later_item(
        self, h: Harness, linkedin: FakeLinkedIn
    ) -> None:
        images = [{"type": "image", **(await upload(h, PNG + bytes([i])))} for i in range(2)]
        linkedin.plan("initialize_upload", httpx2.Response(200, json={"value": {}}))  # 1st: junk
        publication = await publish(h, text="x", media=images)
        assert publication["status"] == "published"
        assert len(linkedin.assets) == 2


class TestEditAndDelete:
    async def test_edit_changes_commentary_with_escaping(
        self, h: Harness, linkedin: FakeLinkedIn
    ) -> None:
        publication = await publish(h, text="first")
        response = await h.client.patch(
            f"/v1/publications/{publication['id']}", json={"text": "second (v2)"}, headers=h.auth()
        )
        assert response.status_code == 200, response.text
        assert linkedin.bodies("edit_post") == [
            {"patch": {"$set": {"commentary": r"second \(v2\)"}}}
        ]
        assert linkedin.posts[0]["commentary"] == r"second \(v2\)"

    async def test_delete_is_idempotent(self, h: Harness, linkedin: FakeLinkedIn) -> None:
        publication = await publish(h, text="x")
        url = f"/v1/publications/{publication['id']}"
        assert (await h.client.delete(url, headers=h.auth())).json()["status"] == "deleted"
        assert linkedin.posts == []
        assert (await h.client.delete(url, headers=h.auth())).status_code == 200
        assert linkedin.count("delete_post") == 1

    async def test_share_that_rest_posts_cannot_find_is_deleted_through_v2_shares(
        self, h: Harness, linkedin: FakeLinkedIn
    ) -> None:
        """Observed live: DELETE /rest/posts answered 404 for a share that was
        still up. Trusting that 404 left a real post on the profile."""
        publication = await publish(h, text="x")
        assert publication["result"]["posts"][0]["id"].startswith("urn:li:share:")
        response = await h.client.delete(f"/v1/publications/{publication['id']}", headers=h.auth())

        assert response.json()["status"] == "deleted"
        assert linkedin.operations()[-2:] == ["delete_post", "delete_share"]
        assert linkedin.posts == []

    async def test_ugc_post_not_found_is_not_reported_as_deleted(
        self, h: Harness, linkedin: FakeLinkedIn
    ) -> None:
        publication = await publish(h, text="x")
        record = h.services.db.get_publication(publication["id"])
        assert record is not None and record.result is not None
        record.result.posts[0].id = "urn:li:ugcPost:123"
        h.services.db.finish(record.id, now=h.clock(), status=record.status, result=record.result)
        linkedin.plan("delete_post", li_error(404, "Could not find entity", "NOT_FOUND"))
        response = await h.client.delete(f"/v1/publications/{publication['id']}", headers=h.auth())
        assert response.status_code == 502
        assert (await h.status(publication["id"]))["status"] == "published"

    async def test_failed_edit_is_a_problem_document(
        self, h: Harness, linkedin: FakeLinkedIn
    ) -> None:
        publication = await publish(h, text="x")
        linkedin.plan("edit_post", li_error(503, "unavailable"))
        response = await h.client.patch(
            f"/v1/publications/{publication['id']}", json={"text": "y"}, headers=h.auth()
        )
        assert response.status_code == 503
        assert response.json()["retryable"] is True


class TestCredentials:
    @pytest.fixture
    async def full(self, harness_factory: HarnessFactory, linkedin: FakeLinkedIn) -> Harness:
        return await harness_factory(
            config=linkedin_config(credentials=FULL_CREDENTIALS),
            transport=linkedin.transport(),
            secrets=SECRETS,
        )

    async def test_access_token_is_renewed_with_the_refresh_token(
        self, full: Harness, linkedin: FakeLinkedIn
    ) -> None:
        await full.services.worker.maintain_credentials(force=True)
        assert linkedin.count("refresh_token") == 1
        assert linkedin.bodies("refresh_token")[0]["grant_type"] == "refresh_token"

        # LinkedIn now only accepts the renewed token.
        assert (await publish(full, text="x"))["status"] == "published"
        account = (await full.client.get("/v1/accounts/main", headers=full.auth())).json()
        assert account["credential_status"] == "ok"

        full.clock.advance(ACCESS_EXPIRES_IN - 20 * DAY)
        await full.services.worker.maintain_credentials(force=True)
        assert linkedin.count("refresh_token") == 1  # more than 14 days left
        full.clock.advance(10 * DAY)
        await full.services.worker.maintain_credentials(force=True)
        assert linkedin.count("refresh_token") == 2

    async def test_revoked_refresh_token_needs_a_person(
        self, full: Harness, linkedin: FakeLinkedIn
    ) -> None:
        linkedin.plan(
            "refresh_token",
            httpx2.Response(
                400,
                json={
                    "error": "invalid_request",
                    "error_description": "The provided authorization grant or refresh token "
                    "is invalid, expired or revoked",
                },
            ),
        )
        await full.services.worker.maintain_credentials(force=True)
        account = (await full.client.get("/v1/accounts/main", headers=full.auth())).json()
        assert account["credential_status"] == "reauthorization_required"
        assert (await full.submit(text="x")).status_code == 409

    async def test_without_a_refresh_token_the_deadline_is_still_reported(
        self, harness_factory: HarnessFactory, linkedin: FakeLinkedIn
    ) -> None:
        h = await harness_factory(
            config=linkedin_config(
                credentials={
                    "access_token": "li_token",
                    "client_id": "li_client_id",
                    "client_secret": "li_client_secret",
                }
            ),
            transport=linkedin.transport(),
            secrets=SECRETS,
        )
        linkedin.token_expires_at = int(h.clock()) + 30 * DAY
        await h.services.worker.maintain_credentials(force=True)
        assert linkedin.count("introspect_token") == 1
        assert linkedin.count("refresh_token") == 0

        async def account() -> dict[str, Any]:
            response = await h.client.get("/v1/accounts/main", headers=h.auth())
            return response.json()  # type: ignore[no-any-return]

        assert (await account())["credential_status"] == "ok"
        assert (await account())["credential_expires_at"] is not None
        h.clock.advance(25 * DAY)
        assert (await account())["credential_status"] == "expiring"
        h.clock.advance(6 * DAY)
        assert (await account())["credential_status"] == "reauthorization_required"

    async def test_token_only_account_has_nothing_to_maintain(
        self, h: Harness, linkedin: FakeLinkedIn
    ) -> None:
        await h.services.worker.maintain_credentials(force=True)
        assert linkedin.calls == []


def test_escaping_makes_plain_text_render_as_written() -> None:
    assert escape_little("a|b{c}d@e[f]g(h)i<j>k\\l*m_n~o") == (
        r"a\|b\{c\}d\@e\[f\]g\(h\)i\<j\>k\\l\*m\_n\~o"
    )
    assert escape_little("#tag stays, # alone and C# do not") == (
        r"#tag stays, \# alone and C\# do not"
    )


async def test_linkedin_credentials_never_leak(
    harness_factory: HarnessFactory, linkedin: FakeLinkedIn, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("DEBUG")
    h = await harness_factory(
        config=linkedin_config(credentials=FULL_CREDENTIALS),
        transport=linkedin.transport(),
        secrets=SECRETS,
    )
    echo = f"Bearer {TOKEN} client_secret={CLIENT_SECRET} refresh_token={REFRESH_TOKEN}"
    linkedin.plan("create_post", li_error(400, echo), httpx2.ReadTimeout(echo))
    responses = [await h.submit(text="a", key="a"), await h.submit(text="b", key="b")]
    for response in responses[:2]:
        await settle(h, response.json()["id"])
    await h.services.worker.maintain_credentials(force=True)
    for response in list(responses):
        responses.append(
            await h.client.get(f"/v1/publications/{response.json()['id']}", headers=h.auth())
        )
    responses.append(await h.client.get("/v1/accounts", headers=h.auth()))

    logs = capsys.readouterr()
    assert '"event": "linkedin_request"' in logs.out
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
        for secret in (TOKEN, REFRESHED_TOKEN, REFRESH_TOKEN, CLIENT_SECRET, MASTER_KEY):
            assert secret not in text, f"a credential leaked into {where}"
