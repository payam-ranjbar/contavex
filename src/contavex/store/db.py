"""SQLite persistence for publications and media metadata.

One connection, one writer, serialized by a lock. Queries are sub-millisecond
at this service's volume, so they run inline rather than through a driver or
thread pool. The service must run as a single replica (ARCHITECTURE D-3).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from contavex.core.models import (
    ErrorInfo,
    PublicationRequest,
    PublicationResult,
    PublicationStatus,
)


@dataclass(frozen=True)
class PublicationRecord:
    id: str
    consumer: str
    idempotency_key: str
    fingerprint: str
    account: str
    platform: str
    implementation: str
    status: PublicationStatus
    request: PublicationRequest
    state: dict[str, Any]
    result: PublicationResult | None
    error: ErrorInfo | None
    attempts: int
    run_after: float | None
    deadline: float
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class MediaRecord:
    id: str
    consumer: str
    content_type: str
    size: int
    sha256: str
    filename: str | None
    created_at: float
    expires_at: float


class DuplicateIdempotencyKey(Exception):
    pass


def _publication(row: sqlite3.Row) -> PublicationRecord:
    return PublicationRecord(
        id=row["id"],
        consumer=row["consumer"],
        idempotency_key=row["idempotency_key"],
        fingerprint=row["fingerprint"],
        account=row["account"],
        platform=row["platform"],
        implementation=row["implementation"],
        status=PublicationStatus(row["status"]),
        request=PublicationRequest.model_validate_json(row["request_json"]),
        state=json.loads(row["state_json"]),
        result=(
            PublicationResult.model_validate_json(row["result_json"])
            if row["result_json"]
            else None
        ),
        error=ErrorInfo.model_validate_json(row["error_json"]) if row["error_json"] else None,
        attempts=row["attempts"],
        run_after=row["run_after"],
        deadline=row["deadline"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _media(row: sqlite3.Row) -> MediaRecord:
    return MediaRecord(**{key: row[key] for key in row.keys()})


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, autocommit=True, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        for pragma in (
            "journal_mode=WAL",
            "synchronous=FULL",
            "busy_timeout=5000",
            "foreign_keys=ON",
        ):
            self._conn.execute(f"PRAGMA {pragma}")
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def ping(self) -> bool:
        with self._lock:
            return bool(self._conn.execute("SELECT 1").fetchone()[0] == 1)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _migrate(self) -> None:
        files = sorted(
            (
                f
                for f in resources.files(__package__).joinpath("migrations").iterdir()
                if f.name.endswith(".sql")
            ),
            key=lambda f: f.name,
        )
        current = self._conn.execute("PRAGMA user_version").fetchone()[0]
        for file in files:
            version = int(file.name.split("_", 1)[0])
            if version <= current:
                continue
            with self._tx() as conn:
                # Comments are dropped first so a ";" inside one cannot split a statement.
                sql = "\n".join(
                    line
                    for line in file.read_text(encoding="utf-8").splitlines()
                    if not line.lstrip().startswith("--")
                )
                for statement in sql.split(";"):
                    if statement.strip():
                        conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {version}")

    # ---------------------------------------------------------------- publications

    def insert_publication(
        self,
        *,
        id: str,
        consumer: str,
        idempotency_key: str,
        fingerprint: str,
        account: str,
        platform: str,
        implementation: str,
        request: PublicationRequest,
        now: float,
        deadline: float,
    ) -> PublicationRecord:
        """Accept a request: the key, the publication and its job in one commit."""
        try:
            with self._tx() as conn:
                conn.execute(
                    """INSERT INTO publications
                       (id, consumer, idempotency_key, fingerprint, account, platform,
                        implementation, status, request_json, run_after, deadline,
                        created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        id,
                        consumer,
                        idempotency_key,
                        fingerprint,
                        account,
                        platform,
                        implementation,
                        PublicationStatus.ACCEPTED,
                        request.model_dump_json(),
                        now,
                        deadline,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            if "idempotency_key" in str(exc):
                raise DuplicateIdempotencyKey from None
            raise
        record = self.get_publication(id)
        assert record is not None
        return record

    def get_publication(self, id: str) -> PublicationRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM publications WHERE id = ?", (id,)).fetchone()
        return _publication(row) if row else None

    def get_by_idempotency_key(self, consumer: str, key: str) -> PublicationRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM publications WHERE consumer = ? AND idempotency_key = ?",
                (consumer, key),
            ).fetchone()
        return _publication(row) if row else None

    def claim_due(self, now: float, lease_seconds: float) -> tuple[PublicationRecord, bool] | None:
        """Lease the next due publication.

        Returns the record and whether its previous step was interrupted (the
        process died while a provider call was in flight).
        """
        with self._tx() as conn:
            row = conn.execute(
                """SELECT * FROM publications
                   WHERE run_after IS NOT NULL AND run_after <= ?
                     AND (lease_until IS NULL OR lease_until <= ?)
                   ORDER BY run_after LIMIT 1""",
                (now, now),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE publications SET lease_until = ?, in_flight = 1 WHERE id = ?",
                (now + lease_seconds, row["id"]),
            )
        return _publication(row), bool(row["in_flight"])

    def next_due_at(self) -> float | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MIN(MAX(run_after, COALESCE(lease_until, 0))) FROM publications "
                "WHERE run_after IS NOT NULL"
            ).fetchone()
        due: float | None = row[0]
        return due

    def save_progress(
        self,
        id: str,
        *,
        now: float,
        state: dict[str, Any],
        run_after: float,
        attempts: int,
        error: ErrorInfo | None,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """UPDATE publications
                   SET status = ?, state_json = ?, run_after = ?, attempts = ?, error_json = ?,
                       lease_until = NULL, in_flight = 0, updated_at = ?
                   WHERE id = ?""",
                (
                    PublicationStatus.PROCESSING,
                    json.dumps(state),
                    run_after,
                    attempts,
                    error.model_dump_json() if error else None,
                    now,
                    id,
                ),
            )

    def checkpoint(self, id: str, state: dict[str, Any]) -> None:
        """Persist provider state mid-step, before a non-repeatable call."""
        with self._tx() as conn:
            conn.execute(
                "UPDATE publications SET state_json = ? WHERE id = ?", (json.dumps(state), id)
            )

    def finish(
        self,
        id: str,
        *,
        now: float,
        status: PublicationStatus,
        state: dict[str, Any] | None = None,
        result: PublicationResult | None = None,
        error: ErrorInfo | None = None,
    ) -> None:
        assert status.terminal
        with self._tx() as conn:
            conn.execute(
                """UPDATE publications
                   SET status = ?, result_json = ?, error_json = ?,
                       state_json = COALESCE(?, state_json),
                       run_after = NULL, lease_until = NULL, in_flight = 0, updated_at = ?
                   WHERE id = ?""",
                (
                    status,
                    result.model_dump_json() if result else None,
                    error.model_dump_json() if error else None,
                    json.dumps(state) if state is not None else None,
                    now,
                    id,
                ),
            )

    def count_by_status(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) FROM publications GROUP BY status"
            ).fetchall()
        return {row[0]: row[1] for row in rows}

    # ---------------------------------------------------------------- media

    def insert_media(self, record: MediaRecord) -> None:
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO media (id, consumer, content_type, size, sha256, filename,
                                      created_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.id,
                    record.consumer,
                    record.content_type,
                    record.size,
                    record.sha256,
                    record.filename,
                    record.created_at,
                    record.expires_at,
                ),
            )

    def get_media(self, id: str) -> MediaRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM media WHERE id = ?", (id,)).fetchone()
        return _media(row) if row else None

    def extend_media(self, ids: list[str], expires_at: float) -> None:
        """Keep media at least until the publication using it can no longer need it."""
        with self._tx() as conn:
            conn.executemany(
                "UPDATE media SET expires_at = MAX(expires_at, ?) WHERE id = ?",
                [(expires_at, id) for id in ids],
            )

    def pop_expired_media(self, now: float) -> list[str]:
        with self._tx() as conn:
            ids = [
                row[0] for row in conn.execute("SELECT id FROM media WHERE expires_at <= ?", (now,))
            ]
            conn.executemany("DELETE FROM media WHERE id = ?", [(id,) for id in ids])
        return ids

    # ---------------------------------------------------------------- credentials

    def get_credential(self, account: str, role: str) -> sqlite3.Row | None:
        with self._lock:
            row: sqlite3.Row | None = self._conn.execute(
                "SELECT * FROM credentials WHERE account = ? AND role = ?", (account, role)
            ).fetchone()
        return row

    def put_credential(
        self,
        *,
        account: str,
        role: str,
        seed_sha256: str,
        ciphertext: bytes,
        obtained_at: float,
        expires_at: float | None,
        now: float,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO credentials
                   (account, role, seed_sha256, ciphertext, obtained_at, expires_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (account, role) DO UPDATE SET
                       seed_sha256 = excluded.seed_sha256, ciphertext = excluded.ciphertext,
                       obtained_at = excluded.obtained_at, expires_at = excluded.expires_at,
                       updated_at = excluded.updated_at""",
                (account, role, seed_sha256, ciphertext, obtained_at, expires_at, now),
            )

    # ---------------------------------------------------------------- staged media

    def stage_media(self, media_id: str, token: str, expires_at: float) -> str:
        """Record a public token for a media item; returns the token in force."""
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO staged_media (token, media_id, expires_at) VALUES (?, ?, ?)
                   ON CONFLICT (media_id) DO UPDATE SET expires_at = excluded.expires_at""",
                (token, media_id, expires_at),
            )
            row = conn.execute(
                "SELECT token FROM staged_media WHERE media_id = ?", (media_id,)
            ).fetchone()
        return str(row[0])

    def staged_media_id(self, token: str, now: float) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT media_id FROM staged_media WHERE token = ? AND expires_at > ?",
                (token, now),
            ).fetchone()
        return str(row[0]) if row else None

    def unstage_media(self, media_id: str) -> None:
        with self._tx() as conn:
            conn.execute("DELETE FROM staged_media WHERE media_id = ?", (media_id,))

    def purge_staged_media(self, now: float) -> None:
        with self._tx() as conn:
            conn.execute("DELETE FROM staged_media WHERE expires_at <= ?", (now,))
