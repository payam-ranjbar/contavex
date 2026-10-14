"""The consumer-facing publishing model."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from contavex.core.errors import ErrorCategory


class MediaType(StrEnum):
    IMAGE = "image"
    VIDEO = "video"
    DOCUMENT = "document"
    AUDIO = "audio"
    ANIMATION = "animation"


class MediaSource(StrEnum):
    UPLOAD = "upload"  # bytes previously sent to POST /v1/media
    URL = "url"  # a URL the provider fetches itself


class MediaItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: MediaType
    media_id: str | None = Field(default=None, description="Id returned by POST /v1/media.")
    url: str | None = Field(
        default=None,
        pattern=r"^https://",
        max_length=2048,
        description="Only for accounts whose capabilities list the `url` media source.",
    )
    alt_text: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _one_source(self) -> Self:
        if (self.media_id is None) == (self.url is None):
            raise ValueError("exactly one of media_id or url is required")
        return self

    @property
    def source(self) -> MediaSource:
        return MediaSource.UPLOAD if self.media_id is not None else MediaSource.URL


class PublicationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account: str = Field(description="Id of a configured account; see GET /v1/accounts.")
    kind: str = Field(
        default="post",
        description="What to publish. Each account's capabilities list the kinds it accepts.",
    )
    text: str | None = Field(default=None, description="Body text, or caption when media is set.")
    text_format: str = Field(default="plain")
    media: list[MediaItem] = []
    options: dict[str, dict[str, Any]] = Field(
        default={},
        description=(
            "Platform-specific options keyed by platform name. Only the target "
            "platform's entry is used; it is validated strictly."
        ),
    )
    title: str | None = Field(
        default=None,
        max_length=300,
        description="Headline, for kinds that have one (an article). See capabilities.",
    )
    client_reference: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _has_content(self) -> Self:
        if not self.media and not (self.text and self.text.strip()):
            raise ValueError("a publication needs text, media, or both")
        return self


class PublicationStatus(StrEnum):
    ACCEPTED = "accepted"
    PROCESSING = "processing"
    PUBLISHED = "published"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"
    DELETED = "deleted"  # was published, then removed from the platform on request

    @property
    def terminal(self) -> bool:
        return self in (
            PublicationStatus.PUBLISHED,
            PublicationStatus.FAILED,
            PublicationStatus.OUTCOME_UNKNOWN,
            PublicationStatus.DELETED,
        )


class PublishedPost(BaseModel):
    id: str = Field(description="The provider's identifier for the post.")
    url: str | None = None


class PublicationResult(BaseModel):
    # More than one entry when a single request produces several provider
    # objects (a Telegram album is one message per item).
    posts: list[PublishedPost]


class ErrorInfo(BaseModel):
    category: ErrorCategory
    retryable: bool = Field(
        description=(
            "On a failed publication: whether submitting it again could succeed. "
            "On a processing publication: always true, the service is retrying."
        )
    )
    message: str
    provider_code: str | None = None
    provider_request_id: str | None = None


class Publication(BaseModel):
    id: str
    account: str
    platform: str
    status: PublicationStatus
    client_reference: str | None = None
    attempts: int = Field(description="Failed tries of the current step.")
    next_attempt_at: datetime | None = None
    result: PublicationResult | None = None
    error: ErrorInfo | None = Field(
        default=None,
        description="Final error when failed; most recent transient error while processing.",
    )
    created_at: datetime
    updated_at: datetime


class Media(BaseModel):
    id: str
    content_type: str
    size: int
    sha256: str
    filename: str | None = None
    created_at: datetime
    expires_at: datetime


class PublicationEdit(BaseModel):
    """Changes to an already published post. Which fields an account can
    edit is listed in its capabilities."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    text_format: str | None = None


class Resolution(BaseModel):
    """How an `outcome_unknown` publication actually turned out."""

    model_config = ConfigDict(extra="forbid")

    published: bool
    post_id: str | None = Field(default=None, description="Provider post id, if known.")
    post_url: str | None = None
