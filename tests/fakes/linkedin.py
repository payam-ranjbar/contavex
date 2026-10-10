"""A stateful stand-in for LinkedIn's REST API, OAuth endpoints and upload host.

Request and response shapes follow docs/INTEGRATIONS/linkedin.md.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote

import httpx2

TOKEN = "AQXtestTESTtestTESTtestTESTtestTESTtestTESTtest"
REFRESHED_TOKEN = "AQXrefreshedREFRESHEDrefreshedREFRESHEDrefreshed"
REFRESH_TOKEN = "AQWrefreshTOKENrefreshTOKENrefreshTOKENrefresh"
CLIENT_ID = "86abcdefgh1234"
CLIENT_SECRET = "WPL_AP1.clientSECRETclientSECRET"
VERSION = "202609"
ORGANIZATION = "urn:li:organization:5515715"
PERSON = "urn:li:person:AbCdEf123"
ACCESS_EXPIRES_IN = 5_184_000  # 60 days
REFRESH_EXPIRES_IN = 300 * 86400

_COLLECTION_KEY = {"images": "image", "videos": "video", "documents": "document"}


def li_error(status: int, message: str, code: str | None = None) -> httpx2.Response:
    body: dict[str, Any] = {"message": message, "status": status, "serviceErrorCode": status}
    if code:
        body["code"] = code
    return httpx2.Response(status, json=body, headers={"x-li-uuid": "uuid-123"})


@dataclass
class Call:
    operation: str
    body: Any
    headers: dict[str, str]


@dataclass
class _Planned:
    reply: httpx2.Response | Exception
    apply: bool


@dataclass
class FakeLinkedIn:
    calls: list[Call] = field(default_factory=list)
    assets: dict[str, dict[str, Any]] = field(default_factory=dict)
    posts: list[dict[str, Any]] = field(default_factory=list)
    valid_token: str = TOKEN
    version: str = VERSION
    can_read_assets: bool = True
    can_list_posts: bool = True
    video_polls: int = 2
    token_active: bool = True
    token_expires_at: int = 1_805_000_000
    grants_refresh_token: bool = True
    _planned: dict[str, list[_Planned]] = field(default_factory=dict)
    _next_id: int = 6_844_785_523_593_134_080

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self._handle)

    def plan(
        self, operation: str, *replies: httpx2.Response | Exception, apply: bool = False
    ) -> None:
        self._planned.setdefault(operation, []).extend(_Planned(r, apply) for r in replies)

    def operations(self) -> list[str]:
        return [call.operation for call in self.calls]

    def count(self, operation: str) -> int:
        return self.operations().count(operation)

    def bodies(self, operation: str) -> list[Any]:
        return [call.body for call in self.calls if call.operation == operation]

    def create_out_of_band(self, body: dict[str, Any]) -> str:
        """What LinkedIn does with a create whose response never arrives."""
        return self._create_post(body)

    # ------------------------------------------------------------ plumbing

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        operation, target = self._route(request)
        content_type = request.headers.get("content-type", "")
        if operation == "upload_media":
            body: Any = request.content
        elif content_type.startswith("application/x-www-form-urlencoded"):
            body = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        elif request.content:
            body = json.loads(request.content)
        else:
            body = dict(request.url.params)
        self.calls.append(Call(operation, body, dict(request.headers)))

        if request.url.host == "api.linkedin.com":
            if request.headers.get("authorization") != f"Bearer {self.valid_token}":
                return li_error(401, "The token used in the request has expired or been revoked")
            assert request.headers.get("x-restli-protocol-version") == "2.0.0"
            if request.headers.get("linkedin-version") != self.version:
                return li_error(
                    426,
                    f"Requested version {request.headers.get('linkedin-version')} is not active",
                    "NONEXISTENT_VERSION",
                )

        planned = self._planned.get(operation)
        if planned:
            item = planned.pop(0)
            if item.apply:
                self._respond(operation, target, body, request)
            if isinstance(item.reply, Exception):
                raise item.reply
            return item.reply
        return self._respond(operation, target, body, request)

    def _route(self, request: httpx2.Request) -> tuple[str, str]:
        host, path = request.url.host, request.url.path
        if host == "www.linkedin.com":
            if path == "/oauth/v2/accessToken":
                return "refresh_token", ""
            if path == "/oauth/v2/introspectToken":
                return "introspect_token", ""
            assert path.startswith("/dms-uploads/"), path
            return "upload_media", path.removeprefix("/dms-uploads/")
        assert host == "api.linkedin.com", f"unexpected host {host}"
        match = re.fullmatch(r"/rest/(images|videos|documents)(?:/(.+))?", path)
        if match:
            if match.group(2):
                return "asset_status", unquote(match.group(2))
            action = request.url.params.get("action")
            operation = "initialize_upload" if action == "initializeUpload" else "finalize_upload"
            return operation, match.group(1)
        if path == "/rest/posts":
            return ("create_post" if request.method == "POST" else "list_posts"), ""
        match = re.fullmatch(r"/rest/posts/(.+)", path)
        assert match, f"unexpected path {path}"
        urn = unquote(match.group(1))
        if request.method == "DELETE":
            return "delete_post", urn
        assert request.headers.get("x-restli-method") == "PARTIAL_UPDATE"
        return "edit_post", urn

    def _respond(
        self, operation: str, target: str, body: Any, request: httpx2.Request
    ) -> httpx2.Response:
        if operation == "initialize_upload":
            details = body["initializeUploadRequest"]
            key = _COLLECTION_KEY[target]
            urn = f"urn:li:{key}:C{self._id()}"
            self.assets[urn] = {
                "kind": key,
                "owner": details["owner"],
                "bytes": b"",
                "status": "WAITING_UPLOAD",
                "polls": self.video_polls if key == "video" else 0,
            }
            value: dict[str, Any] = {key: urn}
            if key == "video":
                size = int(details["fileSizeBytes"])
                part = 4 * 1024 * 1024
                value["uploadToken"] = "upload-token"
                value["uploadInstructions"] = [
                    {
                        "firstByte": first,
                        "lastByte": min(first + part, size) - 1,
                        "uploadUrl": f"https://www.linkedin.com/dms-uploads/{urn}/{first}",
                    }
                    for first in range(0, size, part)
                ]
            else:
                value["uploadUrl"] = f"https://www.linkedin.com/dms-uploads/{urn}/0"
                value["uploadUrlExpiresAt"] = 1_900_000_000_000
            return httpx2.Response(200, json={"value": value})

        if operation == "upload_media":
            urn = target.rsplit("/", 1)[0]
            asset = self.assets[urn]
            has_token = "authorization" in request.headers
            if asset["kind"] == "video":
                assert not has_token, "video parts must not carry the access token"
            else:
                assert request.headers["authorization"] == f"Bearer {self.valid_token}"
                asset["status"] = "AVAILABLE"
            asset["bytes"] += body
            etag = f"etag-{len(asset['bytes'])}"
            return httpx2.Response(201 if asset["kind"] != "video" else 200, headers={"etag": etag})

        if operation == "finalize_upload":
            details = body["finalizeUploadRequest"]
            asset = self.assets[details["video"]]
            assert details["uploadToken"] == "upload-token"
            asset["etags"] = details["uploadedPartIds"]
            asset["status"] = "PROCESSING"
            return httpx2.Response(200, json={})

        if operation == "asset_status":
            if not self.can_read_assets:
                return li_error(
                    403, "Not enough permissions to access the resource", "ACCESS_DENIED"
                )
            asset = self.assets[target]
            if asset["status"] == "PROCESSING":
                asset["polls"] -= 1
                if asset["polls"] <= 0:
                    asset["status"] = "AVAILABLE"
            return httpx2.Response(200, json={"id": target, "status": asset["status"]})

        if operation == "create_post":
            urn = self._create_post(body)
            return httpx2.Response(201, headers={"x-restli-id": urn, "x-li-uuid": "uuid-123"})

        if operation == "list_posts":
            if not self.can_list_posts:
                return li_error(403, "Not enough permissions to access posts", "ACCESS_DENIED")
            author = request.url.params["author"]
            elements = [
                # LinkedIn returns hashtags as templates when commentary is read back.
                {
                    "id": post["id"],
                    "author": post["author"],
                    "commentary": re.sub(r"(?<!\\)#(\w+)", r"{hashtag|\\#|\1}", post["commentary"]),
                }
                for post in reversed(self.posts)
                if post["author"] == author
            ]
            return httpx2.Response(200, json={"elements": elements[:10], "paging": {}})

        if operation == "edit_post":
            for post in self.posts:
                if post["id"] == target:
                    post["commentary"] = body["patch"]["$set"]["commentary"]
                    return httpx2.Response(204)
            return li_error(404, "Post not found", "NOT_FOUND")

        if operation == "delete_post":
            self.posts = [post for post in self.posts if post["id"] != target]
            return httpx2.Response(204)  # deletion is idempotent at LinkedIn

        if operation == "refresh_token":
            assert body["client_id"] == CLIENT_ID and body["client_secret"] == CLIENT_SECRET
            if body.get("grant_type") != "refresh_token" or body.get("refresh_token") != (
                REFRESH_TOKEN
            ):
                return httpx2.Response(
                    400,
                    json={
                        "error": "invalid_request",
                        "error_description": "The provided authorization grant or refresh "
                        "token is invalid, expired or revoked",
                    },
                )
            self.valid_token = REFRESHED_TOKEN
            return httpx2.Response(
                200,
                json={
                    "access_token": REFRESHED_TOKEN,
                    "expires_in": ACCESS_EXPIRES_IN,
                    "refresh_token": REFRESH_TOKEN,
                    "refresh_token_expires_in": REFRESH_EXPIRES_IN,
                },
            )

        if operation == "introspect_token":
            assert body["client_id"] == CLIENT_ID and body["client_secret"] == CLIENT_SECRET
            if not self.token_active:
                return httpx2.Response(200, json={"active": False, "status": "revoked"})
            return httpx2.Response(
                200,
                json={
                    "active": True,
                    "status": "active",
                    "expires_at": self.token_expires_at,
                    "scope": "w_member_social",
                },
            )
        raise AssertionError(f"unhandled operation {operation}")

    def _create_post(self, body: dict[str, Any]) -> str:
        for field_name in ("author", "commentary", "visibility", "distribution", "lifecycleState"):
            assert field_name in body, f"missing required field {field_name}"
        content = body.get("content", {})
        referenced = [content["media"]["id"]] if "media" in content else []
        referenced += [image["id"] for image in content.get("multiImage", {}).get("images", [])]
        for urn in referenced:
            assert urn in self.assets, f"post references unknown asset {urn}"
        urn = f"urn:li:share:{self._id()}"
        self.posts.append({"id": urn, **body})
        return urn
