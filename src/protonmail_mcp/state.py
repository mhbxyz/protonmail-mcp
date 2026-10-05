from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .policy import Policy, SendPolicy

PRUNE_AGE = timedelta(days=7)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class SendState:
    def __init__(
        self,
        path: Path | str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._path = Path(path).expanduser()
        self._clock = clock or _utcnow
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self._path, check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS sends (ts TEXT NOT NULL, message_id TEXT NOT NULL)"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS sent_keys (key TEXT PRIMARY KEY, ts TEXT NOT NULL)"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS body_hashes (hash TEXT PRIMARY KEY, ts TEXT NOT NULL)"
        )
        self._connection.commit()

    @classmethod
    def from_policy(cls, policy: Policy) -> SendState:
        return cls(policy.send.state_path)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def count_since(self, since: datetime) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) FROM sends WHERE ts >= ?", (since.isoformat(),)
            ).fetchone()
        return int(row[0]) if row else 0

    def quota_status(self, policy: SendPolicy, now: datetime | None = None) -> dict[str, int]:
        moment = now or self._clock()
        hour_count = self.count_since(moment - timedelta(hours=1))
        day_count = self.count_since(moment - timedelta(days=1))
        return {
            "hour_count": hour_count,
            "day_count": day_count,
            "hour_remaining": max(0, policy.max_per_hour - hour_count),
            "day_remaining": max(0, policy.max_per_day - day_count),
        }

    def can_send(self, policy: SendPolicy, now: datetime | None = None) -> tuple[bool, str]:
        status = self.quota_status(policy, now)
        if status["hour_remaining"] <= 0:
            return False, f"hourly send quota reached ({policy.max_per_hour})"
        if status["day_remaining"] <= 0:
            return False, f"daily send quota reached ({policy.max_per_day})"
        return True, ""

    def record_send(self, message_id: str, at: datetime | None = None) -> None:
        moment = at or self._clock()
        with self._lock:
            self._connection.execute(
                "INSERT INTO sends (ts, message_id) VALUES (?, ?)",
                (moment.isoformat(), message_id),
            )
            self._prune(moment)
            self._connection.commit()

    def is_duplicate(self, key: str, window_seconds: float, now: datetime | None = None) -> bool:
        if window_seconds <= 0:
            return False
        moment = now or self._clock()
        cutoff = (moment - timedelta(seconds=window_seconds)).isoformat()
        with self._lock:
            row = self._connection.execute(
                "SELECT ts FROM sent_keys WHERE key = ?", (key,)
            ).fetchone()
        return bool(row and row[0] >= cutoff)

    def remember_key(self, key: str, at: datetime | None = None) -> None:
        moment = at or self._clock()
        with self._lock:
            self._connection.execute(
                "INSERT OR REPLACE INTO sent_keys (key, ts) VALUES (?, ?)",
                (key, moment.isoformat()),
            )
            self._prune(moment)
            self._connection.commit()

    def body_seen(
        self, body_hash: str, window_seconds: float, now: datetime | None = None
    ) -> bool:
        if window_seconds <= 0:
            return False
        moment = now or self._clock()
        cutoff = (moment - timedelta(seconds=window_seconds)).isoformat()
        with self._lock:
            row = self._connection.execute(
                "SELECT ts FROM body_hashes WHERE hash = ?", (body_hash,)
            ).fetchone()
        return bool(row and row[0] >= cutoff)

    def remember_body(self, body_hash: str, at: datetime | None = None) -> None:
        moment = at or self._clock()
        with self._lock:
            self._connection.execute(
                "INSERT OR REPLACE INTO body_hashes (hash, ts) VALUES (?, ?)",
                (body_hash, moment.isoformat()),
            )
            self._prune(moment)
            self._connection.commit()

    def _prune(self, moment: datetime) -> None:
        cutoff = (moment - PRUNE_AGE).isoformat()
        self._connection.execute("DELETE FROM sends WHERE ts < ?", (cutoff,))
        self._connection.execute("DELETE FROM sent_keys WHERE ts < ?", (cutoff,))
        self._connection.execute("DELETE FROM body_hashes WHERE ts < ?", (cutoff,))
