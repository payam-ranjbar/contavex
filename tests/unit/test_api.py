from __future__ import annotations

import pytest

from content_fleet.core.errors import ErrorCategory, PublishError
from tests.conftest import OTHER_KEY, Harness

pytestmark = [pytest.mark.unit, pytest.mark.anyio]

PROBLEM = "application/problem+json"


async def upload(
    h: Harness, data: bytes = b"\xff\xd8jpeg", content_type: str = "image/jpeg"
) -> str:
    response = await h.client.post(
        "/v1/media", content=data, headers={**h.auth(), "content-type": content_type}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]  # type: ignore[no-any-return]


class TestAuthentication:
    @pytest.mark.parametrize(
        "headers", [{}, {"authorization": "Bearer wrong"}, {"authorization": "Basic abc"}]
    )
    async def test_requires_a_valid_consumer_key(
        self, harness: Harness, headers: dict[str, str]
    ) -> None:
        for method, path in [("GET", "/v1/accounts"), ("POST", "/v1/publications")]:
            response = await harness.client.request(method, path, headers=headers, json={})
            assert response.status_code == 401
            assert response.headers["content-type"] == PROBLEM
            assert response.json()["type"] == "urn:content-fleet:problem:unauthenticated"

    async def test_health_endpoints_need_no_key(self, harness: Harness) -> None:
        assert (await harness.client.get("/livez")).status_code == 200
        ready = await harness.client.get("/readyz")
        assert ready.status_code == 200
        assert ready.json()["checks"] == {"database": True, "worker": True}


class TestSubmit:
    async def test_accepts_and_points_at_the_status_resource(self, harness: Harness) -> None:
        response = await harness.submit(client_reference="campaign-1")
        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "accepted"
        assert body["platform"] == "fake"
        assert body["client_reference"] == "campaign-1"
        assert response.headers["location"] == f"/v1/publications/{body['id']}"
        assert harness.provider.calls == []  # nothing sent to the provider yet

    async def test_requires_an_idempotency_key(self, harness: Harness) -> None:
        response = await harness.client.post(
            "/v1/publications", json={"account": "main", "text": "x"}, headers=harness.auth()
        )
        assert response.status_code == 400
        assert response.json()["type"].endswith(":idempotency-key-required")

    async def test_same_key_same_body_returns_the_original(self, harness: Harness) -> None:
        first = await harness.submit(key="k")
        again = await harness.submit(key='"k"')  # structured-field quoting is accepted
        assert again.status_code == 202
        assert again.json()["id"] == first.json()["id"]
        assert again.headers["idempotent-replay"] == "true"
        await harness.drain()
        assert len(harness.provider.calls) == 1

    async def test_replay_after_publishing_reports_the_result(self, harness: Harness) -> None:
        first = await harness.submit(key="k")
        await harness.drain()
        again = await harness.submit(key="k")
        assert again.json()["id"] == first.json()["id"]
        assert again.json()["status"] == "published"
        assert len(harness.provider.calls) == 1

    async def test_same_key_different_body_is_refused(self, harness: Harness) -> None:
        first = await harness.submit(key="k", text="one")
        second = await harness.submit(key="k", text="two")
        assert second.status_code == 422
        assert second.json()["type"].endswith(":idempotency-key-reused")
        assert second.json()["publication_id"] == first.json()["id"]

    async def test_keys_are_scoped_to_the_consumer(self, harness: Harness) -> None:
        mine = await harness.submit(key="shared")
        theirs = await harness.client.post(
            "/v1/publications",
            json={"account": "main", "text": "hello"},
            headers={**harness.auth(OTHER_KEY), "idempotency-key": "shared"},
        )
        assert theirs.status_code == 202
        assert theirs.json()["id"] != mine.json()["id"]

    @pytest.mark.parametrize(
        "body",
        [
            {"account": "main"},
            {"account": "main", "text": "   "},
            {"account": "main", "text": "x", "surprise": 1},
            {"account": "main", "text": "x", "media": [{"type": "image"}]},
            {"account": "main", "media": [{"type": "gif", "media_id": "m"}]},
            {"account": "main", "media": [{"type": "image", "url": "http://insecure/a.jpg"}]},
            {
                "account": "main",
                "media": [{"type": "image", "media_id": "m", "url": "https://x/a"}],
            },
        ],
    )
    async def test_malformed_requests_are_problems(
        self, harness: Harness, body: dict[str, object]
    ) -> None:
        response = await harness.client.post(
            "/v1/publications", json=body, headers={**harness.auth(), "idempotency-key": "k"}
        )
        assert response.status_code == 422
        assert response.headers["content-type"] == PROBLEM
        problem = response.json()
        assert problem["type"].endswith(":invalid-request")
        assert problem["errors"] and all("field" in e for e in problem["errors"])

    async def test_unknown_account(self, harness: Harness) -> None:
        response = await harness.submit(account="nope")
        assert response.status_code == 422
        assert response.json()["type"].endswith(":unknown-account")

    async def test_other_platforms_options_are_ignored(self, harness: Harness) -> None:
        response = await harness.submit(options={"telegram": {"disable_notification": True}})
        assert response.status_code == 202


