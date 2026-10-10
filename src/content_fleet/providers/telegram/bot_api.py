"""Telegram channel publishing through the official Bot API, over plain HTTPS.

Facts this module relies on are in docs/INTEGRATIONS/telegram.md (Bot API
10.3). The important ones:

* A send is one synchronous call that returns the created message(s).
* There is no idempotency key and a bot cannot read channel history, so a
  send whose response was lost can neither be repeated safely nor checked.
  Such a send ends as `Unknown`, never as an automatic retry.
* The token is part of the URL path. Request URLs are never logged or put
  into an error message.
"""

from __future__ import annotations

import json
import time
from contextlib import ExitStack
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

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
from content_fleet.observability import log
from content_fleet.providers import http
from content_fleet.providers.base import Done, Provider, ProviderContext, StepOutcome, Unknown
from content_fleet.providers.http import Ambiguous, open_outcome

MB = 1024 * 1024
DEFAULT_API_BASE = "https://api.telegram.org"

# media type -> (send method, name of the file parameter, InputMedia type)
_SEND: dict[MediaType, tuple[str, str, str]] = {
    MediaType.IMAGE: ("sendPhoto", "photo", "photo"),
    MediaType.VIDEO: ("sendVideo", "video", "video"),
    MediaType.DOCUMENT: ("sendDocument", "document", "document"),
    MediaType.ANIMATION: ("sendAnimation", "animation", "animation"),
}

# Substrings of `description` used only to refine a 400. Telegram publishes
# no error catalogue and says descriptions may change, so an unrecognised
# 400 falls back to a permanent invalid_request.
_RIGHTS = ("not enough rights", "need administrator rights", "chat_write_forbidden")
_URL_FETCH = ("failed to get http url content", "wrong type of the web page content")
_GONE = ("message to delete not found", "message to edit not found")


class TelegramOptions(BaseModel):
    """`options.telegram` of a publication request."""

    model_config = ConfigDict(extra="forbid")

    disable_notification: bool = False
    protect_content: bool = False
    disable_link_preview: bool = False
    show_caption_above_media: bool = False
    parse_mode: Literal["HTML", "MarkdownV2"] | None = None


