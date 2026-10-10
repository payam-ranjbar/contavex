"""Serve staged media from this service on a separate, public listener.

For operators without object storage. The operator exposes exactly one thing
to the internet: a TLS reverse proxy in front of the public port. That
listener serves currently staged files by unguessable token and nothing
else; the consumer API is a different app on a different port.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse, Response
from starlette.routing import Route

from content_fleet.media import MediaStore
from content_fleet.staging import Stager
from content_fleet.store.db import Database, MediaRecord

DEFAULT_TTL_SECONDS = 24 * 3600  # an Instagram container lives 24 hours
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def public_name(media: MediaRecord) -> str:
    """A US-ASCII file name: Meta recommends ASCII-only media URLs."""
    name = _UNSAFE.sub("_", media.filename or "") or "media"
    return name[-80:]


class SelfHostedStager(Stager):
    name = "self-hosted"

    def __init__(
        self,
        db: Database,
        media: MediaStore,
        clock: Callable[[], float],
        settings: dict[str, Any],
    ) -> None:
        unknown = set(settings) - {"public_base_url", "ttl_seconds"}
        if unknown:
            raise ValueError(f"unknown staging settings: {sorted(unknown)}")
        base = settings.get("public_base_url")
        if not isinstance(base, str) or not base.startswith("https://"):
            raise ValueError(
                "staging settings.public_base_url is required and must be the https:// "
                "address at which the public listener is reachable from the internet"
            )
        self._base = base.rstrip("/")
        self._ttl = float(settings.get("ttl_seconds", DEFAULT_TTL_SECONDS))
        self._db = db
        self._media = media
        self._clock = clock

    async def stage(self, media: MediaRecord) -> str:
        token = self._db.stage_media(media.id, secrets.token_urlsafe(32), self._clock() + self._ttl)
        return f"{self._base}/m/{token}/{quote(public_name(media))}"

    async def unstage(self, media_id: str) -> None:
        self._db.unstage_media(media_id)

    def app(self) -> Starlette:
        """The public application. Deliberately tiny: one route, GET/HEAD only."""

        async def serve(request: Request) -> Response:
            media_id = self._db.staged_media_id(request.path_params["token"], self._clock())
            record = self._media.get(media_id) if media_id else None
            if record is None:
                # Same answer for unknown, withdrawn and expired: nothing to learn.
                return PlainTextResponse("not found", status_code=404)
            return FileResponse(
                self._media.path(record.id),
                media_type=record.content_type,
                headers={
                    "cache-control": "private, max-age=0",
                    "x-content-type-options": "nosniff",
                },
            )

        return Starlette(routes=[Route("/m/{token}/{name}", serve, methods=["GET", "HEAD"])])
