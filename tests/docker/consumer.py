"""A consuming application, run as its own container in the Docker smoke test.

It knows the publishing service's address and its own API key, and nothing
about Telegram. Standard library only.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BASE = "http://publisher:8080"
KEY = Path("/run/secrets/consumer_api_key_demo").read_text().strip()


def call(
    method: str, path: str, *, body: bytes | None = None, headers: dict[str, str] | None = None
) -> tuple[int, Any]:
    request = urllib.request.Request(  # noqa: S310
        BASE + path,
        data=body,
        method=method,
        headers={"authorization": f"Bearer {KEY}", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def publish(payload: dict[str, Any], key: str) -> dict[str, Any]:
    status, publication = call(
        "POST",
        "/v1/publications",
        body=json.dumps(payload).encode(),
        headers={"content-type": "application/json", "idempotency-key": key},
    )
    assert status == 202, (status, publication)
    for _ in range(40):
        status, publication = call("GET", f"/v1/publications/{publication['id']}")
        if publication["status"] not in ("accepted", "processing"):
            return publication  # type: ignore[no-any-return]
        time.sleep(0.5)
    raise AssertionError(f"still {publication['status']}")


def main() -> None:
    command = sys.argv[1]
    if command == "publish":
        status, accounts = call("GET", "/v1/accounts")
        assert status == 200 and accounts[0]["id"] == "news-channel", accounts

        text = publish({"account": "news-channel", "text": "hello from a container"}, "smoke-1")
        again = publish({"account": "news-channel", "text": "hello from a container"}, "smoke-1")
        assert again["id"] == text["id"], "the idempotency key did not deduplicate"

        status, media = call(
            "POST",
            "/v1/media?filename=pixel.jpg",
            body=b"\xff\xd8\xff\xe0 smoke test image",
            headers={"content-type": "image/jpeg"},
        )
        assert status == 201, media
        photo = publish(
            {
                "account": "news-channel",
                "text": "with a picture",
                "media": [{"type": "image", "media_id": media["id"]}],
            },
            "smoke-2",
        )
        print(json.dumps({"text": text, "photo": photo}))
    elif command == "status":
        print(json.dumps(call("GET", f"/v1/publications/{sys.argv[2]}")[1]))
    elif command == "anonymous":
        request = urllib.request.Request(BASE + "/v1/accounts")  # noqa: S310
        try:
            urllib.request.urlopen(request, timeout=10)  # noqa: S310
        except urllib.error.HTTPError as exc:
            print(exc.code)


if __name__ == "__main__":
    main()
