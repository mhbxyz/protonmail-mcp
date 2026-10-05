from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

from .bridge import BridgeClient
from .models import IndexHit

DEFAULT_MAX_BODY_CHARS = 10000


class MessageIndex:
    def __init__(
        self,
        path: Path | str,
        max_body_chars: int = DEFAULT_MAX_BODY_CHARS,
    ) -> None:
        self._path = Path(path).expanduser()
        self._max_body_chars = max_body_chars
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self._path, check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS messages USING fts5(
                message_id UNINDEXED,
                folder UNINDEXED,
                subject,
                sender,
                recipients,
                body,
                received UNINDEXED,
                unread UNINDEXED,
                tokenize = 'unicode61'
            )
            """
        )
        self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def count(self) -> int:
        with self._lock:
            row = self._connection.execute("SELECT COUNT(*) FROM messages").fetchone()
        return int(row[0]) if row else 0

    def clear(self) -> None:
        with self._lock:
            self._connection.execute("DELETE FROM messages")
            self._connection.commit()

    def sync_folder(self, client: BridgeClient, folder: str, limit: int) -> int:
        page = client.list_emails(folder=folder, limit=limit)
        indexed = 0
        with self._lock:
            for summary in page.messages:
                content = client.get_message(
                    summary.message_id, folder=folder, max_chars=0
                )
                body = content.body_text[: self._max_body_chars]
                self._connection.execute(
                    "DELETE FROM messages WHERE message_id = ?", (summary.message_id,)
                )
                self._connection.execute(
                    "INSERT INTO messages "
                    "(message_id, folder, subject, sender, recipients, body, received, unread) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        summary.message_id,
                        folder,
                        summary.subject,
                        summary.sender,
                        summary.recipients,
                        body,
                        summary.received,
                        "1" if summary.unread else "0",
                    ),
                )
                indexed += 1
            self._connection.commit()
        return indexed

    def search(self, query: str, folder: str | None, limit: int) -> list[IndexHit]:
        match = '"' + query.replace('"', '""') + '"'
        sql = (
            "SELECT message_id, folder, subject, sender, received, unread, "
            "snippet(messages, 5, '[', ']', '…', 24) "
            "FROM messages WHERE messages MATCH ?"
        )
        params: list[Any] = [match]
        if folder:
            sql += " AND folder = ?"
            params.append(folder)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._connection.execute(sql, params).fetchall()
        return [
            IndexHit(
                message_id=row[0],
                folder=row[1],
                subject=row[2],
                sender=row[3],
                received=row[4],
                unread=row[5] == "1",
                snippet=row[6] or "",
            )
            for row in rows
        ]