class TestCapabilityChecks:
    @pytest.mark.parametrize(
        ("overrides", "capability"),
        [
            ({"kind": "reel"}, "kind:reel"),
            ({"text_format": "markdown"}, "text_format:markdown"),
            ({"text": "x" * 101}, "text_length"),
            (
                {"media": [{"type": "document", "url": "https://x/a.pdf"}]},
                "media_type:document",
            ),
            ({"media": [{"type": "image", "url": "https://x/a.jpg"}]}, "media_source:url"),
            (
                {"media": [{"type": "video", "url": f"https://x/{i}.mp4"} for i in range(3)]},
                "media_count",
            ),
        ],
    )
    async def test_unsupported_requests_are_refused_before_any_provider_call(
        self, harness: Harness, overrides: dict[str, object], capability: str
    ) -> None:
        response = await harness.submit(overrides)
        assert response.status_code == 422
        problem = response.json()
        assert problem["type"].endswith(":unsupported-capability")
        assert problem["capability"] == capability
        assert await harness.drain() == 0

    async def test_caption_limit_applies_when_media_is_attached(self, harness: Harness) -> None:
        body = {"text": "x" * 51, "media": [{"type": "video", "url": "https://x/a.mp4"}]}
        assert (await harness.submit(body)).json()["capability"] == "text_length"
        assert (await harness.submit(text="x" * 51)).status_code == 202

    async def test_upload_limits_come_from_the_account(self, harness: Harness) -> None:
        big = await upload(harness, b"x" * 1500)
        response = await harness.submit(media=[{"type": "image", "media_id": big}])
        assert response.json()["capability"] == "media_size"

        png = await upload(harness, b"png", "image/png")
        response = await harness.submit(media=[{"type": "image", "media_id": png}])
        assert response.json()["capability"] == "media_content_type"

    async def test_provider_specific_validation_is_reported(self, harness: Harness) -> None:
        def validate(account: object, request: object) -> None:
            raise PublishError(ErrorCategory.INVALID_REQUEST, "albums cannot mix documents")

        harness.provider.validate = validate  # type: ignore[method-assign]
        response = await harness.submit()
        assert response.status_code == 422
        assert response.json()["type"].endswith(":invalid-content")
        assert response.json()["detail"] == "albums cannot mix documents"


