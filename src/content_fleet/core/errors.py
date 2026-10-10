"""One error vocabulary for every provider (REQUIREMENTS F-23, F-25)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCategory(StrEnum):
    INVALID_REQUEST = "invalid_request"
    UNSUPPORTED = "unsupported"
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    RATE_LIMITED = "rate_limited"
    MEDIA_REJECTED = "media_rejected"
    MEDIA_UNREACHABLE = "media_unreachable"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    TIMEOUT = "timeout"
    PROVIDER_REJECTED = "provider_rejected"
    MALFORMED_PROVIDER_RESPONSE = "malformed_provider_response"
    INTERNAL = "internal"


# Whether trying the same thing again later can succeed. A provider may
# override per error (e.g. a URL-fetch failure reported as a 400).
DEFAULT_RETRYABLE: dict[ErrorCategory, bool] = {
    ErrorCategory.INVALID_REQUEST: False,
    ErrorCategory.UNSUPPORTED: False,
    ErrorCategory.AUTHENTICATION: False,
    ErrorCategory.AUTHORIZATION: False,
    ErrorCategory.RATE_LIMITED: True,
    ErrorCategory.MEDIA_REJECTED: False,
    ErrorCategory.MEDIA_UNREACHABLE: True,
    ErrorCategory.PROVIDER_UNAVAILABLE: True,
    ErrorCategory.TIMEOUT: True,
    ErrorCategory.PROVIDER_REJECTED: False,
    ErrorCategory.MALFORMED_PROVIDER_RESPONSE: True,
    ErrorCategory.INTERNAL: False,
}


class PublishError(Exception):
    """A classified failure of a provider operation.

    `message` must already be safe to show to a consumer: no credentials, no
    raw request URLs.
    """

    def __init__(
        self,
        category: ErrorCategory,
        message: str,
        *,
        retryable: bool | None = None,
        retry_after: float | None = None,
        provider_code: str | None = None,
        provider_request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.message = message
        self.retryable = DEFAULT_RETRYABLE[category] if retryable is None else retryable
        self.retry_after = retry_after
        self.provider_code = provider_code
        self.provider_request_id = provider_request_id


class ApiProblem(Exception):
    """An RFC 9457 problem returned to a consumer."""

    def __init__(
        self,
        status: int,
        code: str,
        title: str,
        detail: str | None = None,
        **extras: Any,
    ) -> None:
        super().__init__(detail or title)
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail
        self.extras = extras
