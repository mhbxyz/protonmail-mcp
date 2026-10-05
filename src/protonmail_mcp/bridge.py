from __future__ import annotations

import re
import ssl
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any, TypeVar

from imapclient import IMAPClient
from imapclient.exceptions import (
    IMAPClientAbortError,
    IMAPClientError,
    LoginError,
    ProtocolError,
)

from .config import BridgeConfig
from .models import EmailContent, EmailSummary, Folder
from .parsing import full_from_message, summary_from_header

T = TypeVar("T")

_RETRYABLE_ERRORS = (IMAPClientAbortError, ProtocolError, OSError)
_APPENDUID = re.compile(rb"APPENDUID\s+\d+\s+(\d+)")


class MailboxError(RuntimeError):
    """An IMAP operation against Proton Bridge failed."""


class MessageNotFoundError(MailboxError):
    """No message matched the requested Message-ID."""


class BridgeClient:
    def __init__(self, config: BridgeConfig) -> None:
        self.config = config
        self._lock = threading.RLock()
        self._client: IMAPClient | None = None

    def close(self) -> None:
        with self._lock:
            self._disconnect()

    def list_folders(self) -> list[Folder]:
        def operation(client: IMAPClient) -> list[Folder]:
            folders: list[Folder] = []
            for flags, delimiter, name in client.list_folders():
                normalised = sorted(_decode(flag) for flag in flags)
                folders.append(
                    Folder(
                        name=str(name),
                        delimiter=_decode(delimiter) if delimiter else "",
                        selectable="\\Noselect" not in normalised,
                        flags=normalised,
                    )
                )
            folders.sort(key=lambda folder: (folder.name.upper() != "INBOX", folder.name.casefold()))
            return folders

        return self._run(operation)

    def list_emails(
        self,
        folder: str = "INBOX",
        limit: int = 20,
        unread_only: bool = False,
        since_days: int | None = None,
        sender: str | None = None,
        subject: str | None = None,
    ) -> list[EmailSummary]:
        criteria = self._list_criteria(unread_only, since_days, sender, subject)
        return self._run(lambda client: self._fetch_summaries(client, folder, criteria, limit))

    def search_emails(self, query: str, folder: str = "INBOX", limit: int = 20) -> list[EmailSummary]:
        criteria: list[Any] = ["TEXT", query]
        return self._run(lambda client: self._fetch_summaries(client, folder, criteria, limit))

    def get_message(self, message_id: str, folder: str = "INBOX", max_chars: int = 20000) -> EmailContent:
        def operation(client: IMAPClient) -> EmailContent:
            client.select_folder(folder, readonly=True)
            uids = client.search(["HEADER", "Message-ID", message_id])
            if not uids:
                raise MessageNotFoundError(
                    f"No message with Message-ID {message_id!r} in folder {folder!r}"
                )
            uid = max(uids)
            response = client.fetch([uid], ["FLAGS", "RFC822.SIZE", "BODY.PEEK[]", "INTERNALDATE"])
            item = response.get(uid)
            if not item:
                raise MessageNotFoundError(f"Message {message_id!r} vanished while fetching")
            raw = _as_bytes(item.get(b"BODY[]"))
            return full_from_message(
                raw,
                uid=uid,
                folder=folder,
                flags=self._flags(item),
                size=int(item.get(b"RFC822.SIZE") or 0),
                max_chars=max_chars,
                received=_internaldate(item),
            )

        return self._run(operation)

    def find_drafts_folder(self) -> str:
        for folder in self.list_folders():
            if "\\Drafts" in folder.flags:
                return folder.name
        return "Drafts"

    def list_drafts(self, limit: int = 20) -> list[EmailSummary]:
        return self.list_emails(folder=self.find_drafts_folder(), limit=limit)

    def get_draft(self, uid: int, max_chars: int = 20000) -> EmailContent:
        folder = self.find_drafts_folder()

        def operation(client: IMAPClient) -> EmailContent:
            client.select_folder(folder, readonly=True)
            response = client.fetch([uid], ["FLAGS", "RFC822.SIZE", "BODY.PEEK[]", "INTERNALDATE"])
            item = response.get(uid)
            if not item:
                raise MessageNotFoundError(f"Draft UID {uid} not found in {folder!r}")
            return full_from_message(
                _as_bytes(item.get(b"BODY[]")),
                uid=uid,
                folder=folder,
                flags=self._flags(item),
                size=int(item.get(b"RFC822.SIZE") or 0),
                max_chars=max_chars,
                received=_internaldate(item),
            )

        return self._run(operation)

    def append_to_drafts(self, raw: bytes) -> int:
        folder = self.find_drafts_folder()

        def operation(client: IMAPClient) -> int:
            response = client.append(
                folder, raw, flags=[r"\Draft"], msg_time=datetime.now(UTC)
            )
            uid = _append_uid(response)
            if uid is None:
                raise MailboxError("Bridge did not return an APPENDUID for the new draft")
            return uid

        return self._run(operation)

    def replace_draft(self, uid: int, raw: bytes) -> int:
        folder = self.find_drafts_folder()

        def operation(client: IMAPClient) -> int:
            try:
                client.select_folder(folder)
                if uid not in client.fetch([uid], ["FLAGS"]):
                    raise MessageNotFoundError(f"Draft UID {uid} not found in {folder!r}")
                response = client.append(
                    folder, raw, flags=[r"\Draft"], msg_time=datetime.now(UTC)
                )
                new_uid = _append_uid(response)
                if new_uid is None:
                    raise MailboxError("Bridge did not return an APPENDUID for the replacement draft")
                client.add_flags([uid], [r"\Deleted"])
                client.expunge([uid])
                return new_uid
            except (IMAPClientError, *_RETRYABLE_ERRORS) as exc:
                raise MailboxError(f"replacing draft {uid} failed: {exc}") from exc

        return self._run(operation)

    def delete_draft(self, uid: int) -> None:
        folder = self.find_drafts_folder()

        def operation(client: IMAPClient) -> None:
            try:
                client.select_folder(folder)
                if uid not in client.fetch([uid], ["FLAGS"]):
                    raise MessageNotFoundError(f"Draft UID {uid} not found in {folder!r}")
                client.add_flags([uid], [r"\Deleted"])
                client.expunge([uid])
            except (IMAPClientError, *_RETRYABLE_ERRORS) as exc:
                raise MailboxError(f"deleting draft {uid} failed: {exc}") from exc

        self._run(operation)

    @staticmethod
    def _list_criteria(
        unread_only: bool,
        since_days: int | None,
        sender: str | None,
        subject: str | None,
    ) -> list[Any]:
        criteria: list[Any] = ["UNSEEN"] if unread_only else ["ALL"]
        if since_days is not None:
            criteria.extend(["SINCE", date.today() - timedelta(days=int(since_days))])
        if sender:
            criteria.extend(["FROM", sender])
        if subject:
            criteria.extend(["SUBJECT", subject])
        return criteria

    def _fetch_summaries(
        self,
        client: IMAPClient,
        folder: str,
        criteria: Sequence[Any],
        limit: int,
    ) -> list[EmailSummary]:
        client.select_folder(folder, readonly=True)
        uids = client.search(criteria)
        selected = uids[:limit]
        if not selected:
            return []
        response = client.fetch(selected, ["FLAGS", "RFC822.SIZE", "RFC822.HEADER", "INTERNALDATE"])
        summaries: list[EmailSummary] = []
        for uid in selected:
            item = response.get(uid)
            if not item:
                continue
            summaries.append(
                summary_from_header(
                    _as_bytes(item.get(b"RFC822.HEADER")),
                    uid=uid,
                    folder=folder,
                    flags=self._flags(item),
                    size=int(item.get(b"RFC822.SIZE") or 0),
                    received=_internaldate(item),
                )
            )
        summaries.sort(key=lambda summary: summary.received, reverse=True)
        return summaries

    @staticmethod
    def _flags(item: dict[Any, Any]) -> list[str]:
        raw = item.get(b"FLAGS") or ()
        return sorted(_decode(flag) for flag in raw)

    def _connect(self) -> IMAPClient:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        if not self.config.verify_tls:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        if self.config.imap_security == "ssl":
            client = IMAPClient(
                self.config.host,
                port=self.config.imap_port,
                ssl=True,
                ssl_context=context,
                timeout=self.config.timeout,
            )
        else:
            client = IMAPClient(
                self.config.host,
                port=self.config.imap_port,
                ssl=False,
                timeout=self.config.timeout,
            )
            client.starttls(ssl_context=context)
        client.login(self.config.username, self.config.password)
        return client

    def _disconnect(self) -> None:
        if self._client is not None:
            try:
                self._client.logout()
            except Exception:
                pass
            self._client = None

    def _connection(self) -> IMAPClient:
        if self._client is None:
            self._client = self._connect()
        return self._client

    def _run(self, operation: Callable[[IMAPClient], T]) -> T:
        with self._lock:
            try:
                return operation(self._connection())
            except _RETRYABLE_ERRORS:
                self._disconnect()
                try:
                    return operation(self._connection())
                except (LoginError, IMAPClientError, *_RETRYABLE_ERRORS) as exc:
                    self._disconnect()
                    raise MailboxError(str(exc)) from exc
            except (LoginError, IMAPClientError) as exc:
                raise MailboxError(str(exc)) from exc


def _decode(value: bytes | str | None) -> str:
    if value is None:
        return ""
    return value.decode() if isinstance(value, bytes) else str(value)


def _internaldate(item: dict[Any, Any]) -> str:
    value = item.get(b"INTERNALDATE")
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    return str(value) if value else ""


def _as_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode()
    return b""


def _append_uid(response: Any) -> int | None:
    if isinstance(response, (bytes, bytearray)):
        raw = bytes(response)
    elif isinstance(response, str):
        raw = response.encode()
    elif isinstance(response, (list, tuple)):
        raw = b"".join(_as_bytes(part) for part in response)
    else:
        raw = b""
    match = _APPENDUID.search(raw)
    return int(match.group(1)) if match else None
