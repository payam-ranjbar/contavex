"""Behaviour every provider implementation must show to a consumer.

Each implementation is exercised through the public API against a fake of
its vendor. A new implementation joins by adding a `Driver` to `DRIVERS`;
it then has to pass every test here for each capability it declares. This is
what keeps "one platform, several implementations" interchangeable.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import httpx2
import pytest

from content_fleet.config import FleetConfig
from content_fleet.providers.registry import default_registry
from tests.conftest import Harness, HarnessFactory
from tests.fakes.telegram import CHAT_ID, TOKEN, FakeTelegram

pytestmark = [pytest.mark.contract, pytest.mark.anyio]


class Driver(ABC):
    """Scripts one vendor fake in vendor-neutral terms."""

    platform: str
    implementation: str
    account_settings: dict[str, Any]
    secrets: dict[str, str]  # secret name -> value; the first is the account credential
    credentials: dict[str, str]  # role -> secret name

    @abstractmethod
    def transport(self) -> httpx2.AsyncBaseTransport: ...

    @abstractmethod
    def vendor_calls(self) -> int: ...

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
    account_settings = {"chat_id": str(CHAT_ID)}  # noqa: RUF012
    secrets = {"vendor_credential": TOKEN}  # noqa: RUF012
    credentials = {"token": "vendor_credential"}  # noqa: RUF012

    def __init__(self) -> None:
        self.fake = FakeTelegram()

    def transport(self) -> httpx2.AsyncBaseTransport:
        return self.fake.transport()

    def vendor_calls(self) -> int:
        return len(self.fake.calls)

    def reject_credentials(self) -> None:
        self.fake.reply(self.fake.error(401, "Unauthorized"))

    def rate_limit(self, seconds: int) -> None:
        self.fake.reply(self.fake.error(429, "Too Many Requests", retry_after=seconds))

    def unreachable(self) -> None:
        self.fake.reply(httpx2.ConnectError("unreachable"))

    def reject_content(self) -> None:
        self.fake.reply(self.fake.error(400, "Bad Request: can't parse entities"))


DRIVERS: list[type[Driver]] = [TelegramBotApiDriver]


@pytest.fixture(params=DRIVERS, ids=lambda d: f"{d.platform}/{d.implementation}")
def driver(request: pytest.FixtureRequest) -> Driver:
    return request.param()  # type: ignore[no-any-return]


@pytest.fixture
async def h(harness_factory: HarnessFactory, driver: Driver) -> Harness:
    config = FleetConfig.parse(
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
        }
    )
    return await harness_factory(
        config=config,
        registry=default_registry(),
        transport=driver.transport(),
        secrets=driver.secrets,
    )


async def capabilities(h: Harness) -> dict[str, Any]:
    response = await h.client.get("/v1/accounts/main", headers=h.auth())
    assert response.status_code == 200
    return response.json()["capabilities"]  # type: ignore[no-any-return]


async def settle(h: Harness, publication_id: str, *, minutes: int = 30) -> dict[str, Any]:
    """Let the worker run through polls and backoffs until the outcome is final."""
    for _ in range(minutes):
        await h.drain()
        status = await h.status(publication_id)
        if status["next_attempt_at"] is None:
            return status
        h.clock.advance(60)
    raise AssertionError("publication did not settle")


async def publish_text(h: Harness, text: str = "contract test", key: str = "k1") -> dict[str, Any]:
    response = await h.submit(text=text, key=key)
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
    response = await h.submit(text="contract test")
    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    assert driver.vendor_calls() == 0


async def test_text_post_is_published_with_a_post_id(h: Harness) -> None:
    caps = await capabilities(h)
    if caps["kinds"].get("post", {}).get("min_media", 1) > 0:
        pytest.skip("this account cannot publish text-only posts")
    publication = await publish_text(h)
    assert publication["status"] == "published"
    assert publication["error"] is None
    assert publication["result"]["posts"] and publication["result"]["posts"][0]["id"]


async def test_retrying_the_same_request_does_not_publish_twice(h: Harness, driver: Driver) -> None:
    first = await publish_text(h, key="same")
    calls = driver.vendor_calls()
    again = await h.submit(text="contract test", key="same")
    await h.drain()
    assert again.json()["id"] == first["id"]
    assert driver.vendor_calls() == calls


async def test_text_over_the_declared_limit_is_refused_up_front(h: Harness, driver: Driver) -> None:
    limit = (await capabilities(h))["max_text_length"]
    if limit is None:
        pytest.skip("no declared text limit")
    response = await h.submit(text="x" * (limit + 1))
    assert response.status_code == 422
    assert response.json()["type"].endswith(":unsupported-capability")
    assert driver.vendor_calls() == 0


async def test_rejected_credentials_fail_permanently(h: Harness, driver: Driver) -> None:
    driver.reject_credentials()
    publication = await publish_text(h)
    assert publication["status"] == "failed"
    assert publication["error"]["category"] == "authentication"
    assert publication["error"]["retryable"] is False


async def test_rejected_content_fails_permanently(h: Harness, driver: Driver) -> None:
    driver.reject_content()
    publication = await publish_text(h)
    assert publication["status"] == "failed"
    assert publication["error"]["retryable"] is False
    calls = driver.vendor_calls()
    h.clock.advance(3600)
    await h.drain()
    assert driver.vendor_calls() == calls


async def test_rate_limit_is_waited_out_not_failed(h: Harness, driver: Driver) -> None:
    driver.rate_limit(90)
    response = await h.submit(text="contract test")
    await h.drain()
    waiting = await h.status(response.json()["id"])
    assert waiting["status"] == "processing"
    assert waiting["error"]["category"] == "rate_limited"

    calls = driver.vendor_calls()
    h.clock.advance(89)
    await h.drain()
    assert driver.vendor_calls() == calls, "retried before the vendor's hint elapsed"

    h.clock.advance(5)
    assert (await settle(h, response.json()["id"]))["status"] == "published"


async def test_unreachable_vendor_is_retried(h: Harness, driver: Driver) -> None:
    driver.unreachable()
    response = await h.submit(text="contract test")
    await h.drain()
    assert (await h.status(response.json()["id"]))["status"] == "processing"
    assert (await settle(h, response.json()["id"]))["status"] == "published"


async def test_delete_matches_the_declared_capability(h: Harness) -> None:
    publication = await publish_text(h)
    response = await h.client.delete(f"/v1/publications/{publication['id']}", headers=h.auth())
    if (await capabilities(h))["deletable"]:
        assert response.status_code == 200
        assert response.json()["status"] == "deleted"
    else:
        assert response.status_code == 422
        assert response.json()["capability"] == "delete"
        assert (await h.status(publication["id"]))["status"] == "published"


async def test_edit_matches_the_declared_capability(h: Harness) -> None:
    publication = await publish_text(h)
    response = await h.client.patch(
        f"/v1/publications/{publication['id']}", json={"text": "edited"}, headers=h.auth()
    )
    if "text" in (await capabilities(h))["editable"]:
        assert response.status_code == 200
    else:
        assert response.status_code == 422
        assert response.json()["capability"] == "edit:text"


async def test_vendor_credential_is_not_visible_to_consumers(h: Harness, driver: Driver) -> None:
    driver.reject_credentials()
    failed = await publish_text(h, key="a")
    ok = await publish_text(h, key="b")
    accounts = await h.client.get("/v1/accounts", headers=h.auth())
    credential = next(iter(driver.secrets.values()))
    visible = f"{failed} {ok} {accounts.text}"
    assert credential not in visible
    assert credential.split(":")[-1] not in visible
