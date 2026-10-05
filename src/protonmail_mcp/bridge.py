from __future__ import annotations

import contextlib
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
from .models import EmailContent, EmailPage, EmailSummary, Folder, FolderStatus
from .parsing import (
    AttachmentPart,
    attachment_parts,
    full_from_message,
    header_message_id,
    summary_from_header,
    thread_parent_ids,
)

T = TypeVar("T")

_RETRYABLE_ERRORS = (IMAPClientAbortError, ProtocolError, OSError)
_APPENDUID = re.compile(rb"APPENDUID\s+\d+\s+(\d+)")
MAX_MESSAGE_BYTES = 25 * 1024 * 1024


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
        before: str | None = None,
    ) -> EmailPage:
        criteria = self._list_criteria(unread_only, since_days, sender, subject)
        return self._fetch_page(folder, criteria, limit, before)

    def get_status(self, folder: str | None = None) -> list[FolderStatus]:
        if folder is not None:
            names = [folder]
        else:
            names = [item.name for item in self.list_folders() if item.selectable][:100]

        def operation(client: IMAPClient) -> list[FolderStatus]:
            results: list[FolderStatus] = []
            for name in names:
                status = client.folder_status(name, ["MESSAGES", "UNSEEN"])
                results.append(
                    FolderStatus(
                        name=name,
                        total=int(status.get(b"MESSAGES") or 0),
                        unread=int(status.get(b"UNSEEN") or 0),
                    )
                )
            return results

        return self._run(operation)

    def search_emails(
        self,
        query: str = "",
        folder: str = "INBOX",
        limit: int = 20,
        sender: str | None = None,
        recipient: str | None = None,
        subject: str | None = None,
        since_days: int | None = None,
        before_days: int | None = None,
        unread_only: bool = False,
    ) -> list[EmailSummary]:
        criteria = self._search_criteria(
            query, unread_only, since_days, before_days, sender, recipient, subject
        )
        return self._run(lambda client: self._fetch_summaries(client, folder, criteria, limit))

    def get_raw(self, message_id: str, folder: str = "INBOX") -> bytes:
        def operation(client: IMAPClient) -> bytes:
            client.select_folder(folder, readonly=True)
            uids = client.search(["HEADER", "Message-ID", message_id])
            if not uids:
                raise MessageNotFoundError(
                    f"No message with Message-ID {message_id!r} in folder {folder!r}"
                )
            uid = max(uids)
            item = self._fetch_body_guarded(
                client, uid, f"Message {message_id!r} vanished while fetching"
            )
            return _as_bytes(item.get(b"BODY[]"))

        return self._run(operation)

    def get_attachment_parts(
        self, message_id: str, folder: str = "INBOX"
    ) -> list[AttachmentPart]:
        return attachment_parts(self.get_raw(message_id, folder))

    def get_thread(
        self,
        message_id: str,
        folder: str = "INBOX",
        limit: int = 50,
    ) -> list[EmailSummary]:
        all_mail = self.folder_by_flag("\\All") or folder

        def fetch_header(client: IMAPClient, mid: str) -> bytes | None:
            client.select_folder(all_mail, readonly=True)
            uids = client.search(["HEADER", "Message-ID", mid])
            if not uids:
                return None
            uid = uids[-1]
            response = client.fetch(
                [uid], ["FLAGS", "RFC822.SIZE", "RFC822.HEADER", "INTERNALDATE"]
            )
            item = response.get(uid)
            if not item:
                return None
            return _as_bytes(item.get(b"RFC822.HEADER"))

        def summary_for(client: IMAPClient, header: bytes) -> EmailSummary | None:
            client.select_folder(all_mail, readonly=True)
            mid = header_message_id(header)
            if not mid:
                return None
            uids = client.search(["HEADER", "Message-ID", mid])
            if not uids:
                return None
            uid = uids[-1]
            response = client.fetch(
                [uid], ["FLAGS", "RFC822.SIZE", "RFC822.HEADER", "INTERNALDATE"]
            )
            item = response.get(uid)
            if not item:
                return None
            return summary_from_header(
                header,
                uid=uid,
                folder=all_mail,
                flags=self._flags(item),
                size=int(item.get(b"RFC822.SIZE") or 0),
                received=_internaldate(item),
            )

        def child_ids(client: IMAPClient, mid: str) -> list[str]:
            found: list[str] = []
            for criteria in (["HEADER", "In-Reply-To", mid], ["HEADER", "References", mid]):
                client.select_folder(all_mail, readonly=True)
                for uid in client.search(criteria):
                    response = client.fetch([uid], ["RFC822.HEADER"])
                    item = response.get(uid)
                    if not item:
                        continue
                    child = header_message_id(_as_bytes(item.get(b"RFC822.HEADER")))
                    if child and child not in found:
                        found.append(child)
            return found

        def operation(client: IMAPClient) -> list[EmailSummary]:
            collected: dict[str, EmailSummary] = {}
            queue = [message_id]
            seen: set[str] = set()
            while queue and len(collected) < limit:
                current = queue.pop(0)
                if current in seen:
                    continue
                seen.add(current)
                header = fetch_header(client, current)
                if header is not None:
                    summary = summary_for(client, header)
                    if summary is not None and summary.message_id:
                        collected[summary.message_id] = summary
                    for parent in thread_parent_ids(header):
                        if parent not in seen:
                            queue.append(parent)
                for child in child_ids(client, current):
                    if child not in seen:
                        queue.append(child)
            summaries = sorted(collected.values(), key=lambda item: item.received)
            return summaries[:limit]

        return self._run(operation)

    def get_message(self, message_id: str, folder: str = "INBOX", max_chars: int = 20000) -> EmailContent:
        def operation(client: IMAPClient) -> EmailContent:
            client.select_folder(folder, readonly=True)
            uids = client.search(["HEADER", "Message-ID", message_id])
            if not uids:
                raise MessageNotFoundError(
                    f"No message with Message-ID {message_id!r} in folder {folder!r}"
                )
            uid = max(uids)
            item = self._fetch_body_guarded(
                client, uid, f"Message {message_id!r} vanished while fetching"
            )
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
        return self.list_emails(folder=self.find_drafts_folder(), limit=limit).messages

    def get_draft(self, uid: int, max_chars: int = 20000) -> EmailContent:
        folder = self.find_drafts_folder()

        def operation(client: IMAPClient) -> EmailContent:
            client.select_folder(folder, readonly=True)
            item = self._fetch_body_guarded(
                client, uid, f"Draft UID {uid} not found in {folder!r}"
            )
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

    def get_draft_raw(self, message_id: str, folder: str | None = None) -> tuple[int, bytes]:
        target = folder or self.find_drafts_folder()

        def operation(client: IMAPClient) -> tuple[int, bytes]:
            client.select_folder(target, readonly=True)
            uids = client.search(["HEADER", "Message-ID", message_id])
            if not uids:
                raise MessageNotFoundError(
                    f"No draft with Message-ID {message_id!r} in {target!r}"
                )
            uid = uids[-1]
            item = self._fetch_body_guarded(
                client, uid, f"Draft {message_id!r} vanished while fetching"
            )
            return uid, _as_bytes(item.get(b"BODY[]"))

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

    def folder_by_flag(self, flag: str) -> str | None:
        for folder in self.list_folders():
            if flag in folder.flags:
                return folder.name
        return None

    def delete_message_permanently(self, message_id: str, folder: str) -> int:
        def operation(client: IMAPClient) -> int:
            try:
                client.select_folder(folder)
                uids = client.search(["HEADER", "Message-ID", message_id])
                if not uids:
                    raise MessageNotFoundError(
                        f"No message with Message-ID {message_id!r} in folder {folder!r}"
                    )
                uid = uids[-1]
                client.add_flags([uid], [r"\Deleted"])
                client.expunge([uid])
                return int(uid)
            except (IMAPClientError, *_RETRYABLE_ERRORS) as exc:
                raise MailboxError(f"deleting message {message_id!r} failed: {exc}") from exc

        return self._run(operation)

    def set_flags(
        self,
        message_ids: Sequence[str],
        folder: str,
        add: Sequence[str] = (),
        remove: Sequence[str] = (),
    ) -> tuple[list[str], list[str]]:
        def operation(client: IMAPClient) -> tuple[list[str], list[str]]:
            client.select_folder(folder)
            updated: list[str] = []
            missing: list[str] = []
            for message_id in message_ids:
                uids = client.search(["HEADER", "Message-ID", message_id])
                if not uids:
                    missing.append(message_id)
                    continue
                uid = uids[-1]
                if add:
                    client.add_flags([uid], list(add))
                if remove:
                    client.remove_flags([uid], list(remove))
                updated.append(message_id)
            return updated, missing

        return self._run(operation)

    def move_messages(
        self,
        message_ids: Sequence[str],
        source: str,
        destination: str,
    ) -> tuple[list[str], list[str]]:
        def operation(client: IMAPClient) -> tuple[list[str], list[str]]:
            client.select_folder(source)
            updated: list[str] = []
            missing: list[str] = []
            for message_id in message_ids:
                uids = client.search(["HEADER", "Message-ID", message_id])
                if not uids:
                    missing.append(message_id)
                    continue
                client.move([uids[-1]], destination)
                updated.append(message_id)
            return updated, missing

        return self._run(operation)

    def add_label(
        self,
        message_ids: Sequence[str],
        folder: str,
        label: str,
    ) -> tuple[list[str], list[str]]:
        def operation(client: IMAPClient) -> tuple[list[str], list[str]]:
            updated: list[str] = []
            missing: list[str] = []
            for message_id in message_ids:
                client.select_folder(label, readonly=True)
                if client.search(["HEADER", "Message-ID", message_id]):
                    updated.append(message_id)
                    continue
                client.select_folder(folder)
                uids = client.search(["HEADER", "Message-ID", message_id])
                if not uids:
                    missing.append(message_id)
                    continue
                client.copy([uids[-1]], label)
                updated.append(message_id)
            return updated, missing

        return self._run(operation)

    def remove_label(
        self,
        message_ids: Sequence[str],
        label: str,
    ) -> tuple[list[str], list[str]]:
        def operation(client: IMAPClient) -> tuple[list[str], list[str]]:
            client.select_folder(label)
            updated: list[str] = []
            missing: list[str] = []
            for message_id in message_ids:
                uids = client.search(["HEADER", "Message-ID", message_id])
                if not uids:
                    missing.append(message_id)
                    continue
                client.add_flags(uids, [r"\Deleted"])
                client.expunge(uids)
                updated.append(message_id)
            return updated, missing

        return self._run(operation)

    @staticmethod
    def _fetch_body_guarded(
        client: IMAPClient, uid: int, missing_message: str
    ) -> dict[Any, Any]:
        meta = client.fetch([uid], ["FLAGS", "RFC822.SIZE", "INTERNALDATE"])
        item = meta.get(uid)
        if not item:
            raise MessageNotFoundError(missing_message)
        size = int(item.get(b"RFC822.SIZE") or 0)
        if size > MAX_MESSAGE_BYTES:
            raise MailboxError(
                f"message is too large to fetch ({size} bytes; limit {MAX_MESSAGE_BYTES})"
            )
        response = client.fetch([uid], ["FLAGS", "RFC822.SIZE", "BODY.PEEK[]", "INTERNALDATE"])
        body_item = response.get(uid)
        return body_item if isinstance(body_item, dict) else item

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

    @staticmethod
    def _search_criteria(
        query: str,
        unread_only: bool,
        since_days: int | None,
        before_days: int | None,
        sender: str | None,
        recipient: str | None,
        subject: str | None,
    ) -> list[Any]:
        criteria: list[Any] = []
        if unread_only:
            criteria.append("UNSEEN")
        if query:
            criteria.extend(["TEXT", query])
        if sender:
            criteria.extend(["FROM", sender])
        if recipient:
            criteria.extend(["TO", recipient])
        if subject:
            criteria.extend(["SUBJECT", subject])
        if since_days is not None:
            criteria.extend(["SINCE", date.today() - timedelta(days=int(since_days))])
        if before_days is not None:
            criteria.extend(["BEFORE", date.today() - timedelta(days=int(before_days))])
        return criteria or ["ALL"]

    def _fetch_summaries(
        self,
        client: IMAPClient,
        folder: str,
        criteria: Sequence[Any],
        limit: int,
    ) -> list[EmailSummary]:
        client.select_folder(folder, readonly=True)
        uids = client.search(criteria)
        return self._summaries_for(client, uids[:limit], folder)

    def _fetch_page(
        self,
        folder: str,
        criteria: Sequence[Any],
        limit: int,
        before: str | None,
    ) -> EmailPage:
        def operation(client: IMAPClient) -> EmailPage:
            client.select_folder(folder, readonly=True)
            uids = client.search(criteria)
            start = 0
            if before is not None:
                try:
                    before_uid = int(before)
                except ValueError as exc:
                    raise MailboxError(f"invalid pagination cursor {before!r}") from exc
                if before_uid not in uids:
                    raise MailboxError(
                        "pagination cursor is no longer valid; restart from the first page"
                    )
                start = uids.index(before_uid) + 1
            selected = uids[start : start + limit]
            has_more = bool(uids[start + limit :])
            summaries = self._summaries_for(client, selected, folder)
            next_cursor = str(selected[-1]) if has_more and selected else None
            return EmailPage(folder=folder, messages=summaries, next_cursor=next_cursor)

        return self._run(operation)

    def _summaries_for(
        self,
        client: IMAPClient,
        selected: Sequence[int],
        folder: str,
    ) -> list[EmailSummary]:
        if not selected:
            return []
        response = client.fetch(
            list(selected), ["FLAGS", "RFC822.SIZE", "RFC822.HEADER", "INTERNALDATE"]
        )
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
            with contextlib.suppress(Exception):
                self._client.logout()
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
