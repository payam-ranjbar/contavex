"""What an account can do, and the checks that follow from it.

Capabilities are data: served to consumers for discovery (F-5) and used here
to refuse requests the target cannot honour (F-4). Rules that cannot be
expressed as data live in the provider's own `validate`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, Field

from contavex.core.errors import ApiProblem
from contavex.core.models import MediaSource, MediaType, PublicationRequest


class MediaTypeCapability(BaseModel):
    sources: list[MediaSource]
    max_bytes: int | None = Field(default=None, description="Limit for uploaded media.")
    content_types: list[str] | None = Field(
        default=None, description="Accepted MIME types; null means not restricted here."
    )


class KindCapability(BaseModel):
    min_media: int = 0
    max_media: int = 0
    media_types: list[MediaType] = []
    text_required: bool = False
    title: bool = Field(default=False, description="Whether this kind takes a `title`.")
    max_text_length: int | None = Field(
        default=None, description="Text limit for this kind, when it differs from the account's."
    )


class Capabilities(BaseModel):
    kinds: dict[str, KindCapability]
    text_formats: list[str] = ["plain"]
    max_text_length: int | None = Field(default=None, description="Text without media.")
    max_caption_length: int | None = Field(default=None, description="Text with media.")
    media: dict[MediaType, MediaTypeCapability] = {}
    editable: list[str] = Field(default=[], description="Fields that can be edited afterwards.")
    deletable: bool = False
    options_schema: dict[str, Any] = Field(
        default={}, description="JSON Schema of this platform's `options` entry."
    )


class UploadedMedia(BaseModel):
    """The facts about an uploaded file that capability checks need."""

    id: str
    content_type: str
    size: int


def _unsupported(detail: str, capability: str) -> ApiProblem:
    return ApiProblem(
        422,
        "unsupported-capability",
        "The target account does not support this request",
        detail,
        capability=capability,
    )


def check_request(
    caps: Capabilities,
    request: PublicationRequest,
    uploads: Mapping[str, UploadedMedia],
) -> None:
    """Raise `ApiProblem` for the first thing the account cannot do."""
    kind = caps.kinds.get(request.kind)
    if kind is None:
        raise _unsupported(
            f"kind {request.kind!r} is not supported; supported: {sorted(caps.kinds)}",
            f"kind:{request.kind}",
        )
    if request.text_format not in caps.text_formats:
        raise _unsupported(
            f"text_format {request.text_format!r} is not supported; supported: {caps.text_formats}",
            f"text_format:{request.text_format}",
        )

    count = len(request.media)
    if not kind.min_media <= count <= kind.max_media:
        raise _unsupported(
            f"kind {request.kind!r} takes {kind.min_media} to {kind.max_media} media items, "
            f"got {count}",
            "media_count",
        )
    text = request.text or ""
    if kind.text_required and not text.strip():
        raise _unsupported(f"kind {request.kind!r} requires text", "text_required")
    if request.title is not None and not kind.title:
        raise _unsupported(f"kind {request.kind!r} does not take a title", "title")
    limit = kind.max_text_length or (caps.max_caption_length if count else caps.max_text_length)
    if limit is not None and len(text) > limit:
        raise _unsupported(
            f"text is {len(text)} characters; the limit is {limit}"
            + (" when media is attached" if count and not kind.max_text_length else ""),
            "text_length",
        )

    for index, item in enumerate(request.media):
        where = f"media[{index}]"
        type_caps = caps.media.get(item.type)
        if item.type not in kind.media_types or type_caps is None:
            raise _unsupported(
                f"{where}: {item.type.value} is not supported for kind {request.kind!r}",
                f"media_type:{item.type.value}",
            )
        if item.source not in type_caps.sources:
            raise _unsupported(
                f"{where}: {item.type.value} cannot be supplied by {item.source.value}; "
                f"accepted: {[s.value for s in type_caps.sources]}",
                f"media_source:{item.source.value}",
            )
        if item.media_id is None:
            continue
        upload = uploads[item.media_id]
        if type_caps.max_bytes is not None and upload.size > type_caps.max_bytes:
            raise _unsupported(
                f"{where}: file is {upload.size} bytes; the limit for "
                f"{item.type.value} is {type_caps.max_bytes}",
                "media_size",
            )
        if type_caps.content_types is not None and upload.content_type not in (
            type_caps.content_types
        ):
            raise _unsupported(
                f"{where}: content type {upload.content_type!r} is not accepted for "
                f"{item.type.value}; accepted: {type_caps.content_types}",
                "media_content_type",
            )
