"""Credentials an account uses, including the ones that change at runtime.

Every credential starts as an injected secret file (the *seed*). Some
providers replace theirs while running: Instagram and LinkedIn tokens are
refreshed. Those replacements cannot live in a read-only secret mount, so
they are kept encrypted in the service database, and from then on the stored
value is used instead of the seed.

When the operator replaces a seed file (to recover a lapsed token), its hash
no longer matches the one the stored value descends from; the new seed then
wins. That is how a stale seed never overrides a refreshed token, yet a new
seed always does. See docs/SECURITY.md.
"""

from __future__ import annotations

import base64
import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from contavex.config import AccountConfig
from contavex.secrets import Secret, SecretError, SecretResolver, redactor
from contavex.store.db import Database

NONCE_BYTES = 12


@dataclass(frozen=True)
class CredentialState:
    """What is known about one credential, without its value."""

    obtained_at: float | None  # None: still the injected seed, age unknown
    expires_at: float | None


@dataclass(frozen=True)
class RefreshedCredential:
    role: str
    value: Secret
    expires_at: float | None


def _digest(seed: Secret) -> str:
    return hashlib.sha256(seed.reveal().encode()).hexdigest()


class CredentialStore:
    def __init__(
        self,
        db: Database,
        secrets: SecretResolver,
        clock: Callable[[], float],
        master_key: Secret | None,
    ) -> None:
        self._db = db
        self._secrets = secrets
        self._clock = clock
        self._cipher: AESGCM | None = None
        if master_key is not None:
            try:
                key = base64.b64decode(master_key.reveal(), validate=True)
            except ValueError:
                raise SecretError("the master key is not valid base64") from None
            if len(key) != 32:
                raise SecretError("the master key must be 32 random bytes, base64-encoded")
            self._cipher = AESGCM(key)

    @property
    def can_store(self) -> bool:
        return self._cipher is not None

    @staticmethod
    def _aad(account: str, role: str) -> bytes:
        # Binds a ciphertext to its row: it cannot be moved to another account.
        return f"{account}\x00{role}".encode()

    def resolve(self, account: AccountConfig) -> dict[str, Secret]:
        """The credential values to use for this account right now."""
        return {role: self._current(account, role)[0] for role in account.credentials}

    def states(self, account: AccountConfig) -> dict[str, CredentialState]:
        return {role: self._current(account, role)[1] for role in account.credentials}

    def _current(self, account: AccountConfig, role: str) -> tuple[Secret, CredentialState]:
        seed = self._secrets.get(account.credentials[role])
        row = self._db.get_credential(account.id, role) if self._cipher else None
        if row is None or row["seed_sha256"] != _digest(seed):
            return seed, CredentialState(obtained_at=None, expires_at=None)
        assert self._cipher is not None
        blob: bytes = row["ciphertext"]
        try:
            plain = self._cipher.decrypt(
                blob[:NONCE_BYTES], blob[NONCE_BYTES:], self._aad(account.id, role)
            )
        except InvalidTag:
            raise SecretError(
                f"stored credential {account.id}/{role} cannot be decrypted; "
                "the master key does not match the one it was stored with"
            ) from None
        value = plain.decode()
        redactor.register(value)
        return Secret(value), CredentialState(row["obtained_at"], row["expires_at"])

    def first_seen(self, account: AccountConfig, role: str) -> None:
        """Start the clock on a seed, so its age is known when refresh rules ask."""
        if self._cipher is None:
            return
        seed = self._secrets.get(account.credentials[role])
        row = self._db.get_credential(account.id, role)
        if row is None or row["seed_sha256"] != _digest(seed):
            self._write(account, role, seed, seed, expires_at=None)

    def save(self, account: AccountConfig, refreshed: RefreshedCredential) -> None:
        if self._cipher is None:
            raise SecretError(
                "a credential was refreshed but no master key is configured to store it"
            )
        seed = self._secrets.get(account.credentials[refreshed.role])
        self._write(account, refreshed.role, seed, refreshed.value, refreshed.expires_at)

    def _write(
        self,
        account: AccountConfig,
        role: str,
        seed: Secret,
        value: Secret,
        expires_at: float | None,
    ) -> None:
        assert self._cipher is not None
        nonce = os.urandom(NONCE_BYTES)
        redactor.register(value.reveal())
        now = self._clock()
        self._db.put_credential(
            account=account.id,
            role=role,
            seed_sha256=_digest(seed),
            ciphertext=nonce
            + self._cipher.encrypt(nonce, value.reveal().encode(), self._aad(account.id, role)),
            obtained_at=now,
            expires_at=expires_at,
            now=now,
        )