class TelegramBotApi(Provider):
    platform = "telegram"
    implementation = "bot-api"

    # ---------------------------------------------------------------- description

    def capabilities(self, account: AccountConfig) -> Capabilities:
        both = [MediaSource.UPLOAD, MediaSource.URL]
        return Capabilities(
            kinds={
                "post": KindCapability(
                    max_media=10,
                    media_types=[
                        MediaType.IMAGE,
                        MediaType.VIDEO,
                        MediaType.DOCUMENT,
                        MediaType.ANIMATION,
                    ],
                )
            },
            text_formats=["plain", "html"],
            max_text_length=4096,
            max_caption_length=1024,
            media={
                MediaType.IMAGE: MediaTypeCapability(sources=both, max_bytes=10 * MB),
                MediaType.VIDEO: MediaTypeCapability(sources=both, max_bytes=50 * MB),
                MediaType.ANIMATION: MediaTypeCapability(sources=both, max_bytes=50 * MB),
                # By URL Telegram only accepts PDF and ZIP documents; uploads have no such rule.
                MediaType.DOCUMENT: MediaTypeCapability(
                    sources=[MediaSource.UPLOAD], max_bytes=50 * MB
                ),
            },
            editable=["text"],
            deletable=True,
            options_schema=TelegramOptions.model_json_schema(),
        )

    def validate_account(self, account: AccountConfig) -> None:
        chat_id = account.settings.get("chat_id")
        if not isinstance(chat_id, str | int) or isinstance(chat_id, bool) or not str(chat_id):
            raise ValueError(
                "settings.chat_id is required: the channel's numeric id (-100...) or @username"
            )
        if set(account.credentials) != {"token"}:
            raise ValueError("credentials must contain exactly one role: token")
        unknown = set(account.settings) - {"chat_id", "api_base"}
        if unknown:
            raise ValueError(f"unknown settings: {sorted(unknown)}")

    def validate(self, account: AccountConfig, request: PublicationRequest) -> None:
        self._options(request)
        types = {item.type for item in request.media}
        if len(request.media) > 1:
            if MediaType.ANIMATION in types:
                raise PublishError(
                    ErrorCategory.UNSUPPORTED, "an animation cannot be part of an album"
                )
            if MediaType.DOCUMENT in types and len(types) > 1:
                raise PublishError(
                    ErrorCategory.UNSUPPORTED,
                    "documents can only be grouped in an album with other documents",
                )

    @staticmethod
    def _options(request: PublicationRequest) -> TelegramOptions:
        try:
            return TelegramOptions.model_validate(request.options.get("telegram", {}))
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
            )
            raise PublishError(
                ErrorCategory.INVALID_REQUEST, f"options.telegram: {problems}"
            ) from None

    # ---------------------------------------------------------------- publish

    async def advance(
        self, ctx: ProviderContext, request: PublicationRequest, state: dict[str, Any]
    ) -> StepOutcome:
        options = self._options(request)
        parse_mode = options.parse_mode or ("HTML" if request.text_format == "html" else None)
        params: dict[str, Any] = {"chat_id": ctx.account.settings["chat_id"]}
        if options.disable_notification:
            params["disable_notification"] = True
        if options.protect_content:
            params["protect_content"] = True

        text: dict[str, Any] = {}
        if request.text:
            text["caption" if request.media else "text"] = request.text
            if parse_mode:
                text["parse_mode"] = parse_mode
            if request.media and options.show_caption_above_media:
                text["show_caption_above_media"] = True

        with ExitStack() as files:
            uploads: dict[str, Any] = {}
            if not request.media:
                method = "sendMessage"
                params |= text
                if options.disable_link_preview:
                    params["link_preview_options"] = {"is_disabled": True}
            elif len(request.media) == 1:
                item = request.media[0]
                method, field, _ = _SEND[item.type]
                params |= text
                params[field] = self._source(ctx, item, field, uploads, files)
            else:
                method = "sendMediaGroup"
                album = []
                for index, item in enumerate(request.media):
                    entry: dict[str, Any] = {
                        "type": _SEND[item.type][2],
                        "media": self._source(ctx, item, f"file{index}", uploads, files),
                    }
                    if index == 0:
                        # An album has no caption of its own; clients show the
                        # first item's caption for the whole album.
                        entry |= text
                    album.append(entry)
                params["media"] = album

            try:
                result = await self._call(ctx, method, params, uploads, send=True)
            except Ambiguous as exc:
                return Unknown(
                    f"Telegram did not confirm the post ({exc}); it may or may not have "
                    "been published, and the Bot API offers no way to check"
                )

        messages = result if isinstance(result, list) else [result]
        try:
            posts = [_post(message) for message in messages]
        except KeyError, TypeError:
            # Telegram said ok, so the post exists; we just cannot name it.
            return Unknown("Telegram accepted the post but its reply could not be read")
        return Done(PublicationResult(posts=[post for post in posts if post is not None]))

    def _source(
        self,
        ctx: ProviderContext,
        item: MediaItem,
        attach_name: str,
        uploads: dict[str, Any],
        files: ExitStack,
    ) -> str:
        """The value Telegram expects for one media item: a URL it fetches, or
        a reference to a multipart part holding the file."""
        if item.url is not None:
            return item.url
        assert item.media_id is not None
        media = ctx.media.get(item.media_id)
        if media is None:
            raise PublishError(
                ErrorCategory.MEDIA_UNREACHABLE,
                f"uploaded media {item.media_id} is no longer available",
                retryable=False,
            )
        handle = files.enter_context(ctx.media.path(media.id).open("rb"))
        uploads[attach_name] = (media.filename or media.id, handle, media.content_type)
        return f"attach://{attach_name}"

    # ---------------------------------------------------------------- edit / delete

    async def edit(
        self,
        ctx: ProviderContext,
        request: PublicationRequest,
        result: PublicationResult,
        changes: dict[str, Any],
    ) -> None:
        if not result.posts:
            raise PublishError(ErrorCategory.UNSUPPORTED, "Telegram returned no handle to edit")
        chat_id, message_id = _handle(result.posts[0])
        parse_mode = "HTML" if changes.get("text_format", request.text_format) == "html" else None
        params: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id}
        if request.media:
            method = "editMessageCaption"
            params["caption"] = changes["text"]
        else:
            method = "editMessageText"
            params["text"] = changes["text"]
        if parse_mode:
            params["parse_mode"] = parse_mode
        await self._call(ctx, method, params, {}, send=False)

    async def delete(self, ctx: ProviderContext, result: PublicationResult) -> None:
        if not result.posts:
            raise PublishError(ErrorCategory.UNSUPPORTED, "Telegram returned no handle to delete")
        handles = [_handle(post) for post in result.posts]
        params = {"chat_id": handles[0][0], "message_ids": [message for _, message in handles]}
        await self._call(ctx, "deleteMessages", params, {}, send=False)

    # ---------------------------------------------------------------- transport

    async def _call(
        self,
        ctx: ProviderContext,
        method: str,
        params: dict[str, Any],
        uploads: dict[str, Any],
        *,
        send: bool,
    ) -> Any:
        """Call one Bot API method.

        `send` marks a call that creates a post: for those, any failure after
        the request may have reached Telegram raises `Ambiguous` instead of
        a retryable error. Edits and deletes converge, so they just retry.
        """
        base = str(ctx.account.settings.get("api_base", DEFAULT_API_BASE)).rstrip("/")
        url = f"{base}/bot{ctx.credentials['token'].reveal()}/{method}"
        started = time.perf_counter()

        def ambiguous_or(category: ErrorCategory, what: str) -> Exception:
            return open_outcome("Telegram", send, category, what)

        if uploads:
            # Multipart fields are strings; structured values travel as JSON.
            fields = {
                key: value if isinstance(value, str) else json.dumps(value)
                for key, value in params.items()
            }
            body: dict[str, Any] = {"data": fields, "files": uploads}
        else:
            body = {"json": params}
        response = await http.send(
            ctx.http, "POST", url, vendor="Telegram", operation=method, ambiguous=send, **body
        )

        status = response.status_code
        log.info(
            "telegram_request",
            operation=method,
            status=status,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        try:
            reply = response.json()
            ok, description = reply["ok"], str(reply.get("description", ""))
            parameters = reply.get("parameters") or {}
            result = reply.get("result")
        except ValueError, KeyError, TypeError, AttributeError:
            if status < 300 or status >= 500:
                raise ambiguous_or(
                    ErrorCategory.MALFORMED_PROVIDER_RESPONSE,
                    f"returned an unreadable response (HTTP {status})",
                ) from None
            reply, ok, description, parameters, result = {}, False, "", {}, None

        if ok is True and result is not None:
            return result
        if ok is True:
            raise ambiguous_or(
                ErrorCategory.MALFORMED_PROVIDER_RESPONSE, "reported success without a result"
            )

        code = reply.get("error_code", status)
        lowered = description.lower()
        fail = _Failure(str(code), description or f"HTTP {status}")
        if code == 429:
            retry_after = parameters.get("retry_after")
            raise fail(
                ErrorCategory.RATE_LIMITED,
                retry_after=float(retry_after) if isinstance(retry_after, int | float) else None,
            )
        if code == 401:
            raise fail(ErrorCategory.AUTHENTICATION)
        if code == 403 or any(marker in lowered for marker in _RIGHTS):
            raise fail(ErrorCategory.AUTHORIZATION)
        if code == 413:
            raise fail(ErrorCategory.MEDIA_REJECTED)
        if isinstance(code, int) and code >= 500:
            raise ambiguous_or(ErrorCategory.PROVIDER_UNAVAILABLE, f"failed with HTTP {code}")
        if not send and ("message is not modified" in lowered or any(m in lowered for m in _GONE)):
            # The edit or delete already holds; repeating it is not an error.
            if "to edit not found" in lowered:
                raise fail(ErrorCategory.PROVIDER_REJECTED)
            return True
        if any(marker in lowered for marker in _URL_FETCH):
            # Telegram could not fetch a media URL; the host may be briefly down.
            raise fail(ErrorCategory.MEDIA_UNREACHABLE)
        if code == 400:
            raise fail(ErrorCategory.INVALID_REQUEST)
        raise fail(ErrorCategory.PROVIDER_REJECTED)


class _Failure:
    """Builds a `PublishError` carrying Telegram's own code and description."""

    def __init__(self, code: str, description: str) -> None:
        self._code = code
        self._description = description

    def __call__(self, category: ErrorCategory, retry_after: float | None = None) -> PublishError:
        return PublishError(
            category,
            f"Telegram: {self._description}",
            retry_after=retry_after,
            provider_code=self._code,
        )


def _post(message: dict[str, Any]) -> PublishedPost | None:
    chat, message_id = message["chat"], int(message["message_id"])
    if message_id == 0:
        # Telegram scheduled the post itself (large video); there is no handle yet.
        return None
    chat_id = int(chat["id"])
    username = chat.get("username")
    if username:
        url = f"https://t.me/{username}/{message_id}"
    elif str(chat_id).startswith("-100"):
        url = f"https://t.me/c/{str(chat_id)[4:]}/{message_id}"
    else:
        url = None
    return PublishedPost(id=f"{chat_id}:{message_id}", url=url)


def _handle(post: PublishedPost) -> tuple[int, int]:
    chat_id, _, message_id = post.id.rpartition(":")
    return int(chat_id), int(message_id)
