"""A stateful stand-in for graph.instagram.com at the HTTP transport level.

It models what the publishing flow depends on: containers that take time to
process, a publish call that works once per finished container, the media
list, the publishing quota and token refresh. Request and response shapes
follow docs/INTEGRATIONS/instagram.md.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx2

TOKEN = "IGAAtestTESTtestTESTtestTESTtestTESTtestTEST"
REFRESHED_TOKEN = "IGAArefreshedREFRESHEDrefreshedREFRESHEDrefreshed"
IG_USER_ID = "17841400000000000"
VERSION = "v25.0"
EXPIRES_IN = 5_183_944  # about 60 days, as Meta returns it


@dataclass
class Call:
    operation: str
    body: dict[str, Any]


@dataclass
class Container:
    id: str
    body: dict[str, Any]
    polls_until_ready: int
    status_code: str = "IN_PROGRESS"
    status: str = ""


def graph_error(
    code: int, message: str, subcode: int | None = None, status: int = 400, **extra: Any
) -> httpx2.Response:
    error: dict[str, Any] = {
        "message": message,
        "type": "OAuthException",
        "code": code,
        "fbtrace_id": "TRACE123",
        **extra,
    }
    if subcode is not None:
        error["error_subcode"] = subcode
    return httpx2.Response(status, json={"error": error})


@dataclass
class _Planned:
    reply: httpx2.Response | Exception
    apply: bool  # perform the real effect first, then lose/replace the response


@dataclass
class FakeInstagram:
    calls: list[Call] = field(default_factory=list)
    containers: dict[str, Container] = field(default_factory=dict)
    media: list[dict[str, Any]] = field(default_factory=list)
    stories: list[dict[str, Any]] = field(default_factory=list)
    valid_token: str = TOKEN
    video_polls: int = 2  # how many status polls a video stays IN_PROGRESS
    host: str = "graph.instagram.com"  # "graph.facebook.com" for the Facebook Login variant
    quota_usage: int = 0
    quota_total: int = 50
    _planned: dict[str, list[_Planned]] = field(default_factory=dict)
    _next_id: int = 1

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self._handle)

    def plan(
        self, operation: str, *replies: httpx2.Response | Exception, apply: bool = False
    ) -> None:
        """Make the next calls of `operation` answer with `replies` instead."""
        self._planned.setdefault(operation, []).extend(_Planned(r, apply) for r in replies)

    def operations(self) -> list[str]:
        return [call.operation for call in self.calls]

    def count(self, operation: str) -> int:
        return self.operations().count(operation)

    def bodies(self, operation: str) -> list[dict[str, Any]]:
        return [call.body for call in self.calls if call.operation == operation]

    def publish_out_of_band(self, container_id: str) -> None:
        """What Meta does when it handles a publish whose response never arrives."""
        self._publish(container_id)

    # ------------------------------------------------------------ plumbing

    def _id(self, prefix: str) -> str:
        self._next_id += 1
        return f"{prefix}{self._next_id}"

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        assert request.url.host == self.host, f"wrong host {request.url.host}"
        operation, target = self._route(request)
        body = json.loads(request.content) if request.content else dict(request.url.params)
        self.calls.append(Call(operation, body))

        if operation != "refresh_token":
            assert "access_token" not in request.url.params, "token must not be in the URL"
            if request.headers.get("authorization") != f"Bearer {self.valid_token}":
                return graph_error(190, "Invalid OAuth access token.", subcode=463)

        planned = self._planned.get(operation)
        if planned:
            item = planned.pop(0)
            if item.apply:
                self._respond(operation, target, body)
            if isinstance(item.reply, Exception):
                raise item.reply
            return item.reply
        return self._respond(operation, target, body)

    def _route(self, request: httpx2.Request) -> tuple[str, str]:
        path = request.url.path
        if path == "/refresh_access_token":
            return "refresh_token", ""
        prefix = f"/{VERSION}/"
        assert path.startswith(prefix), f"unexpected path {path}"
        parts = path.removeprefix(prefix).split("/")
        if parts[0] == IG_USER_ID and len(parts) == 2:
            edge = parts[1]
            if edge == "media":
                return ("create_container" if request.method == "POST" else "list_media"), ""
            if edge == "media_publish":
                return "media_publish", ""
            if edge == "content_publishing_limit":
                return "publishing_limit", ""
        assert len(parts) == 1, f"unexpected path {path}"
        if request.method == "DELETE":
            return "delete_media", parts[0]
        if parts[0] in self.containers:
            return "container_status", parts[0]
        return "get_media", parts[0]

    def _respond(self, operation: str, target: str, body: dict[str, Any]) -> httpx2.Response:
        if operation == "create_container":
            is_video = "video_url" in body
            container = Container(self._id("container-"), body, self.video_polls if is_video else 0)
            if container.polls_until_ready == 0:
                container.status_code = "FINISHED"
            self.containers[container.id] = container
            return httpx2.Response(200, json={"id": container.id})

        if operation == "container_status":
            container = self.containers[target]
            if container.status_code == "IN_PROGRESS":
                container.polls_until_ready -= 1
                if container.polls_until_ready <= 0:
                    container.status_code = "FINISHED"
                else:
                    return httpx2.Response(
                        200, json={"status_code": "IN_PROGRESS", "id": container.id}
                    )
            return httpx2.Response(
                200,
                json={
                    "status_code": container.status_code,
                    "status": container.status,
                    "id": container.id,
                },
            )

        if operation == "media_publish":
            container_id = str(body["creation_id"])
            container = self.containers.get(container_id)
            if container is None:
                return graph_error(24, "Cannot find the container.", subcode=2207008)
            if container.status_code == "PUBLISHED":
                return graph_error(100, "The container was already published.")
            if container.status_code != "FINISHED":
                return graph_error(9007, "Media ID is not available", subcode=2207027)
            if self.quota_usage >= self.quota_total:
                return graph_error(9, "Application request limit reached", subcode=2207042)
            return httpx2.Response(200, json={"id": self._publish(container_id)})

        if operation == "delete_media":
            exists = any(media["id"] == target for media in self.media + self.stories)
            if self.host != "graph.facebook.com" or not exists:
                # What graph.instagram.com answered to a real delete on 2026-10-11.
                return graph_error(
                    100,
                    f"Unsupported delete request. Object with ID '{target}' does not exist, "
                    "cannot be loaded due to missing permissions, or does not support this "
                    "operation",
                    subcode=33,
                )
            self.media = [media for media in self.media if media["id"] != target]
            self.stories = [media for media in self.stories if media["id"] != target]
            return httpx2.Response(200, json={"success": True, "deleted_id": target})

        if operation == "list_media":
            return httpx2.Response(200, json={"data": list(reversed(self.media))[:10]})

        if operation == "get_media":
            for media in self.media:
                if media["id"] == target:
                    return httpx2.Response(
                        200, json={"permalink": media["permalink"], "id": target}
                    )
            return graph_error(100, "Object does not exist", subcode=33)

        if operation == "publishing_limit":
            entry = {
                "quota_usage": self.quota_usage,
                "config": {"quota_total": self.quota_total, "quota_duration": 86400},
            }
            return httpx2.Response(200, json={"data": [entry]})

        if operation == "refresh_token":
            if body.get("access_token") != self.valid_token:
                return graph_error(190, "Invalid OAuth access token.", subcode=463)
            self.valid_token = REFRESHED_TOKEN
            return httpx2.Response(
                200,
                json={
                    "access_token": REFRESHED_TOKEN,
                    "token_type": "bearer",
                    "expires_in": EXPIRES_IN,
                },
            )
        raise AssertionError(f"unhandled operation {operation}")

    def _publish(self, container_id: str) -> str:
        container = self.containers[container_id]
        container.status_code = "PUBLISHED"
        self.quota_usage += 1
        media_id = self._id("1790000000000")
        is_story = container.body.get("media_type") == "STORIES"
        entry = {
            "id": media_id,
            "caption": container.body.get("caption"),
            "permalink": f"https://www.instagram.com/p/{media_id}/",
        }
        # Stories are not returned by the media edge.
        (self.stories if is_story else self.media).append(entry)
        return media_id
