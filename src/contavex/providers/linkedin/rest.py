"""LinkedIn publishing through the versioned REST API (`/rest/posts`).

Facts this module relies on are in docs/INTEGRATIONS/linkedin.md:

* Every post names its author: a member (`urn:li:person:...`) or a Page
  (`urn:li:organization:...`). That is an account setting here.
* Media is uploaded to LinkedIn as bytes first (never fetched from a URL),
  then referenced from the post by URN. Video goes up in 4 MB parts.
* Creating the post is one call with no idempotency key. After an ambiguous
  create, a Page's posts can be listed to see whether it exists; a member's
  cannot, so that case ends as `Unknown`.
* Access tokens last 60 days. They are renewed only for apps that LinkedIn
  gives refresh tokens to; otherwise a person must re-authorize.
* Plain text must be escaped: LinkedIn's "little" text format reserves a
  dozen punctuation characters.

The flow is a state machine like Instagram's; `state["step"]` names the next step.
"""

from __future__ import annotations

import re
import time
from typing import Any, Literal
from urllib.parse import quote, urlsplit

import httpx2
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from contavex.config import AccountConfig
from contavex.core.capabilities import Capabilities, KindCapability, MediaTypeCapability
from contavex.core.errors import ErrorCategory, PublishError
from contavex.core.models import (
    MediaSource,
    MediaType,
    PublicationRequest,
    PublicationResult,
    PublishedPost,
)
from contavex.credentials import CredentialState, RefreshedCredential
from contavex.observability import log
from contavex.providers import http
from contavex.providers.base import (
    Continue,
    Done,
    Provider,
    ProviderContext,
    StepOutcome,
    Unknown,
)
from contavex.providers.http import Ambiguous
from contavex.secrets import Secret
from contavex.store.db import MediaRecord

MB = 1024 * 1024
DAY = 86400.0
DEFAULT_API_BASE = "https://api.linkedin.com"
DEFAULT_OAUTH_BASE = "https://www.linkedin.com"
# LinkedIn versions its API monthly and retires each version after about a
# year (HTTP 426). Set settings.api_version to move; review it yearly.
DEFAULT_API_VERSION = "202609"

VIDEO_PART_BYTES = 4 * MB
UPLOAD_BUDGET_SECONDS = 45.0  # per step; a large video continues in the next one
POLL_DELAYS = (3.0, 5.0, 10.0, 20.0, 30.0)
POLL_STEADY = 60.0
MAX_POLLS = 45
MAX_RECOVER_TRIES = 5
THROTTLE_RETRY_SECONDS = 900.0
REFRESH_WHEN_LEFT = 14 * DAY

_AUTHOR = re.compile(r"^urn:li:(person|organization):[A-Za-z0-9_-]+$")
# Characters LinkedIn's "little" text format reserves. `#` is handled
# separately so that a written hashtag stays a hashtag.
_RESERVED = re.compile(r"[|{}@\[\]()<>\\*_~]")
_BARE_HASH = re.compile(r"#(?!\w)")
_HASHTAG_TEMPLATE = re.compile(r"\{hashtag\|\\?#\|([^}]*)\}")

# media type -> (REST collection, key of the URN in the initialize response)
_ASSETS: dict[MediaType, tuple[str, str]] = {
    MediaType.IMAGE: ("images", "image"),
    MediaType.VIDEO: ("videos", "video"),
    MediaType.DOCUMENT: ("documents", "document"),
}


class LinkedInOptions(BaseModel):
    """`options.linkedin` of a publication request."""

    model_config = ConfigDict(extra="forbid")

    visibility: Literal["PUBLIC", "CONNECTIONS", "LOGGED_IN"] = "PUBLIC"
    media_title: str | None = Field(
        default=None, max_length=400, description="Title shown for a video or document."
    )
    disable_reshare: bool = False


def escape_little(text: str) -> str:
    """Turn plain text into LinkedIn "little" text that renders as written."""
    return _BARE_HASH.sub(r"\\#", _RESERVED.sub(lambda m: "\\" + m.group(0), text))


def _unescape_little(text: str) -> str:
    """Undo what LinkedIn does to commentary when it is read back."""
    return re.sub(r"\\(.)", r"\1", _HASHTAG_TEMPLATE.sub(r"#\1", text))


