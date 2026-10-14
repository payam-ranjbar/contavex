"""Behaviour every provider implementation must show to a consumer.

Each implementation is exercised through the public API against a fake of
its vendor. A new implementation joins by adding a `Driver` to `DRIVERS`;
it then has to pass every test here for each capability it declares. This is
what keeps "one platform, several implementations" interchangeable, and it
is why none of these tests mentions a vendor.
"""

from __future__ import annotations

import base64
import json
from abc import ABC, abstractmethod
from typing import Any

import httpx2
import pytest

from contavex.config import ContavexConfig
from tests.conftest import Harness, HarnessFactory
from tests.fakes import instagram as ig
from tests.fakes import linkedin as li
from tests.fakes import telegram as tg

pytestmark = [pytest.mark.contract, pytest.mark.anyio]


class Driver(ABC):
    """Scripts one vendor fake in vendor-neutral terms."""

    platform: str
    implementation: str
    account_settings: dict[str, Any]
    credentials: dict[str, str]  # role -> secret name
    secrets: dict[str, str]  # secret name -> value
    credential: str  # the vendor credential value, to check it never leaks
    extra_config: dict[str, Any] = {}  # noqa: RUF012

    @abstractmethod
    def transport(self) -> httpx2.AsyncBaseTransport: ...

    @abstractmethod
    def body(self, text: str) -> dict[str, Any]:
        """The simplest publication this implementation supports."""

    @abstractmethod
    def vendor_calls(self) -> int: ...

    @abstractmethod
    def posts_at_vendor(self) -> int:
        """How many posts the vendor actually holds."""

    @abstractmethod
    def reject_credentials(self) -> None: ...

    @abstractmethod
    def rate_limit(self, seconds: int) -> None: ...

    @abstractmethod
    def unreachable(self) -> None: ...

    @abstractmethod
    def reject_content(self) -> None: ...


class TelegramBotApiDriver(Driver):
    platform = "telegram"
    implementation = "bot-api"
    account_settings = {"chat_id": str(tg.CHAT_ID)}  # noqa: RUF012
    credentials = {"token": "vendor_credential"}  # noqa: RUF012
    secrets = {"vendor_credential": tg.TOKEN}  # noqa: RUF012
    credential = tg.TOKEN.split(":")[1]

    def __init__(self) -> None:
        self.fake = tg.FakeTelegram()

    def transport(self) -> httpx2.AsyncBaseTransport:
        return self.fake.transport()

    def body(self, text: str) -> dict[str, Any]:
        return {"text": text}

    def vendor_calls(self) -> int:
        return len(self.fake.calls)

    def posts_at_vendor(self) -> int:
        return self.fake.posts

    def reject_credentials(self) -> None:
        self.fake.reply(self.fake.error(401, "Unauthorized"))

    def rate_limit(self, seconds: int) -> None:
        self.fake.reply(self.fake.error(429, "Too Many Requests", retry_after=seconds))

    def unreachable(self) -> None:
        self.fake.reply(httpx2.ConnectError("unreachable"))

    def reject_content(self) -> None:
        self.fake.reply(self.fake.error(400, "Bad Request: can't parse entities"))


class InstagramLoginDriver(Driver):
    platform = "instagram"
    implementation = "instagram-login"
    account_settings = {"ig_user_id": ig.IG_USER_ID}  # noqa: RUF012
    credentials = {"access_token": "vendor_credential"}  # noqa: RUF012
    secrets = {  # noqa: RUF012
        "vendor_credential": ig.TOKEN,
        "master_key": base64.b64encode(bytes(range(32))).decode(),
    }
    credential = ig.TOKEN
    extra_config = {"service": {"master_key": "master_key"}}  # noqa: RUF012

    def __init__(self) -> None:
        self.fake = ig.FakeInstagram()

    def transport(self) -> httpx2.AsyncBaseTransport:
        return self.fake.transport()

    def body(self, text: str) -> dict[str, Any]:
        return {"text": text, "media": [{"type": "image", "url": "https://cdn.example/a.jpg"}]}

    def vendor_calls(self) -> int:
        return len(self.fake.calls)

    def posts_at_vendor(self) -> int:
        return len(self.fake.media)

    def reject_credentials(self) -> None:
        self.fake.plan("create_container", ig.graph_error(190, "Token expired", subcode=463))

    def rate_limit(self, seconds: int) -> None:
        usage = {ig.IG_USER_ID: [{"estimated_time_to_regain_access": seconds / 60}]}
        response = ig.graph_error(80002, "Too many calls")
        response.headers["x-business-use-case-usage"] = json.dumps(usage)
        self.fake.plan("create_container", response)

    def unreachable(self) -> None:
        self.fake.plan("create_container", httpx2.ConnectError("unreachable"))

    def reject_content(self) -> None:
        self.fake.plan("create_container", ig.graph_error(100, "Invalid parameter"))


