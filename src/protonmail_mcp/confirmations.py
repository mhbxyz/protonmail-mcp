from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

DEFAULT_MAX_PENDING = 16


class ConfirmationError(RuntimeError):
    """A confirmation token is missing, expired, reused, or does not match the payload."""


@dataclass(frozen=True, slots=True)
class PreparedConfirmation:
    token: str
    action: str
    digest: str
    expires_in: float
    preview: dict[str, Any]


@dataclass(slots=True)
class _Pending:
    action: str
    digest: str
    expires_at: float


def payload_digest(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ConfirmationManager:
    def __init__(
        self,
        ttl_seconds: float = 300.0,
        max_pending: int = DEFAULT_MAX_PENDING,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_pending <= 0:
            raise ValueError("max_pending must be positive")
        self._ttl = float(ttl_seconds)
        self._max_pending = int(max_pending)
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}

    def prepare(
        self,
        action: str,
        payload: dict[str, Any],
        preview: dict[str, Any] | None = None,
    ) -> PreparedConfirmation:
        with self._lock:
            self._purge()
            if len(self._pending) >= self._max_pending:
                raise ConfirmationError(
                    "too many pending confirmations; commit or discard some first"
                )
            token = secrets.token_urlsafe(24)
            entry = _Pending(
                action=action,
                digest=payload_digest(payload),
                expires_at=self._clock() + self._ttl,
            )
            self._pending[token] = entry
            shown = preview if preview is not None else payload
            return PreparedConfirmation(
                token=token,
                action=action,
                digest=entry.digest,
                expires_in=self._ttl,
                preview=dict(shown),
            )

    def commit(self, token: str, payload: dict[str, Any]) -> None:
        with self._lock:
            entry = self._pending.pop(token, None)
            if entry is None:
                raise ConfirmationError("unknown, expired, or already used confirmation token")
            if self._clock() >= entry.expires_at:
                raise ConfirmationError("confirmation token expired")
            if not hmac.compare_digest(payload_digest(payload), entry.digest):
                raise ConfirmationError(
                    "payload does not match the confirmed request; token revoked"
                )

    def discard(self, token: str) -> bool:
        with self._lock:
            return self._pending.pop(token, None) is not None

    def reset(self) -> None:
        with self._lock:
            self._pending.clear()

    def pending_count(self) -> int:
        with self._lock:
            self._purge()
            return len(self._pending)

    def _purge(self) -> None:
        now = self._clock()
        expired = [token for token, entry in self._pending.items() if now >= entry.expires_at]
        for token in expired:
            del self._pending[token]
