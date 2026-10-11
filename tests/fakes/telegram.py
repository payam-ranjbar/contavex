"""A scripted stand-in for api.telegram.org at the HTTP transport level."""

from __future__ import annotations

import email
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2

# Field names and required flags of every Bot API method, from the community
# machine-readable spec (Telegram publishes none). Every call the service
# makes to this fake is checked against it.
SPEC = json.loads(
    (Path(__file__).parent.parent / "fixtures/telegram/bot_api_spec.json").read_text("utf-8")
)
_INPUT_MEDIA = {
    "photo": "InputMediaPhoto",
    "video": "InputMediaVideo",
    "document": "InputMediaDocument",
}


def check_against_spec(method: str, params: dict[str, Any], files: set[str]) -> None:
    """Fail the test if a request would not be valid Bot API usage."""
    fields = SPEC["methods"].get(method)
    assert fields is not None, f"{method} is not a Bot API {SPEC['version']} method"
    unknown = set(params) - set(fields)
    assert not unknown, f"{method}: parameters not in the Bot API: {sorted(unknown)}"
    missing = {name for name, required in fields.items() if required} - set(params)
    assert not missing, f"{method}: required parameters missing: {sorted(missing)}"

    attached = {v.removeprefix("attach://") for v in params.values() if isinstance(v, str)}
    if "link_preview_options" in params:
        options = params["link_preview_options"]
        options = json.loads(options) if isinstance(options, str) else options
        assert set(options) <= set(SPEC["types"]["LinkPreviewOptions"])
    if method == "sendRichMessage":
        message = params["rich_message"]
        message = json.loads(message) if isinstance(message, str) else message
        assert set(message) <= set(SPEC["types"]["InputRichMessage"])
        assert sum(key in message for key in ("html", "markdown", "blocks")) == 1
        for entry in message.get("media", []):
            assert set(entry) == set(SPEC["types"]["InputRichMessageMedia"])
            kind = SPEC["types"][_INPUT_MEDIA[entry["media"]["type"]]]
            assert set(entry["media"]) <= set(kind)
            # Every declared media must be used in the text, and vice versa.
            assert f'?id={entry["id"]}"' in message["html"], f"media {entry['id']} is unused"
            attached.add(str(entry["media"]["media"]).removeprefix("attach://"))
        assert message["html"].count("tg://") == len(message.get("media", []))
    if method == "sendMediaGroup":
        media = params["media"]
        for item in json.loads(media) if isinstance(media, str) else media:
            kind = SPEC["types"][_INPUT_MEDIA[item["type"]]]
            assert set(item) <= set(kind), f"unknown InputMedia fields: {set(item) - set(kind)}"
            assert all(name in item for name, required in kind.items() if required)
            attached.add(str(item["media"]).removeprefix("attach://"))
    # Every uploaded part must be referenced, or Telegram ignores the file.
    assert files <= attached, f"{method}: unreferenced uploads {sorted(files - attached)}"


# Shaped like a real token; never a real one.
TOKEN = "987654321:TESTtestTESTtestTESTtestTESTtestTEST"
CHAT_ID = -1001234567890


@dataclass
class Call:
    method: str
    params: dict[str, Any]
    files: dict[str, tuple[str | None, str, bytes]]  # part name -> (filename, type, bytes)


@dataclass
class FakeTelegram:
    calls: list[Call] = field(default_factory=list)
    queue: list[httpx2.Response | Exception] = field(default_factory=list)
    next_message_id: int = 42
    username: str | None = None
    posts: int = 0  # sends that Telegram actually accepted
    test_environment: bool = False  # Telegram's test servers use /bot<token>/test/METHOD

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self._handle)

    def reply(self, *responses: httpx2.Response | Exception) -> None:
        """Queue responses for the next calls; afterwards calls succeed."""
        self.queue.extend(responses)

    @staticmethod
    def error(code: int, description: str, **parameters: Any) -> httpx2.Response:
        body: dict[str, Any] = {"ok": False, "error_code": code, "description": description}
        if parameters:
            body["parameters"] = parameters
        return httpx2.Response(code, json=body)

    def methods(self) -> list[str]:
        return [call.method for call in self.calls]

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        assert request.url.host == "api.telegram.org"
        prefix = f"/bot{TOKEN}/test/" if self.test_environment else f"/bot{TOKEN}/"
        assert request.url.path.startswith(prefix), "wrong token or environment in the URL"
        call = self._parse(request.url.path.removeprefix(prefix), request)
        check_against_spec(call.method, call.params, set(call.files))
        self.calls.append(call)
        if self.queue:
            item = self.queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        if call.method.startswith("send"):
            self.posts += 1
        return httpx2.Response(200, json={"ok": True, "result": self._result(call)})

    def _parse(self, method: str, request: httpx2.Request) -> Call:
        content_type = request.headers["content-type"]
        if content_type.startswith("application/json"):
            return Call(method, json.loads(request.content), {})
        assert content_type.startswith("multipart/form-data")
        message = email.message_from_bytes(
            b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + request.content
        )
        params: dict[str, Any] = {}
        files: dict[str, tuple[str | None, str, bytes]] = {}
        for part in message.get_payload():  # type: ignore[union-attr]
            name = part.get_param("name", header="content-disposition")
            payload = part.get_payload(decode=True)
            if part.get_filename() is not None:
                files[name] = (part.get_filename(), part.get_content_type(), payload)
            else:
                params[name] = payload.decode()
        return Call(method, params, files)

    def _message(self) -> dict[str, Any]:
        chat: dict[str, Any] = {"id": CHAT_ID, "type": "channel", "title": "Test channel"}
        if self.username:
            chat["username"] = self.username
        message = {"message_id": self.next_message_id, "date": 1_800_000_000, "chat": chat}
        self.next_message_id += 1
        return message

    def _result(self, call: Call) -> Any:
        if call.method == "sendMediaGroup":
            media = call.params["media"]
            count = len(json.loads(media) if isinstance(media, str) else media)
            return [self._message() for _ in range(count)]
        if call.method.startswith("delete"):
            return True
        return self._message()