class InstagramFacebookLoginDriver(InstagramLoginDriver):
    """The same platform through its other implementation: the whole suite
    must hold for it too, with `deletable` now true."""

    implementation = "facebook-login"
    secrets = {"vendor_credential": ig.TOKEN}  # noqa: RUF012
    extra_config: dict[str, Any] = {}  # noqa: RUF012

    def __init__(self) -> None:
        self.fake = ig.FakeInstagram(host="graph.facebook.com")


class LinkedInRestDriver(Driver):
    platform = "linkedin"
    implementation = "rest"
    account_settings = {"author": li.ORGANIZATION}  # noqa: RUF012
    credentials = {"access_token": "vendor_credential"}  # noqa: RUF012
    secrets = {  # noqa: RUF012
        "vendor_credential": li.TOKEN,
        "master_key": base64.b64encode(bytes(range(32))).decode(),
    }
    credential = li.TOKEN
    extra_config = {"service": {"master_key": "master_key"}}  # noqa: RUF012

    def __init__(self) -> None:
        self.fake = li.FakeLinkedIn()

    def transport(self) -> httpx2.AsyncBaseTransport:
        return self.fake.transport()

    def body(self, text: str) -> dict[str, Any]:
        return {"text": text}

    def vendor_calls(self) -> int:
        return len(self.fake.calls)

    def posts_at_vendor(self) -> int:
        return len(self.fake.posts)

    def reject_credentials(self) -> None:
        self.fake.plan("create_post", li.li_error(401, "The token has been revoked"))

    def rate_limit(self, seconds: int) -> None:
        response = li.li_error(429, "Resource level throttle limit reached")
        response.headers["retry-after"] = str(seconds)
        self.fake.plan("create_post", response)

    def unreachable(self) -> None:
        self.fake.plan("create_post", httpx2.ConnectError("unreachable"))

    def reject_content(self) -> None:
        self.fake.plan("create_post", li.li_error(400, "Invalid value", "INVALID_VALUE_FOR_FIELD"))


DRIVERS: list[type[Driver]] = [
    TelegramBotApiDriver,
    InstagramLoginDriver,
    InstagramFacebookLoginDriver,
    LinkedInRestDriver,
]


@pytest.fixture(params=DRIVERS, ids=lambda d: f"{d.platform}/{d.implementation}")
def driver(request: pytest.FixtureRequest) -> Driver:
    return request.param()  # type: ignore[no-any-return]


@pytest.fixture
async def h(harness_factory: HarnessFactory, driver: Driver) -> Harness:
    config = ContavexConfig.parse(
        {
            "consumers": {"tester": {"api_key": "consumer_key"}},
            "accounts": {
                "main": {
                    "platform": driver.platform,
                    "implementation": driver.implementation,
                    "credentials": driver.credentials,
                    "settings": driver.account_settings,
                }
            },
            **driver.extra_config,
        }
    )
    return await harness_factory(
        config=config, transport=driver.transport(), secrets=driver.secrets
    )


async def capabilities(h: Harness) -> dict[str, Any]:
    response = await h.client.get("/v1/accounts/main", headers=h.auth())
    assert response.status_code == 200
    return response.json()["capabilities"]  # type: ignore[no-any-return]


async def settle(h: Harness, publication_id: str, *, rounds: int = 60) -> dict[str, Any]:
    """Let the worker run through polls and backoffs until the outcome is final."""
    for _ in range(rounds):
        await h.drain()
        status = await h.status(publication_id)
        if status["next_attempt_at"] is None:
            return status
        h.clock.advance(60)
    raise AssertionError("publication did not settle")


async def publish(h: Harness, driver: Driver, key: str = "k1") -> dict[str, Any]:
    response = await h.submit(driver.body("contract test"), key=key)
    assert response.status_code == 202, response.text
    return await settle(h, response.json()["id"])


async def test_capabilities_are_self_consistent(h: Harness) -> None:
    caps = await capabilities(h)
    assert caps["kinds"], "an account must accept at least one kind"
    assert "plain" in caps["text_formats"]
    for name, kind in caps["kinds"].items():
        assert kind["min_media"] <= kind["max_media"], name
        for media_type in kind["media_types"]:
            assert caps["media"][media_type]["sources"], f"{name}: no way to supply {media_type}"


async def test_submission_is_accepted_before_the_vendor_is_called(
    h: Harness, driver: Driver
) -> None:
    response = await h.submit(driver.body("contract test"))
    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    assert driver.vendor_calls() == 0


async def test_publication_ends_published_with_a_post_id(h: Harness, driver: Driver) -> None:
    publication = await publish(h, driver)
    assert publication["status"] == "published"
    assert publication["error"] is None
    assert publication["result"]["posts"] and publication["result"]["posts"][0]["id"]
    assert driver.posts_at_vendor() == 1


