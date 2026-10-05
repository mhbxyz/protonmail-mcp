from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass

DEFAULT_MAX_ENTRIES = 20


@dataclass(frozen=True, slots=True)
class MoveEntry:
    id: int
    source: str
    destination: str
    message_ids: tuple[str, ...]
    created_at: float
    undone: bool = False


class MoveJournal:
    def __init__(
        self,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._entries: deque[MoveEntry] = deque(maxlen=max_entries)
        self._counter = 0
        self._lock = threading.Lock()
        self._clock = clock

    def record(self, source: str, destination: str, message_ids: Sequence[str]) -> MoveEntry:
        with self._lock:
            self._counter += 1
            entry = MoveEntry(
                id=self._counter,
                source=source,
                destination=destination,
                message_ids=tuple(message_ids),
                created_at=self._clock(),
            )
            self._entries.append(entry)
            return entry

    def last(self) -> MoveEntry | None:
        with self._lock:
            for entry in reversed(self._entries):
                if not entry.undone:
                    return entry
            return None

    def get(self, entry_id: int) -> MoveEntry | None:
        with self._lock:
            for entry in self._entries:
                if entry.id == entry_id:
                    return entry
            return None

    def mark_undone(self, entry_id: int) -> None:
        with self._lock:
            for index, entry in enumerate(self._entries):
                if entry.id == entry_id:
                    self._entries[index] = MoveEntry(
                        id=entry.id,
                        source=entry.source,
                        destination=entry.destination,
                        message_ids=entry.message_ids,
                        created_at=entry.created_at,
                        undone=True,
                    )
                    return

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
