"""Instagram publishing through "Instagram API with Instagram Login".

Facts this module relies on are in docs/INTEGRATIONS/instagram.md. In short:

* Publishing is three calls with waiting in between: create a container,
  poll it until Meta has fetched and processed the media, then publish it.
* Meta fetches media itself, so every file must be at a public URL. Uploaded
  media is staged first (see `content_fleet.staging`).
* Creating containers and polling are repeatable. `media_publish` is not,
  and there is no idempotency key. After an ambiguous publish the container
  itself tells us what happened (`status_code == PUBLISHED`), so this
  provider can usually reconcile instead of reporting "unknown".
* The access token lasts 60 days and is refreshed by the service.

The flow is a small state machine; `state["step"]` names the next step.
"""

from __future__ import annotations

import re
import time
from typing import Any

import httpx2
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from content_fleet.config import AccountConfig
from content_fleet.core.capabilities import Capabilities, KindCapability, MediaTypeCapability
from content_fleet.core.errors import ErrorCategory, PublishError
from content_fleet.core.models import (
    MediaItem,
    MediaSource,
    MediaType,
    PublicationRequest,
    PublicationResult,
    PublishedPost,
)
from content_fleet.credentials import CredentialState, RefreshedCredential
from content_fleet.observability import log
from content_fleet.providers import http
from content_fleet.providers.base import (
    Continue,
    Done,
    Provider,
    ProviderContext,
    StepOutcome,
    Unknown,
)
from content_fleet.providers.http import Ambiguous
from content_fleet.secrets import Secret

MB = 1024 * 1024
DAY = 86400.0
DEFAULT_API_BASE = "https://graph.instagram.com"
# The Instagram reference pages document v25.0 on this host; whether v26.0 is
# served there is unverified. Configurable per account (settings.api_version).
DEFAULT_API_VERSION = "v25.0"

# Meta suggests polling about once a minute for up to five minutes. Images
# are usually ready at once, so the first polls come sooner.
POLL_DELAYS = (2.0, 5.0, 10.0, 20.0, 30.0)
POLL_STEADY = 60.0
MAX_POLLS = 45  # roughly 40 minutes of processing before giving up
MAX_RECOVER_TRIES = 5
QUOTA_RETRY_SECONDS = 1800.0
THROTTLE_RETRY_SECONDS = 300.0
REFRESH_MIN_AGE = DAY  # Meta refuses to refresh a token younger than 24 hours
REFRESH_WHEN_LEFT = 30 * DAY

_HASHTAG = re.compile(r"(?<!\w)#\w+")
_MENTION = re.compile(r"(?<!\w)@[\w.]+")

_THROTTLED = {4, 17, 32, 613} | set(range(80000, 80010))
_MEDIA_REJECTED = {36000, 36001, 36003, 352}
_TRANSIENT = {1, 2, -1}
NOT_READY = 9007


class InstagramOptions(BaseModel):
    """`options.instagram` of a publication request."""

    model_config = ConfigDict(extra="forbid")

    share_to_feed: bool | None = Field(
        default=None, description="Reels only: also show the reel in the feed."
    )
    thumb_offset_ms: int | None = Field(
        default=None, ge=0, description="Reels only: frame to use as the cover."
    )


def _poll_delay(polls: int) -> float:
    return POLL_DELAYS[polls] if polls < len(POLL_DELAYS) else POLL_STEADY