async def test_retrying_the_same_request_does_not_publish_twice(h: Harness, driver: Driver) -> None:
    first = await publish(h, driver, key="same")
    calls = driver.vendor_calls()
    again = await h.submit(driver.body("contract test"), key="same")
    await h.drain()
    assert again.json()["id"] == first["id"]
    assert driver.vendor_calls() == calls
    assert driver.posts_at_vendor() == 1


async def test_text_over_the_declared_limit_is_refused_up_front(h: Harness, driver: Driver) -> None:
    caps = await capabilities(h)
    body = driver.body("x")
    limit = caps["max_caption_length" if body.get("media") else "max_text_length"]
    assert limit is not None, "implementations must declare the limit for their default post"
    response = await h.submit(driver.body("x" * (limit + 1)))
    assert response.status_code == 422
    assert response.json()["type"].endswith(":unsupported-capability")
    assert driver.vendor_calls() == 0


async def test_unsupported_kind_is_refused_up_front(h: Harness, driver: Driver) -> None:
    response = await h.submit(driver.body("x") | {"kind": "no-such-kind"})
    assert response.status_code == 422
    assert response.json()["capability"] == "kind:no-such-kind"
    assert driver.vendor_calls() == 0


async def test_rejected_credentials_fail_permanently(h: Harness, driver: Driver) -> None:
    driver.reject_credentials()
    publication = await publish(h, driver)
    assert publication["status"] == "failed"
    assert publication["error"]["category"] == "authentication"
    assert publication["error"]["retryable"] is False
    assert driver.posts_at_vendor() == 0


async def test_rejected_credentials_take_the_account_out_of_service(
    h: Harness, driver: Driver
) -> None:
    driver.reject_credentials()
    await publish(h, driver, key="first")
    calls = driver.vendor_calls()

    account = (await h.client.get("/v1/accounts/main", headers=h.auth())).json()
    assert account["credential_status"] == "reauthorization_required"
    refused = await h.submit(driver.body("contract test"), key="second")
    assert refused.status_code == 409
    assert refused.json()["type"].endswith(":account-unavailable")
    assert driver.vendor_calls() == calls


async def test_rejected_content_fails_permanently(h: Harness, driver: Driver) -> None:
    driver.reject_content()
    publication = await publish(h, driver)
    assert publication["status"] == "failed"
    assert publication["error"]["retryable"] is False
    calls = driver.vendor_calls()
    h.clock.advance(3600)
    await h.drain()
    assert driver.vendor_calls() == calls


async def test_rate_limit_is_waited_out_not_failed(h: Harness, driver: Driver) -> None:
    driver.rate_limit(600)
    response = await h.submit(driver.body("contract test"))
    publication_id = response.json()["id"]
    for _ in range(5):  # reach the throttled call, whatever steps come before it
        await h.drain()
        waiting = await h.status(publication_id)
        if waiting["error"]:
            break
        h.clock.advance(5)
    assert waiting["status"] == "processing"
    assert waiting["error"]["category"] == "rate_limited"

    calls = driver.vendor_calls()
    h.clock.advance(590)
    await h.drain()
    assert driver.vendor_calls() == calls, "retried before the vendor's hint elapsed"

    h.clock.advance(15)
    assert (await settle(h, publication_id))["status"] == "published"
    assert driver.posts_at_vendor() == 1


async def test_unreachable_vendor_is_retried(h: Harness, driver: Driver) -> None:
    driver.unreachable()
    publication = await publish(h, driver)
    assert publication["status"] == "published"
    assert driver.posts_at_vendor() == 1


async def test_delete_matches_the_declared_capability(h: Harness, driver: Driver) -> None:
    publication = await publish(h, driver)
    response = await h.client.delete(f"/v1/publications/{publication['id']}", headers=h.auth())
    if (await capabilities(h))["deletable"]:
        assert response.status_code == 200
        assert response.json()["status"] == "deleted"
    else:
        assert response.status_code == 422
        assert response.json()["capability"] == "delete"
        assert (await h.status(publication["id"]))["status"] == "published"


async def test_edit_matches_the_declared_capability(h: Harness, driver: Driver) -> None:
    publication = await publish(h, driver)
    response = await h.client.patch(
        f"/v1/publications/{publication['id']}", json={"text": "edited"}, headers=h.auth()
    )
    if "text" in (await capabilities(h))["editable"]:
        assert response.status_code == 200
    else:
        assert response.status_code == 422
        assert response.json()["capability"] == "edit:text"


async def test_vendor_credential_is_not_visible_to_consumers(h: Harness, driver: Driver) -> None:
    ok = await publish(h, driver, key="b")
    driver.reject_credentials()
    failed = await publish(h, driver, key="a")
    accounts = await h.client.get("/v1/accounts", headers=h.auth())
    assert driver.credential not in f"{failed} {ok} {accounts.text}"