def _poll_delay(polls: int) -> float:
    return POLL_DELAYS[polls] if polls < len(POLL_DELAYS) else POLL_STEADY


class LinkedInRest(Provider):
    platform = "linkedin"
    implementation = "rest"
    mutable_credentials = frozenset({"access_token", "refresh_token"})

    # ---------------------------------------------------------------- description

    def capabilities(self, account: AccountConfig) -> Capabilities:
        upload = [MediaSource.UPLOAD]  # LinkedIn never fetches media from a URL
        return Capabilities(
            kinds={
                # Text; or one image, video or document; or 2-20 images.
                "post": KindCapability(
                    max_media=20,
                    media_types=[MediaType.IMAGE, MediaType.VIDEO, MediaType.DOCUMENT],
                )
            },
            # "little" is LinkedIn's own markup, for callers who write mentions.
            text_formats=["plain", "little"],
            max_text_length=3000,
            max_caption_length=3000,
            media={
                MediaType.IMAGE: MediaTypeCapability(
                    sources=upload, content_types=["image/jpeg", "image/png", "image/gif"]
                ),
                MediaType.VIDEO: MediaTypeCapability(
                    sources=upload, max_bytes=500 * MB, content_types=["video/mp4"]
                ),
                MediaType.DOCUMENT: MediaTypeCapability(
                    sources=upload,
                    max_bytes=100 * MB,
                    content_types=[
                        "application/pdf",
                        "application/msword",
                        "application/vnd.ms-powerpoint",
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                    ],
                ),
            },
            editable=["text"],  # commentary only; media cannot be changed
            deletable=True,
            options_schema=LinkedInOptions.model_json_schema(),
        )

    def validate_account(self, account: AccountConfig) -> None:
        author = account.settings.get("author")
        if not isinstance(author, str) or not _AUTHOR.fullmatch(author):
            raise ValueError(
                "settings.author is required: urn:li:person:<id> for a profile or "
                "urn:li:organization:<id> for a Page"
            )
        unknown = set(account.settings) - {"author", "api_version", "api_base", "oauth_base"}
        if unknown:
            raise ValueError(f"unknown settings: {sorted(unknown)}")
        roles = set(account.credentials)
        allowed = {"access_token", "refresh_token", "client_id", "client_secret"}
        if "access_token" not in roles or roles - allowed:
            raise ValueError(f"credentials must include access_token; allowed: {sorted(allowed)}")
        if "refresh_token" in roles and not {"client_id", "client_secret"} <= roles:
            raise ValueError("a refresh_token also needs client_id and client_secret")
        if ("client_id" in roles) != ("client_secret" in roles):
            raise ValueError("client_id and client_secret go together")

    def validate(self, account: AccountConfig, request: PublicationRequest) -> None:
        self._options(request)
        types = [item.type for item in request.media]
        if len(types) > 1 and set(types) != {MediaType.IMAGE}:
            raise PublishError(
                ErrorCategory.UNSUPPORTED,
                "a LinkedIn post takes one video or one document, or up to 20 images",
            )

    @staticmethod
    def _options(request: PublicationRequest) -> LinkedInOptions:
        try:
            return LinkedInOptions.model_validate(request.options.get("linkedin", {}))
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
            )
            raise PublishError(
                ErrorCategory.INVALID_REQUEST, f"options.linkedin: {problems}"
            ) from None

    @staticmethod
    def _commentary(request: PublicationRequest, text: str | None = None) -> str:
        value = (request.text or "") if text is None else text
        return value if request.text_format == "little" else escape_little(value)

    # ---------------------------------------------------------------- state machine

    def step_is_repeatable(self, request: PublicationRequest, state: dict[str, Any]) -> bool:
        return bool(state.get("step", "upload") != "create")

    async def advance(
        self, ctx: ProviderContext, request: PublicationRequest, state: dict[str, Any]
    ) -> StepOutcome:
        steps = {
            "upload": self._upload,
            "wait_media": self._wait_media,
            "create": self._create,
            "recover": self._recover,
        }
        return await steps[state.get("step", "upload")](ctx, request, state)

    async def reconcile(
        self, ctx: ProviderContext, request: PublicationRequest, state: dict[str, Any]
    ) -> StepOutcome | None:
        return await self._recover(ctx, request, {**state, "unconfirmed": True})

    async def _upload(
        self, ctx: ProviderContext, request: PublicationRequest, state: dict[str, Any]
    ) -> StepOutcome:
        """Upload media to LinkedIn, one item (or one slice of a video) per step."""
        assets: list[str] = list(state.get("assets", []))
        if len(assets) == len(request.media):
            return Continue({**state, "step": "wait_media", "assets": assets, "polls": 0})
        item = request.media[len(assets)]
        assert item.media_id is not None  # capabilities only allow uploads
        media = ctx.media.get(item.media_id)
        if media is None:
            raise PublishError(
                ErrorCategory.MEDIA_UNREACHABLE,
                f"uploaded media {item.media_id} is no longer available",
                retryable=False,
            )
        if item.type is MediaType.VIDEO:
            return await self._upload_video(ctx, media, state, assets)

        collection, key = _ASSETS[item.type]
        value = await self._action(
            ctx, collection, "initializeUpload", {"owner": self._author(ctx)}, "initialize_upload"
        )
        try:
            urn, upload_url = str(value[key]), str(value["uploadUrl"])
        except KeyError:
            raise _malformed_upload() from None
        # Image and document uploads are authenticated with the access token.
        await self._put(
            ctx, upload_url, ctx.media.path(media.id).read_bytes(), media, authenticated=True
        )
        return Continue({**state, "step": "upload", "assets": [*assets, urn]})

    async def _upload_video(
        self, ctx: ProviderContext, media: MediaRecord, state: dict[str, Any], assets: list[str]
    ) -> StepOutcome:
        video: dict[str, Any] | None = state.get("video")
        if video is None:
            value = await self._action(
                ctx,
                "videos",
                "initializeUpload",
                {
                    "owner": self._author(ctx),
                    "fileSizeBytes": media.size,
                    "uploadCaptions": False,
                    "uploadThumbnail": False,
                },
                "initialize_upload",
            )
            try:
                video = {
                    "urn": str(value["video"]),
                    "token": str(value.get("uploadToken", "")),
                    "parts": [
                        [int(part["firstByte"]), int(part["lastByte"]), str(part["uploadUrl"])]
                        for part in value["uploadInstructions"]
                    ],
                    "etags": [],
                }
            except KeyError, TypeError, ValueError:
                raise _malformed_upload() from None
            ctx.checkpoint({**state, "video": video})

        started = time.monotonic()
        etags: list[str] = list(video["etags"])
        with ctx.media.path(media.id).open("rb") as handle:
            while len(etags) < len(video["parts"]):
                first, last, upload_url = video["parts"][len(etags)]
                handle.seek(first)
                # Video parts go to a pre-signed URL and must not carry the token.
                response = await self._put(
                    ctx, upload_url, handle.read(last - first + 1), media, authenticated=False
                )
                etag = response.headers.get("etag")
                if not etag:
                    raise PublishError(
                        ErrorCategory.MALFORMED_PROVIDER_RESPONSE,
                        "LinkedIn returned no ETag for an uploaded video part",
                    )
                etags.append(etag)
                video = {**video, "etags": etags}
                ctx.checkpoint({**state, "video": video})
                if time.monotonic() - started > UPLOAD_BUDGET_SECONDS:
                    # Keep each step well inside its time limit.
                    return Continue({**state, "step": "upload", "video": video})

        await self._action(
            ctx,
            "videos",
            "finalizeUpload",
            {"video": video["urn"], "uploadToken": video["token"], "uploadedPartIds": etags},
            "finalize_upload",
        )
        rest = {k: v for k, v in state.items() if k != "video"}
        return Continue({**rest, "step": "upload", "assets": [*assets, video["urn"]]})

    async def _wait_media(
        self, ctx: ProviderContext, request: PublicationRequest, state: dict[str, Any]
    ) -> StepOutcome:
        """Wait until LinkedIn has processed every uploaded asset."""
        for urn, item in zip(state["assets"], request.media, strict=True):
            collection, _ = _ASSETS[item.type]
            try:
                body = await self._request(
                    ctx, "GET", f"/rest/{collection}/{quote(urn, safe='')}", "asset_status"
                )
            except PublishError as exc:
                if exc.category is ErrorCategory.AUTHORIZATION:
                    # A member token with only w_member_social may not read
                    # assets. Proceed; LinkedIn finishes processing on its side.
                    continue
                raise
            status = body.get("status")
            if status == "PROCESSING_FAILED":
                raise PublishError(
                    ErrorCategory.MEDIA_REJECTED,
                    "LinkedIn could not process the media"
                    + (f": {reason}" if (reason := body.get("processingFailureReason")) else ""),
                )
            if status in ("PROCESSING", "WAITING_UPLOAD"):
                polls = int(state.get("polls", 0))
                if polls >= MAX_POLLS:
                    raise PublishError(
                        ErrorCategory.TIMEOUT,
                        "LinkedIn was still processing the media after 40 minutes",
                        retryable=True,
                        final=True,
                    )
                return Continue({**state, "polls": polls + 1}, _poll_delay(polls))
        return Continue({**state, "step": "create"})

    def _post_body(
        self, ctx: ProviderContext, request: PublicationRequest, assets: list[str]
    ) -> dict[str, Any]:
        options = self._options(request)
        body: dict[str, Any] = {
            "author": self._author(ctx),
            "commentary": self._commentary(request),
            "visibility": options.visibility,
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": options.disable_reshare,
        }
        if len(assets) > 1:
            body["content"] = {
                "multiImage": {
                    "images": [
                        {"id": urn} | ({"altText": item.alt_text} if item.alt_text else {})
                        for urn, item in zip(assets, request.media, strict=True)
                    ]
                }
            }
        elif assets:
            item = request.media[0]
            media: dict[str, Any] = {"id": assets[0]}
            if item.type is MediaType.IMAGE and item.alt_text:
                media["altText"] = item.alt_text
            elif item.type is MediaType.DOCUMENT:
                media["title"] = options.media_title or "Document"  # required by LinkedIn
            elif item.type is MediaType.VIDEO and options.media_title:
                media["title"] = options.media_title
            body["content"] = {"media": media}
        return body

    async def _create(
        self, ctx: ProviderContext, request: PublicationRequest, state: dict[str, Any]
    ) -> StepOutcome:
        """The one call that must not be repeated blindly."""
        try:
            response = await self._raw(
                ctx,
                "POST",
                "/rest/posts",
                "create_post",
                json=self._post_body(ctx, request, state.get("assets", [])),
                ambiguous=True,
            )
        except Ambiguous:
            return await self._recover(ctx, request, {**state, "unconfirmed": True})
        except PublishError as exc:
            if exc.retryable and exc.category is ErrorCategory.PROVIDER_UNAVAILABLE:
                # A 5xx or a 409 does not say whether the post was created.
                return await self._recover(ctx, request, {**state, "unconfirmed": True})
            if state.get("unconfirmed") and exc.provider_code == "422":
                # Refused as a duplicate of our own earlier attempt: that one got through.
                return Unknown(
                    "LinkedIn refused a repeat of this post as a duplicate, so an earlier "
                    "attempt was most likely published; its id was never received",
                    state,
                )
            raise
        urn = response.headers.get("x-restli-id")
        if not urn:
            return await self._recover(ctx, request, {**state, "unconfirmed": True})
        return Done(PublicationResult(posts=[_post(urn)]))

    async def _recover(
        self, ctx: ProviderContext, request: PublicationRequest, state: dict[str, Any]
    ) -> StepOutcome:
        """After an unconfirmed create: look for the post among the author's recent ones."""
        author = self._author(ctx)
        cannot_tell = (
            "LinkedIn did not confirm the post, and this account cannot list its posts "
            "to check whether it exists"
        )
        if author.startswith("urn:li:person:"):
            # Reading a member's posts needs r_member_social, which LinkedIn does not grant.
            return Unknown(cannot_tell, state)
        tries = int(state.get("recover_tries", 0))
        try:
            body = await self._request(
                ctx,
                "GET",
                "/rest/posts",
                "list_posts",
                params={"q": "author", "author": author, "count": "10", "sortBy": "LAST_MODIFIED"},
                headers={"X-RestLi-Method": "FINDER"},
            )
        except PublishError as exc:
            if exc.category is ErrorCategory.AUTHORIZATION:
                return Unknown(cannot_tell, state)  # no r_organization_social
            if not exc.retryable:
                raise
            if tries >= MAX_RECOVER_TRIES:
                return Unknown(f"{cannot_tell} ({exc.message})", state)
            return Continue({**state, "step": "recover", "recover_tries": tries + 1}, 30.0)

        wanted = _unescape_little(self._commentary(request))
        for element in body.get("elements", []):
            if not isinstance(element, dict) or not element.get("id"):
                continue
            if _unescape_little(str(element.get("commentary", ""))) == wanted:
                return Done(PublicationResult(posts=[_post(str(element["id"]))]))
        if not state.get("recover_confirmed"):
            # Look twice, 30 seconds apart, before concluding it was never created.
            return Continue({**state, "step": "recover", "recover_confirmed": True}, 30.0)
        cleaned = {k: v for k, v in state.items() if not k.startswith("recover_")}
        return Continue({**cleaned, "step": "create"})

    # ---------------------------------------------------------------- edit / delete

    async def edit(
        self,
        ctx: ProviderContext,
        request: PublicationRequest,
        result: PublicationResult,
        changes: dict[str, Any],
    ) -> None:
        if not result.posts:
            raise PublishError(ErrorCategory.UNSUPPORTED, "LinkedIn returned no post id to edit")
        edited = request.model_copy(
            update={"text_format": changes.get("text_format", request.text_format)}
        )
        await self._raw(
            ctx,
            "POST",
            f"/rest/posts/{quote(result.posts[0].id, safe='')}",
            "edit_post",
            json={"patch": {"$set": {"commentary": self._commentary(edited, changes["text"])}}},
            headers={"X-RestLi-Method": "PARTIAL_UPDATE"},
        )

    async def delete(self, ctx: ProviderContext, result: PublicationResult) -> None:
        if not result.posts:
            raise PublishError(ErrorCategory.UNSUPPORTED, "LinkedIn returned no post id to delete")
        urn = result.posts[0].id
        try:
            await self._raw(
                ctx,
                "DELETE",
                f"/rest/posts/{quote(urn, safe='')}",
                "delete_post",
                headers={"X-RestLi-Method": "DELETE"},
            )
            return
        except PublishError as exc:
            if exc.provider_code != "404" or not urn.startswith("urn:li:share:"):
                raise
        # Observed live on 2026-10-14: for a `urn:li:share` created by a
        # self-serve app, DELETE /rest/posts answers 404 "Could not find
        # entity" while the post is still up, and DELETE /v2/shares/{id}
        # removes it. A 404 here therefore does NOT mean "already gone".
        await self._delete_share(ctx, urn.removeprefix("urn:li:share:"))

    async def _delete_share(self, ctx: ProviderContext, share_id: str) -> None:
        base = str(ctx.account.settings.get("api_base", DEFAULT_API_BASE)).rstrip("/")
        response = await http.send(
            ctx.http,
            "DELETE",
            f"{base}/v2/shares/{quote(share_id, safe='')}",
            vendor="LinkedIn",
            operation="delete_share",
            headers={
                "authorization": f"Bearer {ctx.credentials['access_token'].reveal()}",
                "x-restli-protocol-version": "2.0.0",
            },
        )
        log.info("linkedin_request", operation="delete_share", status=response.status_code)
        if response.status_code == 404:
            return  # the shares endpoint knows the post; not found there means gone
        if response.status_code >= 300:
            raise _classify(response)

    # ---------------------------------------------------------------- credentials

    async def refresh_credentials(
        self, ctx: ProviderContext, states: dict[str, CredentialState], now: float
    ) -> list[RefreshedCredential]:
        if "client_id" not in ctx.credentials:
            return []  # nothing can be learned or renewed without the app's credentials
        access = states["access_token"]
        client = {
            "client_id": ctx.credentials["client_id"].reveal(),
            "client_secret": ctx.credentials["client_secret"].reveal(),
        }
        if "refresh_token" in ctx.credentials and (
            access.expires_at is None or access.expires_at - now < REFRESH_WHEN_LEFT
        ):
            body = await self._oauth(
                ctx,
                "accessToken",
                "refresh_token",
                {
                    "grant_type": "refresh_token",
                    "refresh_token": ctx.credentials["refresh_token"].reveal(),
                    **client,
                },
            )
            token = body.get("access_token")
            if not isinstance(token, str) or not token:
                raise PublishError(
                    ErrorCategory.MALFORMED_PROVIDER_RESPONSE, "LinkedIn returned no access token"
                )
            refreshed = [
                RefreshedCredential("access_token", Secret(token), _after(now, body, "expires_in"))
            ]
            # The refresh token keeps its original deadline; store what comes back.
            renewed = body.get("refresh_token")
            if isinstance(renewed, str) and renewed:
                refreshed.append(
                    RefreshedCredential(
                        "refresh_token",
                        Secret(renewed),
                        _after(now, body, "refresh_token_expires_in"),
                    )
                )
            return refreshed

        if access.expires_at is None:
            # No refresh token: at least learn when the access token lapses,
            # so the account can say "re-authorize by <date>".
            body = await self._oauth(
                ctx,
                "introspectToken",
                "introspect_token",
                {"token": ctx.credentials["access_token"].reveal(), **client},
            )
            if body.get("active") is False:
                raise PublishError(
                    ErrorCategory.AUTHENTICATION,
                    f"LinkedIn reports the access token as {body.get('status', 'inactive')}",
                )
            expires_at = body.get("expires_at")
            if isinstance(expires_at, int | float):
                return [
                    RefreshedCredential(
                        "access_token", ctx.credentials["access_token"], float(expires_at)
                    )
                ]
        return []

    async def _oauth(
        self, ctx: ProviderContext, endpoint: str, operation: str, form: dict[str, str]
    ) -> dict[str, Any]:
        base = str(ctx.account.settings.get("oauth_base", DEFAULT_OAUTH_BASE)).rstrip("/")
        response = await http.send(
            ctx.http,
            "POST",
            f"{base}/oauth/v2/{endpoint}",
            vendor="LinkedIn",
            operation=operation,
            data=form,
        )
        try:
            body = response.json()
            assert isinstance(body, dict)
        except ValueError, AssertionError:
            raise PublishError(
                ErrorCategory.MALFORMED_PROVIDER_RESPONSE,
                f"LinkedIn returned an unreadable response (HTTP {response.status_code})",
            ) from None
        log.info("linkedin_request", operation=operation, status=response.status_code)
        if response.status_code >= 500:
            raise PublishError(
                ErrorCategory.PROVIDER_UNAVAILABLE,
                f"LinkedIn failed with HTTP {response.status_code}",
            )
        if response.status_code >= 400:
            # invalid_grant and friends: the refresh token is expired or revoked.
            raise PublishError(
                ErrorCategory.AUTHENTICATION,
                f"LinkedIn: {body.get('error_description') or body.get('error') or 'refused'}",
                provider_code=str(body.get("error", response.status_code)),
            )
        return body

    # ---------------------------------------------------------------- calls

    @staticmethod
    def _author(ctx: ProviderContext) -> str:
        return str(ctx.account.settings["author"])

    async def _action(
        self,
        ctx: ProviderContext,
        collection: str,
        action: str,
        request: dict[str, Any],
        operation: str,
    ) -> dict[str, Any]:
        body = await self._request(
            ctx,
            "POST",
            f"/rest/{collection}",
            operation,
            params={"action": action},
            json={f"{action}Request": request},
        )
        value = body.get("value", {})
        return value if isinstance(value, dict) else {}

    async def _put(
        self,
        ctx: ProviderContext,
        upload_url: str,
        content: bytes,
        media: MediaRecord,
        *,
        authenticated: bool,
    ) -> httpx2.Response:
        host = urlsplit(upload_url).hostname or ""
        if not upload_url.startswith("https://") or not (
            host == "linkedin.com" or host.endswith(".linkedin.com")
        ):
            # The URL comes from a response body. Never send bytes, let alone
            # the token, anywhere but LinkedIn.
            raise PublishError(
                ErrorCategory.MALFORMED_PROVIDER_RESPONSE,
                "LinkedIn returned an upload URL outside linkedin.com",
                retryable=False,
            )
        headers = {"content-type": "application/octet-stream"}
        if authenticated:
            headers["authorization"] = f"Bearer {ctx.credentials['access_token'].reveal()}"
            headers["content-type"] = media.content_type
        response = await http.send(
            ctx.http,
            "PUT",
            upload_url,
            vendor="LinkedIn",
            operation="upload_media",
            content=content,
            headers=headers,
        )
        log.info("linkedin_request", operation="upload_media", status=response.status_code)
        if response.status_code >= 300:
            raise _classify(response)
        return response

    async def _request(
        self, ctx: ProviderContext, method: str, path: str, operation: str, **kwargs: Any
    ) -> dict[str, Any]:
        response = await self._raw(ctx, method, path, operation, **kwargs)
        if not response.content:
            return {}
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise PublishError(
                ErrorCategory.MALFORMED_PROVIDER_RESPONSE,
                f"LinkedIn returned an unreadable response (HTTP {response.status_code})",
            )
        return body

    async def _raw(
        self,
        ctx: ProviderContext,
        method: str,
        path: str,
        operation: str,
        *,
        ambiguous: bool = False,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> httpx2.Response:
        """Call the versioned REST API; returns the response on 2xx."""
        base = str(ctx.account.settings.get("api_base", DEFAULT_API_BASE)).rstrip("/")
        started = time.perf_counter()
        response = await http.send(
            ctx.http,
            method,
            f"{base}{path}",
            vendor="LinkedIn",
            operation=operation,
            ambiguous=ambiguous,
            headers={
                "authorization": f"Bearer {ctx.credentials['access_token'].reveal()}",
                "linkedin-version": str(
                    ctx.account.settings.get("api_version", DEFAULT_API_VERSION)
                ),
                "x-restli-protocol-version": "2.0.0",
                **(headers or {}),
            },
            **kwargs,
        )
        status = response.status_code
        log.info(
            "linkedin_request",
            operation=operation,
            status=status,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
            provider_request_id=response.headers.get("x-li-uuid"),
        )
        if status < 300:
            return response
        if ambiguous and status in (500, 502, 503, 504):
            raise Ambiguous(f"failed with HTTP {status}")
        raise _classify(response)


# ---------------------------------------------------------------- helpers


def _malformed_upload() -> PublishError:
    return PublishError(
        ErrorCategory.MALFORMED_PROVIDER_RESPONSE,
        "LinkedIn returned upload instructions that could not be read",
    )


def _after(now: float, body: dict[str, Any], key: str) -> float | None:
    seconds = body.get(key)
    return now + float(seconds) if isinstance(seconds, int | float) else None


def _post(urn: str) -> PublishedPost:
    return PublishedPost(id=urn, url=f"https://www.linkedin.com/feed/update/{urn}/")


def _classify(response: httpx2.Response) -> PublishError:
    """Map a LinkedIn error response to the service's vocabulary, by HTTP
    status first and LinkedIn's `code` string where it adds something."""
    status = response.status_code
    try:
        body = response.json()
        assert isinstance(body, dict)
    except ValueError, AssertionError:
        body = {}
    code = str(body.get("code", ""))
    message = str(body.get("message") or f"HTTP {status}")

    def fail(category: ErrorCategory, **kwargs: Any) -> PublishError:
        return PublishError(
            category,
            f"LinkedIn: {message}",
            provider_code=str(status),
            provider_request_id=response.headers.get("x-li-uuid"),
            **kwargs,
        )

    if status == 401:
        return fail(ErrorCategory.AUTHENTICATION)
    if status == 403:
        return fail(ErrorCategory.AUTHORIZATION)
    if status == 429:
        hint = response.headers.get("retry-after", "")
        return fail(
            ErrorCategory.RATE_LIMITED,
            retry_after=float(hint) if hint.isdigit() else THROTTLE_RETRY_SECONDS,
        )
    if status == 426:
        return PublishError(
            ErrorCategory.PROVIDER_REJECTED,
            "LinkedIn no longer serves the configured API version; the operator must "
            "raise settings.api_version",
            provider_code="426",
        )
    if status in (413, 415) or code.startswith("MEDIA_ASSET_PROCESSING"):
        return fail(ErrorCategory.MEDIA_REJECTED)
    if code in ("MEDIA_ASSET_WAITING_UPLOAD", "EXPIRED_UPLOAD_URL"):
        return fail(ErrorCategory.MEDIA_UNREACHABLE)
    if status in (409, 500, 502, 503, 504):
        return fail(ErrorCategory.PROVIDER_UNAVAILABLE)
    if status == 400:
        return fail(ErrorCategory.INVALID_REQUEST)
    return fail(ErrorCategory.PROVIDER_REJECTED)  # 404, 405, 422 and anything unforeseen
