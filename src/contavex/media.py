"""Uploaded media: bytes on the data volume, metadata in the database."""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from contavex.core.errors import ApiProblem
from contavex.store.db import Database, MediaRecord

_CONTENT_TYPE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")


class MediaStore:
    def __init__(
        self,
        db: Database,
        directory: Path,
        *,
        max_bytes: int,
        retention_seconds: float,
        clock: Callable[[], float],
    ) -> None:
        self._db = db
        self._dir = directory
        self._max_bytes = max_bytes
        self._retention = retention_seconds
        self._clock = clock
        directory.mkdir(parents=True, exist_ok=True)

    def path(self, media_id: str) -> Path:
        # Ids are generated here, never taken from a consumer, so this cannot
        # be steered outside the media directory.
        return self._dir / media_id

    def get(self, media_id: str, consumer: str | None = None) -> MediaRecord | None:
        record = self._db.get_media(media_id)
        if record is None or record.expires_at <= self._clock():
            return None
        if consumer is not None and record.consumer != consumer:
            return None
        return record

    async def save(
        self,
        chunks: AsyncIterator[bytes],
        *,
        consumer: str,
        content_type: str,
        filename: str | None,
    ) -> MediaRecord:
        content_type = content_type.split(";", 1)[0].strip().lower()
        if not _CONTENT_TYPE.fullmatch(content_type):
            raise ApiProblem(
                415,
                "invalid-content-type",
                "A valid Content-Type header is required",
                "Send the file's MIME type, for example image/jpeg.",
            )
        media_id = f"med_{uuid.uuid7().hex}"
        final = self.path(media_id)
        partial = final.with_suffix(".part")
        digest = hashlib.sha256()
        size = 0
        try:
            with partial.open("wb") as out:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > self._max_bytes:
                        raise ApiProblem(
                            413,
                            "media-too-large",
                            "The upload exceeds the size limit",
                            f"The limit is {self._max_bytes} bytes.",
                        )
                    digest.update(chunk)
                    out.write(chunk)
            if size == 0:
                raise ApiProblem(400, "empty-media", "The request body is empty")
            partial.replace(final)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        now = self._clock()
        record = MediaRecord(
            id=media_id,
            consumer=consumer,
            content_type=content_type,
            size=size,
            sha256=digest.hexdigest(),
            filename=filename,
            created_at=now,
            expires_at=now + self._retention,
        )
        self._db.insert_media(record)
        return record

    def keep_until(self, media_ids: list[str], until: float) -> None:
        self._db.extend_media(media_ids, until)

    def purge_expired(self) -> int:
        ids = self._db.pop_expired_media(self._clock())
        for media_id in ids:
            self.path(media_id).unlink(missing_ok=True)
        return len(ids)