class InstagramLogin(Provider):
    platform = "instagram"
    implementation = "instagram-login"
    mutable_credentials = frozenset({"access_token"})

    def __init__(self, *, staging_available: bool = False) -> None:
        # Without a stager the service cannot give Meta a URL for uploaded
        # files, so only media that is already at a public URL is accepted.
        self._staging_available = staging_available

    # ---------------------------------------------------------------- description

    def capabilities(self, account: AccountConfig) -> Capabilities:
        sources = [MediaSource.URL]
        if self._staging_available:
            sources.insert(0, MediaSource.UPLOAD)
        visual = [MediaType.IMAGE, MediaType.VIDEO]
        return Capabilities(
            kinds={
                # One image, or a carousel of 2-10 images and videos.
                "post": KindCapability(min_media=1, max_media=10, media_types=visual),
                "reel": KindCapability(min_media=1, max_media=1, media_types=[MediaType.VIDEO]),
                "story": KindCapability(min_media=1, max_media=1, media_types=visual),
            },
            text_formats=["plain"],
            max_text_length=None,  # no kind accepts text without media
            max_caption_length=2200,
            media={
                MediaType.IMAGE: MediaTypeCapability(
                    sources=sources, max_bytes=8 * MB, content_types=["image/jpeg"]
                ),
                MediaType.VIDEO: MediaTypeCapability(
                    sources=sources,
                    max_bytes=300 * MB,
                    content_types=["video/mp4", "video/quicktime"],
                ),
            },
            editable=[],  # the API cannot edit a published post
            deletable=False,  # only the Facebook Login variant can delete
            options_schema=InstagramOptions.model_json_schema(),
        )

    def validate_account(self, account: AccountConfig) -> None:
        user_id = account.settings.get("ig_user_id")
        if not isinstance(user_id, str | int) or isinstance(user_id, bool) or not str(user_id):
            raise ValueError(
                "settings.ig_user_id is required: the Instagram professional account id"
            )
        if set(account.credentials) != {"access_token"}:
            raise ValueError("credentials must contain exactly one role: access_token")
        unknown = set(account.settings) - {"ig_user_id", "api_base", "api_version"}
        if unknown:
            raise ValueError(f"unknown settings: {sorted(unknown)}")

    def validate(self, account: AccountConfig, request: PublicationRequest) -> None:
        self._options(request)
        text = request.text or ""
        if request.kind == "post" and len(request.media) == 1:
            if request.media[0].type is MediaType.VIDEO:
                raise PublishError(
                    ErrorCategory.UNSUPPORTED,
                    "Instagram publishes a single video as a reel; use kind 'reel'",
                )
        if request.kind == "story" and text.strip():
            raise PublishError(ErrorCategory.UNSUPPORTED, "Instagram stories cannot have a caption")
        if len(_HASHTAG.findall(text)) > 30:
            raise PublishError(
                ErrorCategory.INVALID_REQUEST, "a caption can have at most 30 hashtags"
            )
        if len(_MENTION.findall(text)) > 20:
            raise PublishError(
                ErrorCategory.INVALID_REQUEST, "a caption can have at most 20 mentions"
            )
        for item in request.media:
            if item.url is not None and not item.url.isascii():
                raise PublishError(
                    ErrorCategory.INVALID_REQUEST, "media URLs must contain only ASCII characters"
                )

    @staticmethod
    def _options(request: PublicationRequest) -> InstagramOptions:
        try:
            return InstagramOptions.model_validate(request.options.get("instagram", {}))
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
            )
            raise PublishError(
                ErrorCategory.INVALID_REQUEST, f"options.instagram: {problems}"
            ) from None

    # ---------------------------------------------------------------- state machine

    def step_is_repeatable(self, request: PublicationRequest, state: dict[str, Any]) -> bool:
        return bool(state.get("step", "stage") != "publish")

    async def advance(
        self, ctx: ProviderContext, request: PublicationRequest, state: dict[str, Any]
    ) -> StepOutcome:
        steps = {
            "stage": self._stage,
            "create": self._create,
            "wait_children": self._wait_children,
            "create_parent": self._create_parent,
            "wait": self._wait,
            "publish": self._publish,
            "recover": self._recover,
        }
        return await steps[state.get("step", "stage")](ctx, request, state)

    async def reconcile(
        self, ctx: ProviderContext, request: PublicationRequest, state: dict[str, Any]
    ) -> StepOutcome | None:
        if "container" not in state:
            return None
        return await self._recover(ctx, request, state)

    async def _stage(
        self, ctx: ProviderContext, request: PublicationRequest, state: dict[str, Any]
    ) -> StepOutcome:
        """Make every uploaded file reachable at a public URL."""
        urls: list[str] = []
        for item in request.media:
            if item.url is not None:
                urls.append(item.url)
                continue
            assert item.media_id is not None
            media = ctx.media.get(item.media_id)
            if media is None:
                raise PublishError(
                    ErrorCategory.MEDIA_UNREACHABLE,
                    f"uploaded media {item.media_id} is no longer available",
                    retryable=False,
                )
            if ctx.stager is None:
                raise PublishError(
                    ErrorCategory.UNSUPPORTED,
                    "uploaded media needs a [staging] configuration to reach Instagram",
                )
            urls.append(await ctx.stager.stage(media))
        return Continue({"step": "create", "urls": urls})

    async def _create(
        self, ctx: ProviderContext, request: PublicationRequest, state: dict[str, Any]
    ) -> StepOutcome:
        options = self._options(request)
        urls: list[str] = state["urls"]
        if len(request.media) == 1:
            item, url = request.media[0], urls[0]
            body = _single_container(request, item, url, options)
            container = await self._create_container(ctx, body)
            return Continue({**state, "step": "wait", "container": container, "polls": 0}, 2.0)

        # Carousel: one child container per item, then a parent that lists them.
        children: list[str] = list(state.get("children", []))
        for index in range(len(children), len(request.media)):
            item, url = request.media[index], urls[index]
            body = {"is_carousel_item": True}
            if item.type is MediaType.VIDEO:
                body |= {"media_type": "VIDEO", "video_url": url}
            else:
                body |= {"image_url": url}
                if item.alt_text:
                    body["alt_text"] = item.alt_text
            children.append(await self._create_container(ctx, body))
            # A crash now resumes with the children made so far, not from zero.
            ctx.checkpoint({**state, "children": children})
        has_video = any(item.type is MediaType.VIDEO for item in request.media)
        next_step = "wait_children" if has_video else "create_parent"
        return Continue({**state, "step": next_step, "children": children, "polls": 0}, 2.0)

    async def _wait_children(
        self, ctx: ProviderContext, request: PublicationRequest, state: dict[str, Any]
    ) -> StepOutcome:
        for child, item in zip(state["children"], request.media, strict=True):
            if item.type is not MediaType.VIDEO:
                continue
            status, detail = await self._container_status(ctx, child)
            if status == "IN_PROGRESS":
                return self._keep_waiting(state)
            if status != "FINISHED":
                raise _processing_failed(status, detail)
        return Continue({**state, "step": "create_parent", "polls": 0})

    async def _create_parent(
        self, ctx: ProviderContext, request: PublicationRequest, state: dict[str, Any]
    ) -> StepOutcome:
        body: dict[str, Any] = {"media_type": "CAROUSEL", "children": ",".join(state["children"])}
        if request.text:
            body["caption"] = request.text
        container = await self._create_container(ctx, body)
        return Continue({**state, "step": "wait", "container": container, "polls": 0}, 2.0)

    async def _wait(
        self, ctx: ProviderContext, request: PublicationRequest, state: dict[str, Any]
    ) -> StepOutcome:
        status, detail = await self._container_status(ctx, state["container"])
        if status == "IN_PROGRESS":
            return self._keep_waiting(state)
        if status == "PUBLISHED":
            return await self._recover(ctx, request, state)
        if status == "EXPIRED" and not state.get("recreated"):
            # 24 hours passed (a long rate-limit wait, for instance). Build
            # the containers again, once.
            return Continue({"step": "create", "urls": state["urls"], "recreated": True})
        if status != "FINISHED":
            raise _processing_failed(status, detail)
        await self._check_quota(ctx)
        return Continue({**state, "step": "publish"})

    @staticmethod
    def _keep_waiting(state: dict[str, Any]) -> StepOutcome:
        polls = int(state.get("polls", 0))
        if polls >= MAX_POLLS:
            raise PublishError(
                ErrorCategory.TIMEOUT,
                "Instagram was still processing the media after 40 minutes",
                retryable=True,
                final=True,
            )
        return Continue({**state, "polls": polls + 1}, _poll_delay(polls))

    async def _publish(
        self, ctx: ProviderContext, request: PublicationRequest, state: dict[str, Any]
    ) -> StepOutcome:
        """The one call that must not be repeated blindly."""
        try:
            body = await self._request(
                ctx,
                "POST",
                f"/{self._user(ctx)}/media_publish",
                "media_publish",
                json={"creation_id": state["container"]},
                ambiguous=True,
            )
        except Ambiguous:
            return await self._recover(ctx, request, state)
        except PublishError as exc:
            if exc.provider_code == str(NOT_READY):
                return Continue({**state, "step": "wait"}, _poll_delay(0))
            if exc.retryable and exc.category is ErrorCategory.PROVIDER_UNAVAILABLE:
                # A "temporary" server error does not say whether the post was made.
                return await self._recover(ctx, request, state)
            raise
        media_id = body.get("id")
        if not media_id:
            return await self._recover(ctx, request, state)
        return await self._done(ctx, str(media_id))

    async def _recover(
        self, ctx: ProviderContext, request: PublicationRequest, state: dict[str, Any]
    ) -> StepOutcome:
        """Find out whether an unconfirmed publish happened, from the container."""
        tries = int(state.get("recover_tries", 0))
        try:
            status, detail = await self._container_status(ctx, state["container"])
        except PublishError as exc:
            if not exc.retryable:
                raise
            if tries >= MAX_RECOVER_TRIES:
                return Unknown(
                    "Instagram did not confirm the post and could not be asked whether it "
                    f"exists ({exc.message})",
                    state,
                )
            return Continue({**state, "step": "recover", "recover_tries": tries + 1}, 30.0)

        if status == "PUBLISHED":
            return await self._find_published(ctx, request)
        if status in ("FINISHED", "IN_PROGRESS"):
            if not state.get("recover_confirmed"):
                # Look twice, 30 seconds apart, before concluding the publish
                # never happened and trying it again.
                return Continue({**state, "step": "recover", "recover_confirmed": True}, 30.0)
            cleaned = {k: v for k, v in state.items() if not k.startswith("recover_")}
            return Continue({**cleaned, "step": "wait"})
        raise _processing_failed(status, detail)

    async def _find_published(
        self, ctx: ProviderContext, request: PublicationRequest
    ) -> StepOutcome:
        """The container says it was published; look up the resulting media."""
        try:
            body = await self._request(
                ctx,
                "GET",
                f"/{self._user(ctx)}/media",
                "list_media",
                params={"fields": "id,caption,permalink", "limit": "10"},
            )
            for media in body.get("data", []):
                if (media.get("caption") or "") == (request.text or ""):
                    return Done(
                        PublicationResult(
                            posts=[PublishedPost(id=str(media["id"]), url=media.get("permalink"))]
                        )
                    )
        except PublishError, KeyError, TypeError, AttributeError:
            pass
        # It is published, we just cannot say which media object it became
        # (stories, for one, are not listed).
        return Done(PublicationResult(posts=[]))

    async def _done(self, ctx: ProviderContext, media_id: str) -> StepOutcome:
        permalink: str | None = None
        try:
            body = await self._request(
                ctx, "GET", f"/{media_id}", "get_media", params={"fields": "permalink"}
            )
            permalink = body.get("permalink")
        except PublishError:
            pass  # the post exists; a missing link must not fail it
        return Done(PublicationResult(posts=[PublishedPost(id=media_id, url=permalink)]))

    # ---------------------------------------------------------------- calls

    @staticmethod
    def _user(ctx: ProviderContext) -> str:
        return str(ctx.account.settings["ig_user_id"])

    async def _create_container(self, ctx: ProviderContext, body: dict[str, Any]) -> str:
        result = await self._request(
            ctx, "POST", f"/{self._user(ctx)}/media", "create_container", json=body
        )
        container = result.get("id")
        if not container:
            raise PublishError(
                ErrorCategory.MALFORMED_PROVIDER_RESPONSE, "Instagram returned no container id"
            )
        return str(container)

    async def _container_status(self, ctx: ProviderContext, container: str) -> tuple[str, str]:
        body = await self._request(
            ctx, "GET", f"/{container}", "container_status", params={"fields": "status_code,status"}
        )
        return str(body.get("status_code", "")), str(body.get("status", ""))

    async def _check_quota(self, ctx: ProviderContext) -> None:
        """Do not spend a publish attempt when the daily quota is used up."""
        try:
            body = await self._request(
                ctx,
                "GET",
                f"/{self._user(ctx)}/content_publishing_limit",
                "publishing_limit",
                params={"fields": "quota_usage,config"},
            )
            entry = body["data"][0]
            used, total = int(entry["quota_usage"]), int(entry["config"]["quota_total"])
        except PublishError as exc:
            if exc.category is ErrorCategory.AUTHENTICATION:
                raise
            return  # advisory only; media_publish enforces the limit anyway
        except KeyError, IndexError, TypeError, ValueError:
            return
        if used >= total:
            raise PublishError(
                ErrorCategory.RATE_LIMITED,
                f"Instagram publishing quota is used up ({used} of {total} in 24 hours)",
                retry_after=QUOTA_RETRY_SECONDS,
            )

    async def refresh_credentials(
        self, ctx: ProviderContext, states: dict[str, CredentialState], now: float
    ) -> list[RefreshedCredential]:
        state = states["access_token"]
        if state.obtained_at is None or now - state.obtained_at < REFRESH_MIN_AGE:
            return []
        if state.expires_at is not None and state.expires_at - now > REFRESH_WHEN_LEFT:
            return []
        base = str(ctx.account.settings.get("api_base", DEFAULT_API_BASE)).rstrip("/")
        body = await self._request(
            ctx,
            "GET",
            f"{base}/refresh_access_token",
            "refresh_token",
            # This endpoint is documented with the token as a parameter.
            params={
                "grant_type": "ig_refresh_token",
                "access_token": ctx.credentials["access_token"].reveal(),
            },
            authenticated=False,
        )
        token, expires_in = body.get("access_token"), body.get("expires_in")
        if not isinstance(token, str) or not token:
            raise PublishError(
                ErrorCategory.MALFORMED_PROVIDER_RESPONSE, "Instagram returned no refreshed token"
            )
        expires_at = now + float(expires_in) if isinstance(expires_in, int | float) else None
        return [RefreshedCredential("access_token", Secret(token), expires_at)]

    async def _request(
        self,
        ctx: ProviderContext,
        method: str,
        path: str,
        operation: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        ambiguous: bool = False,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        """Call the Graph API. `operation` is what gets logged, never the URL.

        With `ambiguous=True` a failure that leaves the outcome open raises
        `Ambiguous`; otherwise it is an ordinary retryable error.
        """
        if path.startswith("https://"):
            url = path
        else:
            base = str(ctx.account.settings.get("api_base", DEFAULT_API_BASE)).rstrip("/")
            version = ctx.account.settings.get("api_version", DEFAULT_API_VERSION)
            url = f"{base}/{version}{path}"
        headers = {}
        if authenticated:
            # In a header, so the token never becomes part of a URL.
            headers["authorization"] = f"Bearer {ctx.credentials['access_token'].reveal()}"
        started = time.perf_counter()

        def open_outcome(category: ErrorCategory, what: str) -> Exception:
            return http.open_outcome("Instagram", ambiguous, category, what)

        response = await http.send(
            ctx.http,
            method,
            url,
            vendor="Instagram",
            operation=operation,
            ambiguous=ambiguous,
            params=params,
            json=json,
            headers=headers,
        )

        status = response.status_code
        try:
            body = response.json()
            if not isinstance(body, dict):
                raise TypeError
        except ValueError, TypeError:
            log.warning(
                "instagram_request", operation=operation, status=status, outcome="unreadable"
            )
            if status < 300 or status >= 500:
                raise open_outcome(
                    ErrorCategory.MALFORMED_PROVIDER_RESPONSE,
                    f"returned an unreadable response (HTTP {status})",
                ) from None
            raise PublishError(
                ErrorCategory.PROVIDER_REJECTED,
                f"Instagram refused the request (HTTP {status})",
                provider_code=str(status),
            ) from None

        error = body.get("error")
        log.info(
            "instagram_request",
            operation=operation,
            status=status,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
            provider_request_id=error.get("fbtrace_id") if isinstance(error, dict) else None,
        )
        if status < 300 and not error:
            return body
        if not isinstance(error, dict):
            if status >= 500:
                raise open_outcome(ErrorCategory.PROVIDER_UNAVAILABLE, f"failed with HTTP {status}")
            raise PublishError(
                ErrorCategory.PROVIDER_REJECTED,
                f"Instagram refused the request (HTTP {status})",
                provider_code=str(status),
            )
        raise _classify(error, response.headers)


# ---------------------------------------------------------------- helpers


def _single_container(
    request: PublicationRequest, item: MediaItem, url: str, options: InstagramOptions
) -> dict[str, Any]:
    is_video = item.type is MediaType.VIDEO
    body: dict[str, Any] = {"video_url" if is_video else "image_url": url}
    if request.kind == "story":
        body["media_type"] = "STORIES"
        return body
    if request.kind == "reel":
        body["media_type"] = "REELS"
        if options.share_to_feed is not None:
            body["share_to_feed"] = options.share_to_feed
        if options.thumb_offset_ms is not None:
            body["thumb_offset"] = options.thumb_offset_ms
    elif item.alt_text:
        body["alt_text"] = item.alt_text
    if request.text:
        body["caption"] = request.text
    return body


def _processing_failed(status: str, detail: str) -> PublishError:
    if status == "EXPIRED":
        return PublishError(
            ErrorCategory.TIMEOUT,
            "the Instagram media container expired before it could be published",
            retryable=True,
            final=True,
        )
    return PublishError(
        ErrorCategory.MEDIA_REJECTED,
        f"Instagram could not process the media ({status or 'no status'}): {detail}".rstrip(": "),
    )


def _throttle_wait(headers: httpx2.Headers) -> float:
    """Minutes until access returns, from the usage header, as seconds."""
    import json

    try:
        usage = json.loads(headers.get("x-business-use-case-usage", ""))
        minutes = max(
            float(entry.get("estimated_time_to_regain_access", 0))
            for entries in usage.values()
            for entry in entries
        )
    except ValueError, TypeError, AttributeError:
        return THROTTLE_RETRY_SECONDS
    return minutes * 60 if minutes > 0 else THROTTLE_RETRY_SECONDS


def _classify(error: dict[str, Any], headers: httpx2.Headers) -> PublishError:
    """Map a Graph API error to the service's vocabulary.

    Classified on `code` and `error_subcode`, per the table in
    docs/INTEGRATIONS/instagram.md. Unknown codes are permanent unless Meta
    marks them transient.
    """
    code, subcode = error.get("code"), error.get("error_subcode")
    message = error.get("error_user_msg") or error.get("message") or "no error message"

    def fail(category: ErrorCategory, **kwargs: Any) -> PublishError:
        return PublishError(
            category,
            f"Instagram: {message}",
            provider_code=str(code) if subcode is None else f"{code}/{subcode}",
            provider_request_id=error.get("fbtrace_id"),
            **kwargs,
        )

    if code == NOT_READY:
        # Raised with the bare code so the publish step can recognise it.
        exc = fail(ErrorCategory.PROVIDER_UNAVAILABLE)
        exc.provider_code = str(NOT_READY)
        return exc
    if code in (190, 102):
        return fail(ErrorCategory.AUTHENTICATION)
    if code in (10, 3) or (isinstance(code, int) and 200 <= code <= 299):
        return fail(ErrorCategory.AUTHORIZATION)
    if code == 4 and subcode == 2207051:
        return fail(ErrorCategory.PROVIDER_REJECTED)  # flagged as spam; not a throttle
    if code == 9:
        return fail(ErrorCategory.RATE_LIMITED, retry_after=QUOTA_RETRY_SECONDS)
    if code in _THROTTLED:
        return fail(ErrorCategory.RATE_LIMITED, retry_after=_throttle_wait(headers))
    if code in (9004, -2):
        return fail(ErrorCategory.MEDIA_UNREACHABLE)  # Meta could not download the media
    if code in _MEDIA_REJECTED:
        return fail(ErrorCategory.MEDIA_REJECTED)
    if code in (100, 36004) or subcode == 2207057:
        return fail(ErrorCategory.INVALID_REQUEST)
    if code in (25, 368, 506):
        return fail(ErrorCategory.PROVIDER_REJECTED)
    if code in _TRANSIENT or code == 24 or error.get("is_transient") is True:
        return fail(ErrorCategory.PROVIDER_UNAVAILABLE)
    return fail(ErrorCategory.PROVIDER_REJECTED)
