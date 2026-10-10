"""Stage media by pushing it to an S3-compatible bucket (S3, R2, MinIO, B2).

Two ways to hand the vendor a URL:

* Default, private bucket: a presigned GET URL that expires (an hour unless
  configured). Nothing in the bucket is ever public.
* `public_base_url` set: the bucket, or a CDN in front of it, serves the
  staging prefix publicly and the URL is built from that address.

Either way the object key contains an unguessable token and the object is
deleted when the publication ends.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2

from content_fleet.core.errors import ErrorCategory, PublishError
from content_fleet.media import MediaStore
from content_fleet.secrets import Secret
from content_fleet.staging import Stager
from content_fleet.staging.self_hosted import public_name
from content_fleet.staging.sigv4 import EMPTY_SHA256, encode_key, presign_get, sign
from content_fleet.store.db import MediaRecord

CHUNK = 1024 * 1024
DEFAULT_URL_TTL_SECONDS = 3600
MAX_URL_TTL_SECONDS = 86400  # an Instagram container itself lives 24 hours


async def _chunks(path: Path) -> AsyncIterator[bytes]:
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            yield chunk


class ObjectStorageStager(Stager):
    name = "object-storage"

    def __init__(
        self,
        http: httpx2.AsyncClient,
        media: MediaStore,
        clock: Callable[[], float],
        settings: dict[str, Any],
        credentials: dict[str, Secret],
    ) -> None:
        required = {"endpoint", "bucket", "region"}
        unknown = set(settings) - required - {"prefix", "public_base_url", "url_ttl_seconds"}
        missing = required - set(settings)
        if unknown or missing:
            raise ValueError(
                f"staging settings: missing {sorted(missing)}, unknown {sorted(unknown)}"
            )
        if set(credentials) != {"access_key_id", "secret_access_key"}:
            raise ValueError("staging credentials must be access_key_id and secret_access_key")
        for name in ("endpoint", "public_base_url"):
            if name in settings and not str(settings[name]).startswith("https://"):
                raise ValueError(f"staging settings.{name} must be an https:// URL")
        ttl = settings.get("url_ttl_seconds", DEFAULT_URL_TTL_SECONDS)
        if not isinstance(ttl, int) or not 60 <= ttl <= MAX_URL_TTL_SECONDS:
            raise ValueError(
                f"staging settings.url_ttl_seconds must be 60 to {MAX_URL_TTL_SECONDS}"
            )
        self._ttl = ttl
        self._endpoint = str(settings["endpoint"]).rstrip("/")
        self._bucket = str(settings["bucket"])
        self._region = str(settings["region"])
        public = settings.get("public_base_url")
        self._public = str(public).rstrip("/") if public else None
        self._prefix = str(settings.get("prefix", "staged/")).lstrip("/")
        self._http = http
        self._media = media
        self._clock = clock
        self._credentials = credentials

    def _key(self, media_id: str, name: str = "") -> str:
        # Derived, not stored: the same media always maps to the same
        # unguessable key, so staging twice or unstaging after a restart
        # needs no bookkeeping. The secret key is the HMAC key.
        token = hmac.new(
            self._credentials["secret_access_key"].reveal().encode(),
            media_id.encode(),
            hashlib.sha256,
        ).hexdigest()[:40]
        return f"{self._prefix}{token}/{name}" if name else f"{self._prefix}{token}/"

    async def _send(
        self, method: str, key: str, headers: dict[str, str], sha256: str, body: Any = None
    ) -> None:
        url = f"{self._endpoint}/{self._bucket}/{encode_key(key)}"
        signed = sign(
            method=method,
            url=url,
            headers=headers,
            payload_sha256=sha256,
            access_key=self._credentials["access_key_id"].reveal(),
            secret_key=self._credentials["secret_access_key"].reveal(),
            region=self._region,
            when=datetime.fromtimestamp(self._clock(), UTC),
        )
        try:
            response = await self._http.request(method, url, headers=signed, content=body)
        except httpx2.TransportError as exc:
            raise PublishError(
                ErrorCategory.MEDIA_UNREACHABLE,
                f"media staging: could not reach object storage ({type(exc).__name__})",
            ) from None
        if response.status_code >= 500 or response.status_code == 429:
            raise PublishError(
                ErrorCategory.MEDIA_UNREACHABLE,
                f"media staging: object storage answered HTTP {response.status_code}",
            )
        if response.status_code >= 300 and not (method == "DELETE" and response.status_code == 404):
            # 403 here is a wrong key, bucket or policy: operator configuration.
            raise PublishError(
                ErrorCategory.MEDIA_UNREACHABLE,
                f"media staging: object storage refused the request "
                f"(HTTP {response.status_code}); check the staging configuration",
                retryable=False,
            )

    async def stage(self, media: MediaRecord) -> str:
        name = public_name(media)
        key = self._key(media.id, name)
        await self._send(
            "PUT",
            key,
            {"content-type": media.content_type, "content-length": str(media.size)},
            media.sha256,
            _chunks(self._media.path(media.id)),
        )
        if self._public is not None:
            return f"{self._public}/{encode_key(key)}"
        return presign_get(
            url=f"{self._endpoint}/{self._bucket}/{encode_key(key)}",
            access_key=self._credentials["access_key_id"].reveal(),
            secret_key=self._credentials["secret_access_key"].reveal(),
            region=self._region,
            when=datetime.fromtimestamp(self._clock(), UTC),
            expires=self._ttl,
        )

    async def unstage(self, media_id: str) -> None:
        record = self._media.get(media_id)
        if record is None:
            return
        await self._send("DELETE", self._key(media_id, public_name(record)), {}, EMPTY_SHA256)
