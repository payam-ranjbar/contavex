"""Transport-level failure handling shared by every provider.

Extracted in the Phase D review: three providers classify connection
failures identically, and the one distinction that matters everywhere is
whether the request could have reached the vendor.

* Could not connect: nothing was sent. Always safe to retry.
* Anything after that (timeout, reset): the vendor may have acted. For a call
  that creates a post this is `Ambiguous`, never a blind retry; for a call
  that is safe to repeat it is an ordinary retryable error.

What the *response* means stays in each provider: that part is vendor-specific.
"""

from __future__ import annotations

from typing import Any

import httpx2

from contavex.core.errors import ErrorCategory, PublishError
from contavex.observability import log


class Ambiguous(Exception):
    """The request may have reached the vendor, and the result is not known."""


def open_outcome(vendor: str, ambiguous: bool, category: ErrorCategory, what: str) -> Exception:
    """The error for a failure that leaves the outcome open."""
    return Ambiguous(what) if ambiguous else PublishError(category, f"{vendor} {what}")


async def send(
    http: httpx2.AsyncClient,
    method: str,
    url: str,
    *,
    vendor: str,
    operation: str,
    ambiguous: bool = False,
    **kwargs: Any,
) -> httpx2.Response:
    """Send one request. `operation` is logged; the URL never is, because
    vendor URLs can contain credentials."""
    event = f"{vendor.lower()}_request"
    try:
        return await http.request(method, url, **kwargs)
    except (httpx2.ConnectError, httpx2.ConnectTimeout) as exc:
        log.warning(event, operation=operation, outcome=type(exc).__name__)
        raise PublishError(
            ErrorCategory.PROVIDER_UNAVAILABLE, f"could not connect to {vendor}"
        ) from None
    except httpx2.TimeoutException as exc:
        log.warning(event, operation=operation, outcome=type(exc).__name__)
        raise open_outcome(
            vendor, ambiguous, ErrorCategory.TIMEOUT, "did not respond in time"
        ) from None
    except httpx2.TransportError as exc:
        log.warning(event, operation=operation, outcome=type(exc).__name__)
        raise open_outcome(
            vendor, ambiguous, ErrorCategory.PROVIDER_UNAVAILABLE, "connection failed mid-request"
        ) from None
