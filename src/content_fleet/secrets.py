"""Secret access and redaction.

Every credential enters the service through a `SecretResolver`. The only
implementation today reads files (Compose/Swarm/Kubernetes all deliver
secrets as files under /run/secrets). A secret manager is added by writing
another resolver; nothing else changes. See docs/SECURITY.md.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Protocol

REDACTED = "***"

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

# Credential-shaped strings scrubbed even if the value was never registered,
# e.g. a token echoed back inside a provider error message.
_PATTERNS = [
    # Telegram bot token; it follows "bot" in request URLs, so no word boundary.
    re.compile(r"(?<!\d)\d{6,12}:[A-Za-z0-9_-]{30,}"),
    re.compile(r"(?i)\b(access_token|client_secret|refresh_token|appsecret_proof)=[^&\s\"']+"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
]


class SecretError(Exception):
    """A secret could not be obtained. The message never contains its value."""


class Secret:
    """A credential value that does not print itself.

    `str()`, `repr()` and f-strings yield a placeholder; the value is only
    available through `reveal()`, which makes every use easy to find.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"Secret({REDACTED})"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self) -> int:
        return hash(self._value)

    def __reduce__(self) -> Any:
        raise TypeError("Secret values cannot be pickled")


class Redactor:
    """Scrubs known secret values and credential-shaped text from strings."""

    def __init__(self) -> None:
        self._values: set[str] = set()

    def register(self, value: str) -> None:
        # Very short values would shred ordinary log text; real credentials are long.
        if len(value) >= 8:
            self._values.add(value)

    def scrub(self, text: str) -> str:
        for value in sorted(self._values, key=len, reverse=True):
            if value in text:
                text = text.replace(value, REDACTED)
        for pattern in _PATTERNS:
            text = pattern.sub(REDACTED, text)
        return text

    def scrub_obj(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.scrub(obj)
        if isinstance(obj, Secret):
            return REDACTED
        if isinstance(obj, dict):
            return {k: self.scrub_obj(v) for k, v in obj.items()}
        if isinstance(obj, list | tuple):
            return [self.scrub_obj(v) for v in obj]
        return obj


# One process-wide redactor: logging has to scrub values regardless of which
# component resolved them.
redactor = Redactor()


class SecretResolver(Protocol):
    def get(self, name: str) -> Secret: ...


class FileSecretResolver:
    """Reads `<directory>/<name>`; one secret per file."""

    def __init__(self, directory: Path) -> None:
        self._dir = directory

    def get(self, name: str) -> Secret:
        if not _NAME.fullmatch(name):
            raise SecretError(f"invalid secret name: {name!r}")
        path = self._dir / name
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise SecretError(f"secret {name!r} not found in {self._dir}") from None
        except (OSError, UnicodeDecodeError) as exc:
            raise SecretError(f"secret {name!r} is unreadable: {type(exc).__name__}") from None
        # Editors and `echo` append a newline, and the bind mount preserves it.
        if raw.endswith("\r\n"):
            raw = raw[:-2]
        elif raw.endswith("\n"):
            raw = raw[:-1]
        if not raw:
            raise SecretError(f"secret {name!r} is empty")
        redactor.register(raw)
        return Secret(raw)
