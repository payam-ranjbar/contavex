"""The Telegram Bot API implementation, end to end through the service,
against a faked api.telegram.org."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import httpx2
import pytest

from content_fleet.config import ConfigError, FleetConfig
from content_fleet.observability import configure_logging
from content_fleet.providers.registry import default_registry
from tests.conftest import Harness, HarnessFactory
from tests.fakes.telegram import CHAT_ID, TOKEN, FakeTelegram, check_against_spec

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

JPEG = b"\xff\xd8\xff\xe0 not really a jpeg"


def telegram_config(**settings: Any) -> FleetConfig:
    return FleetConfig.parse(
        {
            "consumers": {"tester": {"api_key": "consumer_key"}, "other": {"api_key": "other_key"}},
            "accounts": {
                "main": {
                    "platform": "telegram",
                    "implementation": "bot-api",
                    "credentials": {"token": "telegram_token"},
                    "settings": {"chat_id": str(CHAT_ID), **settings},
                }
            },
        }
    )


@pytest.fixture
def telegram() -> FakeTelegram:
    return FakeTelegram()


@pytest.fixture
async def h(harness_factory: HarnessFactory, telegram: FakeTelegram) -> Harness:
    return await harness_factory(
        config=telegram_config(),
        registry=default_registry(),
        transport=telegram.transport(),
        secrets={"telegram_token": TOKEN + "\n"},
        max_upload_bytes=100_000,
    )


async def upload(h: Harness, data: bytes = JPEG, content_type: str = "image/jpeg") -> str:
    response = await h.client.post(
        "/v1/media?filename=pic.jpg",
        content=data,
        headers={**h.auth(), "content-type": content_type},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]  # type: ignore[no-any-return]


async def publish(h: Harness, **body: Any) -> dict[str, Any]:
    """Submit, let the worker run, and return the publication."""
    response = await h.submit(body or None)
    assert response.status_code == 202, response.text
    await h.drain()
    return await h.status(response.json()["id"])


class TestPublishing:
    async def test_text_post(self, h: Harness, telegram: FakeTelegram) -> None:
        publication = await publish(h, text="Launch day.")

        assert publication["status"] == "published"
        assert publication["result"]["posts"] == [
            {"id": f"{CHAT_ID}:42", "url": "https://t.me/c/1234567890/42"}
        ]
        (call,) = telegram.calls
        assert call.method == "sendMessage"
        assert call.params == {"chat_id": str(CHAT_ID), "text": "Launch day."}

    async def test_public_channel_gets_a_public_link(
        self, h: Harness, telegram: FakeTelegram
    ) -> None:
        telegram.username = "fleet_news"
        publication = await publish(h, text="hi")
        assert publication["result"]["posts"][0]["url"] == "https://t.me/fleet_news/42"

    async def test_formatting_and_options(self, h: Harness, telegram: FakeTelegram) -> None:
        await publish(
            h,
            text="<b>Bold</b> news https://example.com",
            text_format="html",
            options={
                "telegram": {"disable_notification": True, "disable_link_preview": True},
                "instagram": {"share_to_feed": True},  # another platform's options: ignored
            },
        )
        assert telegram.calls[0].params == {
            "chat_id": str(CHAT_ID),
            "text": "<b>Bold</b> news https://example.com",
            "parse_mode": "HTML",
            "disable_notification": True,
            "link_preview_options": {"is_disabled": True},
        }

    async def test_uploaded_image_is_sent_as_multipart(
        self, h: Harness, telegram: FakeTelegram
    ) -> None:
        media_id = await upload(h)
        publication = await publish(
            h, text="Caption", media=[{"type": "image", "media_id": media_id}]
        )

        assert publication["status"] == "published"
        (call,) = telegram.calls
        assert call.method == "sendPhoto"
        assert call.params == {
            "chat_id": str(CHAT_ID),
            "caption": "Caption",
            "photo": "attach://photo",
        }
        assert call.files == {"photo": ("pic.jpg", "image/jpeg", JPEG)}

    @pytest.mark.parametrize(
        ("media_type", "method", "field"),
        [
            ("image", "sendPhoto", "photo"),
            ("video", "sendVideo", "video"),
            ("animation", "sendAnimation", "animation"),
        ],
    )
    async def test_media_by_url_is_passed_through(
        self, h: Harness, telegram: FakeTelegram, media_type: str, method: str, field: str
    ) -> None:
        await publish(h, media=[{"type": media_type, "url": "https://cdn.example.com/a.bin"}])
        (call,) = telegram.calls
        assert call.method == method
        assert call.params == {"chat_id": str(CHAT_ID), field: "https://cdn.example.com/a.bin"}

    async def test_document_upload(self, h: Harness, telegram: FakeTelegram) -> None:
        media_id = await upload(h, b"%PDF-1.7", "application/pdf")
        await publish(h, media=[{"type": "document", "media_id": media_id}])
        assert telegram.calls[0].method == "sendDocument"
        assert telegram.calls[0].files["document"][2] == b"%PDF-1.7"

    async def test_album_is_one_post_per_item_with_the_caption_on_the_first(
        self, h: Harness, telegram: FakeTelegram
    ) -> None:
        first = await upload(h)
        publication = await publish(
            h,
            text="Three pictures",
            media=[
                {"type": "image", "media_id": first},
                {"type": "image", "url": "https://cdn.example.com/b.jpg"},
                {"type": "video", "url": "https://cdn.example.com/c.mp4"},
            ],
        )

        assert [post["id"] for post in publication["result"]["posts"]] == [
            f"{CHAT_ID}:42",
            f"{CHAT_ID}:43",
            f"{CHAT_ID}:44",
        ]
        (call,) = telegram.calls
        assert call.method == "sendMediaGroup"
        assert json.loads(call.params["media"]) == [
            {"type": "photo", "media": "attach://file0", "caption": "Three pictures"},
            {"type": "photo", "media": "https://cdn.example.com/b.jpg"},
            {"type": "video", "media": "https://cdn.example.com/c.mp4"},
        ]
        assert list(call.files) == ["file0"]

    async def test_self_scheduled_post_has_no_handle(
        self, h: Harness, telegram: FakeTelegram
    ) -> None:
        telegram.next_message_id = 0  # Telegram's signal that it scheduled a large video
        publication = await publish(h, text="x")
        assert publication["status"] == "published"
        assert publication["result"]["posts"] == []


class TestValidation:
    @pytest.mark.parametrize(
        ("body", "problem", "detail"),
        [
            ({"text": "x" * 4097}, "unsupported-capability", "limit is 4096"),
            (
                {"text": "x" * 1025, "media": [{"type": "image", "url": "https://x/a.jpg"}]},
                "unsupported-capability",
                "limit is 1024",
            ),
            (
                {"media": [{"type": "image", "url": f"https://x/{i}.jpg"} for i in range(11)]},
                "unsupported-capability",
                "0 to 10 media items",
            ),
            (
                {"media": [{"type": "document", "url": "https://x/a.pdf"}]},
                "unsupported-capability",
                "cannot be supplied by url",
            ),
            ({"kind": "story", "text": "x"}, "unsupported-capability", "kind 'story'"),
            ({"text": "x", "text_format": "markdown"}, "unsupported-capability", "text_format"),
            (
                {
                    "media": [
                        {"type": "image", "url": "https://x/a.jpg"},
                        {"type": "animation", "url": "https://x/b.gif"},
                    ]
                },
                "unsupported-capability",
                "animation cannot be part of an album",
            ),
            (
                {"text": "x", "options": {"telegram": {"silent": True}}},
                "invalid-content",
                "options.telegram: silent",
            ),
        ],
    )
    async def test_refused_before_telegram_is_called(
        self, h: Harness, telegram: FakeTelegram, body: dict[str, Any], problem: str, detail: str
    ) -> None:
        response = await h.client.post(
            "/v1/publications",
            json={"account": "main", **body},
            headers={**h.auth(), "idempotency-key": "k"},
        )
        assert response.status_code == 422, response.text
        assert response.json()["type"].endswith(f":{problem}")
        assert detail in response.json()["detail"]
        assert telegram.calls == []

    async def test_documents_and_other_media_cannot_share_an_album(self, h: Harness) -> None:
        pdf = await upload(h, b"%PDF", "application/pdf")
        response = await h.submit(
            media=[{"type": "document", "media_id": pdf}, {"type": "image", "url": "https://x/a"}]
        )
        assert response.status_code == 422
        assert "only be grouped in an album with other documents" in response.json()["detail"]

    async def test_oversized_photo_is_refused(self, h: Harness) -> None:
        caps = (await h.client.get("/v1/accounts/main", headers=h.auth())).json()["capabilities"]
        assert caps["media"]["image"]["max_bytes"] == 10 * 1024 * 1024
        assert caps["media"]["document"]["sources"] == ["upload"]
        assert caps["editable"] == ["text"] and caps["deletable"] is True
        assert "disable_notification" in caps["options_schema"]["properties"]

    @pytest.mark.parametrize(
        "account",
        [
            {"credentials": {"token": "telegram_token"}, "settings": {}},
            {"credentials": {}, "settings": {"chat_id": "1"}},
            {"credentials": {"token": "telegram_token"}, "settings": {"chat_id": "1", "x": 1}},
        ],
    )
    async def test_misconfigured_account_stops_startup(
        self, harness_factory: HarnessFactory, account: dict[str, Any]
    ) -> None:
        config = FleetConfig.parse(
            {
                "consumers": {"tester": {"api_key": "consumer_key"}},
                "accounts": {
                    "main": {"platform": "telegram", "implementation": "bot-api"} | account
                },
            }
        )
        with pytest.raises(ConfigError, match="account 'main'"):
            await harness_factory(
                config=config, registry=default_registry(), secrets={"telegram_token": TOKEN}
            )


class TestFailures:
    @pytest.mark.parametrize(
        ("code", "description", "category"),
        [
            (401, "Unauthorized", "authentication"),
            (403, "Forbidden: bot was kicked from the channel chat", "authorization"),
            (400, "Bad Request: not enough rights to send text messages", "authorization"),
            (400, "Bad Request: need administrator rights in the channel chat", "authorization"),
            (400, "Bad Request: chat not found", "invalid_request"),
            (400, "Bad Request: can't parse entities: unclosed tag", "invalid_request"),
            (400, "Bad Request: some description nobody has seen before", "invalid_request"),
            (413, "Request Entity Too Large", "media_rejected"),
            (404, "Not Found: method not found", "provider_rejected"),
        ],
    )
    async def test_permanent_errors_fail_once(
        self, h: Harness, telegram: FakeTelegram, code: int, description: str, category: str
    ) -> None:
        telegram.reply(telegram.error(code, description))
        publication = await publish(h, text="x")
        h.clock.advance(3600)
        await h.drain()

        assert publication["status"] == "failed"
        assert publication["error"] == {
            "category": category,
            "retryable": False,
            "message": f"Telegram: {description}",
            "provider_code": str(code),
            "provider_request_id": None,
        }
        assert len(telegram.calls) == 1

    async def test_rate_limit_waits_for_retry_after_then_publishes(
        self, h: Harness, telegram: FakeTelegram
    ) -> None:
        telegram.reply(telegram.error(429, "Too Many Requests: retry after 35", retry_after=35))
        publication = await publish(h, text="x")

        assert publication["status"] == "processing"
        assert publication["error"]["category"] == "rate_limited"
        h.clock.advance(34)
        assert await h.drain() == 0
        h.clock.advance(2)
        await h.drain()
        assert (await h.status(publication["id"]))["status"] == "published"
        assert telegram.methods() == ["sendMessage", "sendMessage"]

    async def test_connection_never_made_is_safe_to_retry(
        self, h: Harness, telegram: FakeTelegram
    ) -> None:
        telegram.reply(httpx2.ConnectError("dns"), httpx2.ConnectTimeout("slow"))
        publication = await publish(h, text="x")
        assert publication["status"] == "processing"
        assert publication["error"]["category"] == "provider_unavailable"
        for _ in range(3):
            h.clock.advance(30)
            await h.drain()
        assert (await h.status(publication["id"]))["status"] == "published"
        assert len(telegram.calls) == 3

    @pytest.mark.parametrize(
        "reply",
        [
            httpx2.ReadTimeout("no answer"),
            httpx2.WriteTimeout("stalled upload"),
            httpx2.RemoteProtocolError("connection reset"),
            FakeTelegram.error(500, "Internal Server Error"),
            httpx2.Response(502, text="<html>Bad Gateway</html>"),
            httpx2.Response(200, text="<html>not json</html>"),
            httpx2.Response(200, json={"ok": True}),
            httpx2.Response(200, json={"ok": True, "result": {"unexpected": "shape"}}),
        ],
        ids=[
            "read-timeout",
            "write-timeout",
            "reset",
            "500",
            "502-html",
            "200-html",
            "ok-no-result",
            "ok-odd-result",
        ],
    )
    async def test_a_send_that_may_have_happened_is_never_repeated(
        self, h: Harness, telegram: FakeTelegram, reply: httpx2.Response | Exception
    ) -> None:
        telegram.reply(reply)
        publication = await publish(h, text="x")
        h.clock.advance(3600)
        await h.drain()

        assert publication["status"] == "outcome_unknown"
        assert publication["next_attempt_at"] is None
        assert len(telegram.calls) == 1

    async def test_unfetchable_media_url_is_retried_then_fails(
        self, h: Harness, telegram: FakeTelegram
    ) -> None:
        failure = "Bad Request: failed to get HTTP URL content"
        telegram.reply(*(telegram.error(400, failure) for _ in range(5)))
        publication = await publish(h, media=[{"type": "image", "url": "https://down.example/a"}])
        for _ in range(5):
            h.clock.advance(60)
            await h.drain()

        publication = await h.status(publication["id"])
        assert publication["status"] == "failed"
        assert publication["error"]["category"] == "media_unreachable"
        assert publication["error"]["retryable"] is True
        assert len(telegram.calls) == h.services.settings.max_attempts

    async def test_uploaded_media_that_vanished_fails_cleanly(
        self, h: Harness, telegram: FakeTelegram
    ) -> None:
        media_id = await upload(h)
        response = await h.submit(media=[{"type": "image", "media_id": media_id}])
        h.services.db.pop_expired_media(h.clock() + 10**9)
        await h.drain()

        publication = await h.status(response.json()["id"])
        assert publication["status"] == "failed"
        assert publication["error"]["category"] == "media_unreachable"
        assert publication["error"]["retryable"] is False
        assert telegram.calls == []


class TestEditAndDelete:
    async def test_edit_text_post(self, h: Harness, telegram: FakeTelegram) -> None:
        publication = await publish(h, text="first")
        response = await h.client.patch(
            f"/v1/publications/{publication['id']}",
            json={"text": "<i>second</i>", "text_format": "html"},
            headers=h.auth(),
        )
        assert response.status_code == 200, response.text
        assert telegram.calls[-1].method == "editMessageText"
        assert telegram.calls[-1].params == {
            "chat_id": CHAT_ID,
            "message_id": 42,
            "text": "<i>second</i>",
            "parse_mode": "HTML",
        }

    async def test_edit_of_a_media_post_changes_the_caption(
        self, h: Harness, telegram: FakeTelegram
    ) -> None:
        publication = await publish(
            h, text="old", media=[{"type": "image", "url": "https://x/a.jpg"}]
        )
        too_long = await h.client.patch(
            f"/v1/publications/{publication['id']}", json={"text": "x" * 1025}, headers=h.auth()
        )
        assert too_long.status_code == 422 and too_long.json()["capability"] == "text_length"

        response = await h.client.patch(
            f"/v1/publications/{publication['id']}", json={"text": "new"}, headers=h.auth()
        )
        assert response.status_code == 200
        assert telegram.calls[-1].method == "editMessageCaption"
        assert telegram.calls[-1].params["caption"] == "new"

    async def test_repeating_an_identical_edit_succeeds(
        self, h: Harness, telegram: FakeTelegram
    ) -> None:
        publication = await publish(h, text="same")
        telegram.reply(telegram.error(400, "Bad Request: message is not modified: ..."))
        response = await h.client.patch(
            f"/v1/publications/{publication['id']}", json={"text": "same"}, headers=h.auth()
        )
        assert response.status_code == 200

    async def test_delete_removes_every_message_of_an_album(
        self, h: Harness, telegram: FakeTelegram
    ) -> None:
        publication = await publish(
            h, media=[{"type": "image", "url": f"https://x/{i}.jpg"} for i in range(2)]
        )
        response = await h.client.delete(f"/v1/publications/{publication['id']}", headers=h.auth())

        assert response.status_code == 200, response.text
        assert response.json()["status"] == "deleted"
        assert response.json()["result"]["posts"]  # the record keeps what was published
        assert telegram.calls[-1].method == "deleteMessages"
        assert telegram.calls[-1].params == {"chat_id": CHAT_ID, "message_ids": [42, 43]}

        again = await h.client.delete(f"/v1/publications/{publication['id']}", headers=h.auth())
        assert again.status_code == 200
        assert telegram.methods().count("deleteMessages") == 1

    async def test_deleting_an_already_gone_message_succeeds(
        self, h: Harness, telegram: FakeTelegram
    ) -> None:
        publication = await publish(h, text="x")
        telegram.reply(telegram.error(400, "Bad Request: message to delete not found"))
        response = await h.client.delete(f"/v1/publications/{publication['id']}", headers=h.auth())
        assert response.json()["status"] == "deleted"

    @pytest.mark.parametrize(
        ("reply", "status", "category", "retryable"),
        [
            (FakeTelegram.error(400, "Bad Request: message can't be deleted"), 422, None, None),
            (FakeTelegram.error(401, "Unauthorized"), 502, "authentication", False),
            (FakeTelegram.error(500, "Internal Server Error"), 503, "provider_unavailable", True),
            (httpx2.ReadTimeout("no answer"), 503, "timeout", True),
            (
                FakeTelegram.error(429, "Too Many Requests: retry after 7", retry_after=7),
                503,
                "rate_limited",
                True,
            ),
        ],
        ids=["too-old", "unauthorized", "500", "timeout", "rate-limited"],
    )
    async def test_failed_delete_leaves_the_publication_published(
        self,
        h: Harness,
        telegram: FakeTelegram,
        reply: httpx2.Response | Exception,
        status: int,
        category: str | None,
        retryable: bool | None,
    ) -> None:
        publication = await publish(h, text="x")
        telegram.reply(reply)
        response = await h.client.delete(f"/v1/publications/{publication['id']}", headers=h.auth())

        assert response.status_code == status, response.text
        assert response.headers["content-type"] == "application/problem+json"
        assert response.json().get("category") == category
        assert response.json().get("retryable") == retryable
        assert (await h.status(publication["id"]))["status"] == "published"

    async def test_only_published_publications_can_be_changed(
        self, h: Harness, telegram: FakeTelegram
    ) -> None:
        telegram.reply(telegram.error(401, "Unauthorized"))
        publication = await publish(h, text="x")
        for method, kwargs in [("PATCH", {"json": {"text": "y"}}), ("DELETE", {})]:
            response = await h.client.request(
                method, f"/v1/publications/{publication['id']}", headers=h.auth(), **kwargs
            )
            assert response.status_code == 409
        assert len(telegram.calls) == 1


async def test_bot_token_never_leaks(
    h: Harness, telegram: FakeTelegram, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("DEBUG")
    echo = f"Unauthorized for https://api.telegram.org/bot{TOKEN}/sendMessage"
    telegram.reply(
        telegram.error(500, echo),
        telegram.error(401, echo),
        httpx2.ReadTimeout(echo),
        telegram.error(429, echo, retry_after=1),
    )
    responses = []
    for key in "abcde":
        responses.append(await h.submit(key=key))
    for _ in range(3):
        await h.drain()
        h.clock.advance(60)
    published = (await h.status(responses[-1].json()["id"]))["id"]
    telegram.reply(telegram.error(401, echo))
    responses.append(await h.client.delete(f"/v1/publications/{published}", headers=h.auth()))
    for response in list(responses[:5]):
        responses.append(
            await h.client.get(f"/v1/publications/{response.json()['id']}", headers=h.auth())
        )
    responses.append(await h.client.get("/v1/accounts", headers=h.auth()))
    responses.append(await h.client.get("/metrics"))

    logs = capsys.readouterr()
    assert '"event": "telegram_request"' in logs.out
    database = sqlite3.connect(h.services.settings.db_path)
    stored = "\n".join(str(row) for row in database.execute("SELECT * FROM publications"))
    database.close()
    secret_part = TOKEN.split(":")[1]
    for where, text in {
        "logs": logs.out + logs.err,
        "responses": "\n".join(r.text + str(r.headers) for r in responses),
        "database": stored,
    }.items():
        assert secret_part not in text, f"bot token leaked into {where}"


async def test_test_environment_uses_telegrams_test_servers(
    harness_factory: HarnessFactory, telegram: FakeTelegram
) -> None:
    telegram.test_environment = True  # the fake now only answers /bot<token>/test/METHOD
    h = await harness_factory(
        config=telegram_config(test_environment=True),
        transport=telegram.transport(),
        secrets={"telegram_token": TOKEN},
    )
    assert (await publish(h, text="to the test servers"))["status"] == "published"


class TestRequestsMatchTheBotApi:
    """Every call in this file is checked against the Bot API field list by
    the fake. These confirm that the check itself has teeth."""

    def test_known_good_calls_pass(self) -> None:
        check_against_spec("sendMessage", {"chat_id": 1, "text": "x"}, set())
        check_against_spec("sendPhoto", {"chat_id": 1, "photo": "attach://photo"}, {"photo"})

    @pytest.mark.parametrize(
        ("method", "params", "files", "problem"),
        [
            ("sendMesage", {"chat_id": 1, "text": "x"}, set(), "not a Bot API"),
            ("sendMessage", {"chat_id": 1}, set(), "required parameters missing"),
            (
                "sendMessage",
                {"chat_id": 1, "text": "x", "silent": True},
                set(),
                "not in the Bot API",
            ),
            ("sendPhoto", {"chat_id": 1, "photo": "attach://a"}, {"b"}, "unreferenced uploads"),
        ],
    )
    def test_invalid_calls_are_caught(
        self, method: str, params: dict[str, Any], files: set[str], problem: str
    ) -> None:
        with pytest.raises(AssertionError, match=problem):
            check_against_spec(method, params, files)
