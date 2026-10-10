"""AWS Signature Version 4 for single-request S3 calls.

Only what staging needs: sign one PUT or DELETE whose payload hash is
already known. Written against the AWS specification and checked in the
tests against AWS's published worked examples. An SDK is not used: two
requests do not justify one (see docs/DEPENDENCIES.md).
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def sign(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    payload_sha256: str,
    access_key: str,
    secret_key: str,
    region: str,
    when: datetime,
    service: str = "s3",
) -> dict[str, str]:
    """Return `headers` plus the ones that authenticate the request.

    Every header passed in is signed. `url` must already be percent-encoded
    the way it will be sent; the query string must be empty or pre-sorted.
    """
    parts = urlsplit(url)
    amz_date = when.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    date = amz_date[:8]
    signed = {key.lower(): " ".join(value.split()) for key, value in headers.items()}
    signed["host"] = parts.netloc
    signed["x-amz-content-sha256"] = payload_sha256
    signed["x-amz-date"] = amz_date
    names = ";".join(sorted(signed))
    canonical = "\n".join(
        [
            method,
            parts.path or "/",
            parts.query,
            "".join(f"{name}:{signed[name]}\n" for name in sorted(signed)),
            names,
            payload_sha256,
        ]
    )
    scope = f"{date}/{region}/{service}/aws4_request"
    to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()]
    )
    key = _hmac(
        _hmac(_hmac(_hmac(f"AWS4{secret_key}".encode(), date), region), service), "aws4_request"
    )
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    result = dict(headers)
    result["x-amz-content-sha256"] = payload_sha256
    result["x-amz-date"] = amz_date
    result["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={names}, Signature={signature}"
    )
    return result


def encode_key(key: str) -> str:
    """Percent-encode an object key for the URL path, as S3 canonicalises it."""
    return quote(key, safe="/-_.~")