class TestMedia:
    async def test_upload_then_publish(self, harness: Harness) -> None:
        response = await harness.client.post(
            "/v1/media?filename=..%2Fphoto.jpg",
            content=b"\xff\xd8jpeg",
            headers={**harness.auth(), "content-type": "image/jpeg; charset=binary"},
        )
        assert response.status_code == 201
        media = response.json()
        assert media["size"] == 6
        assert media["content_type"] == "image/jpeg"
        assert media["filename"] == ".._photo.jpg"
        assert len(media["sha256"]) == 64
        assert harness.services.media.path(media["id"]).read_bytes() == b"\xff\xd8jpeg"

        submitted = await harness.submit(media=[{"type": "image", "media_id": media["id"]}])
        assert submitted.status_code == 202

    async def test_rejects_oversized_uploads_and_leaves_nothing_behind(
        self, harness: Harness
    ) -> None:
        async def body():  # type: ignore[no-untyped-def]
            for _ in range(5):  # no Content-Length, so the limit is enforced while streaming
                yield b"x" * 600

        response = await harness.client.post(
            "/v1/media", content=body(), headers={**harness.auth(), "content-type": "image/jpeg"}
        )
        assert response.status_code == 413
        assert list(harness.services.settings.media_dir.iterdir()) == []

    async def test_rejects_empty_and_untyped_uploads(self, harness: Harness) -> None:
        empty = await harness.client.post(
            "/v1/media", content=b"", headers={**harness.auth(), "content-type": "image/jpeg"}
        )
        assert empty.status_code == 400
        untyped = await harness.client.post("/v1/media", content=b"x", headers=harness.auth())
        assert untyped.status_code == 415

    async def test_media_is_private_to_the_uploading_consumer(self, harness: Harness) -> None:
        media_id = await upload(harness)
        other = harness.auth(OTHER_KEY)
        assert (await harness.client.get(f"/v1/media/{media_id}", headers=other)).status_code == 404
        response = await harness.client.post(
            "/v1/publications",
            json={"account": "main", "media": [{"type": "image", "media_id": media_id}]},
            headers={**other, "idempotency-key": "k"},
        )
        assert response.json()["type"].endswith(":unknown-media")

    async def test_expires_after_retention_unless_still_needed(self, harness: Harness) -> None:
        media = harness.services.media
        needed = await upload(harness)
        unused = await upload(harness)
        retention = harness.services.settings.media_retention_seconds
        media.keep_until([needed], harness.clock() + retention + 1000)

        harness.clock.advance(retention + 1)
        assert media.purge_expired() == 1
        assert not media.path(unused).exists()
        assert media.path(needed).exists()
        gone = await harness.client.get(f"/v1/media/{unused}", headers=harness.auth())
        assert gone.status_code == 404

        harness.clock.advance(1000)
        assert media.purge_expired() == 1
        assert not media.path(needed).exists()


class TestReading:
    async def test_publications_are_private_to_the_submitting_consumer(
        self, harness: Harness
    ) -> None:
        publication_id = (await harness.submit()).json()["id"]
        response = await harness.client.get(
            f"/v1/publications/{publication_id}", headers=harness.auth(OTHER_KEY)
        )
        assert response.status_code == 404
        assert response.headers["content-type"] == PROBLEM

    async def test_pending_publications_carry_a_polling_hint(self, harness: Harness) -> None:
        publication_id = (await harness.submit()).json()["id"]
        response = await harness.client.get(
            f"/v1/publications/{publication_id}", headers=harness.auth()
        )
        assert int(response.headers["retry-after"]) >= 1
        await harness.drain()
        response = await harness.client.get(
            f"/v1/publications/{publication_id}", headers=harness.auth()
        )
        assert "retry-after" not in response.headers

    async def test_accounts_expose_capabilities_but_not_configuration(
        self, harness: Harness
    ) -> None:
        response = await harness.client.get("/v1/accounts", headers=harness.auth())
        (account,) = response.json()
        assert account["id"] == "main" and account["platform"] == "fake"
        assert account["capabilities"]["kinds"]["post"]["max_media"] == 2
        assert set(account) == {
            "id",
            "platform",
            "capabilities",
            "credential_status",
            "credential_expires_at",
        }
        assert account["credential_status"] == "ok"
        assert "fake_token" not in response.text  # not even the secret's name

        missing = await harness.client.get("/v1/accounts/nope", headers=harness.auth())
        assert missing.status_code == 404

    async def test_request_ids_are_echoed_or_generated(self, harness: Harness) -> None:
        echoed = await harness.client.get("/livez", headers={"x-request-id": "abc-123"})
        assert echoed.headers["x-request-id"] == "abc-123"
        generated = await harness.client.get("/livez", headers={"x-request-id": "bad id\n"})
        assert len(generated.headers["x-request-id"]) == 32

    async def test_openapi_document_describes_the_contract(self, harness: Harness) -> None:
        document = (await harness.client.get("/openapi.json")).json()
        assert document["openapi"].startswith("3.1")
        assert {"/v1/publications", "/v1/media", "/v1/accounts"} <= set(document["paths"])
        assert "/readyz" not in document["paths"]
        upload_body = document["paths"]["/v1/media"]["post"]["requestBody"]["content"]
        assert upload_body == {"*/*": {"schema": {"type": "string", "format": "binary"}}}
