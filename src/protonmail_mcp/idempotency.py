from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

DEFAULT_WINDOW_SECONDS = 300
DEFAULT_MAX_ENTRIES = 256


@dataclass(frozen=True, slots=True)
class DraftReference:
    uid: int
    message_id: str
    folder: str
    subject: str


@dataclass(slots=True)
class _Entry:
    reference: DraftReference
    expires_at: float


class IdempotencyStore:
    def __init__(
        self,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if window_seconds < 0:
            raise ValueError("window_seconds must be >= 0")
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._window = float(window_seconds)
        self._max_entries = int(max_entries)
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}

    @property
    def enabled(self) -> bool:
        return self._window > 0

    def lookup(self, key: str) -> DraftReference | None:
        if not self.enabled:
            return None
        with self._lock:
            self._purge()
            entry = self._entries.get(key)
            return entry.reference if entry else None

    def remember(self, key: str, reference: DraftReference) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._purge()
            if len(self._entries) >= self._max_entries:
                oldest = min(self._entries, key=lambda item: self._entries[item].expires_at)
                del self._entries[oldest]
            self._entries[key] = _Entry(
                reference=reference, expires_at=self._clock() + self._window
            )

    def forget(self, key: str) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def _purge(self) -> None:
        now = self._clock()
        expired = [key for key, entry in self._entries.items() if now >= entry.expires_at]
        for key in expired:
            del self._entries[key]
