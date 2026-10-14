"""Making uploaded media publicly reachable, for providers that fetch by URL.

Staging is about the operator's infrastructure (a bucket, a CDN, a public
listener), not about any one vendor, so it is a seam of its own rather than
part of a provider. See ARCHITECTURE D-5. Implementations arrive with the
first provider that needs them (Instagram).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from contavex.store.db import MediaRecord


class Stager(ABC):
    name: str

    @abstractmethod
    async def stage(self, media: MediaRecord) -> str:
        """Return a public HTTPS URL for the file. Safe to call repeatedly."""

    @abstractmethod
    async def unstage(self, media_id: str) -> None:
        """Withdraw the file. Safe to call for media that was never staged."""
