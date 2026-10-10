"""A scripted stand-in for api.telegram.org at the HTTP transport level."""

from __future__ import annotations

import email
import json
from dataclasses import dataclass, field
from typing import Any

import httpx2

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
        prefix = f"/bot{TOKEN}/"
        assert request.url.path.startswith(prefix), "wrong or missing bot token in the URL"
        call = self._parse(request.url.path.removeprefix(prefix), request)
        self.calls.append(call)
        if self.queue:
            item = self.queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
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
