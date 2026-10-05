from __future__ import annotations

import asyncio
import functools
import mimetypes
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager, suppress
from typing import Any
from urllib.parse import unquote

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.subscriptions import ResourceUpdated
from mcp.types import ToolAnnotations

from . import __version__
from .audit import AuditLog
from .bridge import BridgeClient, MailboxError, MessageNotFoundError
from .compose import (
    ComposeError,
    DraftAttachment,
    build_draft,
    build_forward,
    build_reply,
    draft_view,
    forward_subject,
    reply_recipients,
    reply_subject,
    validate_recipients,
)
from .config import ConfigError, resolve_bridge_config
from .confirmations import (
    ConfirmationError,
    ConfirmationManager,
    PreparedConfirmation,
    payload_digest,
)
from .files import (
    SandboxError,
    read_from_sandbox,
    safe_filename,
    sandbox_directory,
    write_in_sandbox,
)
from .idempotency import DraftReference, IdempotencyStore
from .index import MessageIndex
from .journal import MoveEntry, MoveJournal
from .models import (
    Attachment,
    Digest,
    DraftCreated,
    DraftDeleted,
    DraftPreview,
    DraftSent,
    EmailContent,
    EmailPage,
    EmailSummary,
    Folder,
    FolderStatus,
    IndexHit,
    IndexSyncResult,
    MessageDeleted,
    OrganizeResult,
    PreparedAction,
    SavedFile,
    SearchResult,
)
from .parsing import AttachmentPart
from .policy import Policy, load_policy
from .send import (
    SendError,
    SmtpSender,
    Transmission,
    body_digest,
    denied_recipients,
    external_recipients,
    guard_reasons,
    send_key,
    transmission_from_raw,
)
from .state import SendState
from .watcher import InboxWatcher

READ_ONLY = ToolAnnotations(read_only_hint=True)
LOCAL_WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False
)
WRITE_ACTION = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False
)
DESTRUCTIVE_WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False
)

_client_lock = threading.Lock()
_client: BridgeClient | None = None

AUDIT = AuditLog.from_env()

_smtp_lock = threading.Lock()
_smtp_sender: SmtpSender | None = None


def get_smtp_sender() -> SmtpSender:
    global _smtp_sender
    with _smtp_lock:
        if _smtp_sender is None:
            _smtp_sender = SmtpSender(get_client().config)
        return _smtp_sender


def set_smtp_sender(sender: SmtpSender | None) -> None:
    global _smtp_sender
    with _smtp_lock:
        _smtp_sender = sender


def _audited(action: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.monotonic()
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                AUDIT.record(
                    action,
                    outcome="error",
                    duration_ms=int((time.monotonic() - started) * 1000),
                    args=kwargs,
                    error=type(exc).__name__,
                )
                raise
            AUDIT.record(
                action,
                outcome="ok",
                duration_ms=int((time.monotonic() - started) * 1000),
                args=kwargs,
            )
            return result

        return wrapper

    return decorator


def get_client() -> BridgeClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = BridgeClient(resolve_bridge_config(POLICY))
        return _client


def set_client(client: BridgeClient | None) -> None:
    global _client
    with _client_lock:
        _client = client


def _guard(exc: Exception) -> ToolError:
    return ToolError(str(exc))


def _clamp(limit: int) -> int:
    return max(1, min(int(limit), 100))


def _update_payload(
    uid: int,
    to: str,
    cc: str,
    bcc: str,
    subject: str,
    body: str,
    attachments: Sequence[str] | None = None,
) -> dict[str, Any]:
    return {
        "action": "update_draft",
        "uid": uid,
        "to": to,
        "cc": cc,
        "bcc": bcc,
        "subject": subject,
        "body": body,
        "attachments": list(attachments or []),
    }


def _delete_payload(uid: int) -> dict[str, Any]:
    return {"action": "delete_draft", "uid": uid}


def _reply_payload(
    message_id: str,
    folder: str,
    body: str,
    reply_all: bool,
    attachments: Sequence[str] | None = None,
) -> dict[str, Any]:
    return {
        "action": "reply_draft",
        "message_id": message_id,
        "folder": folder,
        "body": body,
        "reply_all": reply_all,
        "attachments": list(attachments or []),
    }


def _forward_payload(
    message_id: str,
    folder: str,
    to: str,
    body: str,
    include_attachments: bool = False,
    attachments: Sequence[str] | None = None,
) -> dict[str, Any]:
    return {
        "action": "forward_draft",
        "message_id": message_id,
        "folder": folder,
        "to": to,
        "body": body,
        "include_attachments": include_attachments,
        "attachments": list(attachments or []),
    }


def _prepared_action(prepared: PreparedConfirmation) -> PreparedAction:
    return PreparedAction(
        token=prepared.token,
        action=prepared.action,
        expires_in=prepared.expires_in,
        preview=prepared.preview,
    )


def _idempotency_key(action: str, payload: dict[str, Any]) -> str:
    return f"{action}:{payload_digest(payload)}"


def _remembered_draft(key: str, client: BridgeClient) -> DraftCreated | None:
    reference = IDEMPOTENCY.lookup(key)
    if reference is None:
        return None
    try:
        client.get_draft(reference.uid, max_chars=1)
    except MessageNotFoundError:
        IDEMPOTENCY.forget(key)
        return None
    return DraftCreated(
        uid=reference.uid,
        message_id=reference.message_id,
        folder=reference.folder,
        subject=reference.subject,
        duplicate=True,
    )


def _remember_draft(key: str, created: DraftCreated) -> None:
    IDEMPOTENCY.remember(
        key,
        DraftReference(
            uid=created.uid,
            message_id=created.message_id,
            folder=created.folder,
            subject=created.subject,
        ),
    )


FLAG_ACTIONS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "read": (("\\Seen",), ()),
    "unread": ((), ("\\Seen",)),
    "flag": (("\\Flagged",), ()),
    "unflag": ((), ("\\Flagged",)),
}


def _dedupe(message_ids: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(message_ids))


def _check_bulk(policy: Policy, message_ids: Sequence[str]) -> None:
    if not message_ids:
        raise MailboxError("message_ids must not be empty")
    limit = policy.organize.max_bulk
    if len(message_ids) > limit:
        raise MailboxError(f"too many messages in one call: {len(message_ids)} > {limit}")


def _folders_by_name(client: BridgeClient) -> dict[str, Folder]:
    return {folder.name: folder for folder in client.list_folders()}


def _resolve_source(client: BridgeClient, policy: Policy, folder: str) -> str:
    target = _folders_by_name(client).get(folder)
    if target is None:
        raise MailboxError(f"folder not found: {folder!r}")
    if not target.selectable:
        raise MailboxError(f"folder is not selectable: {folder!r}")
    if "\\All" in target.flags:
        raise MailboxError("All Mail cannot be modified")
    if policy.organize.protect_drafts and "\\Drafts" in target.flags:
        raise MailboxError("Drafts are protected by policy (organize.protect_drafts)")
    return target.name


def _resolve_destination(client: BridgeClient, policy: Policy, destination: str) -> str:
    folders = _folders_by_name(client)
    alias = destination.strip().lower()
    if alias in {"archive", "trash"}:
        flag = "\\Archive" if alias == "archive" else "\\Trash"
        match = next((folder for folder in folders.values() if flag in folder.flags), None)
        if match is None:
            raise MailboxError(f"no folder with the {flag} attribute was found")
        resolved = match.name
    else:
        match = folders.get(destination)
        if match is None:
            raise MailboxError(f"destination folder not found: {destination!r}")
        resolved = match.name
    target = folders[resolved]
    if not target.selectable:
        raise MailboxError(f"destination is not selectable: {resolved!r}")
    if "\\All" in target.flags:
        raise MailboxError("All Mail cannot be a destination")
    if "\\Flagged" in target.flags:
        raise MailboxError("use flag/unflag to change the Starred state")
    if policy.organize.protect_drafts and "\\Drafts" in target.flags:
        raise MailboxError("Drafts are protected by policy (organize.protect_drafts)")
    if policy.organize.allowed_targets and resolved not in policy.organize.allowed_targets:
        raise MailboxError(f"destination not allowed by policy: {resolved!r}")
    return resolved


def _resolve_label(client: BridgeClient, policy: Policy, label: str) -> str:
    allowed = policy.organize.label_allowlist
    if allowed:
        if label not in allowed:
            raise MailboxError(f"label not allowed by policy: {label!r}")
    elif not label.startswith("Labels/"):
        raise MailboxError("only folders under 'Labels/' can be used as labels")
    match = _folders_by_name(client).get(label)
    if match is None:
        raise MailboxError(f"label not found: {label!r}")
    if not match.selectable:
        raise MailboxError(f"label is not selectable: {label!r}")
    return match.name


def _flags_payload(
    message_ids: Sequence[str], flag_action: str, folder: str
) -> dict[str, Any]:
    return {
        "action": "set_flags",
        "flag_action": flag_action,
        "folder": folder,
        "message_ids": list(message_ids),
    }


def _move_payload(
    message_ids: Sequence[str], source: str, destination: str
) -> dict[str, Any]:
    return {
        "action": "move",
        "source": source,
        "destination": destination,
        "message_ids": list(message_ids),
    }


def _label_payload(
    message_ids: Sequence[str], folder: str, label: str, add: bool
) -> dict[str, Any]:
    return {
        "action": "label",
        "label": label,
        "add": add,
        "folder": folder,
        "message_ids": list(message_ids),
    }


def _undo_payload(entry_id: int) -> dict[str, Any]:
    return {"action": "undo_move", "entry_id": entry_id}


def _send_payload(message_id: str, folder: str) -> dict[str, Any]:
    return {"action": "send_draft", "message_id": message_id, "folder": folder}


def _resolve_draft_folder(client: BridgeClient, requested: str) -> str:
    drafts = client.find_drafts_folder()
    if requested and requested != drafts:
        raise MailboxError(f"only drafts in {drafts!r} can be sent")
    return drafts


def _check_sendable(
    client: BridgeClient,
    policy: Policy,
    state: SendState,
    raw: bytes,
    transmission: Transmission,
) -> None:
    if not transmission.envelope:
        raise SendError("the draft has no recipients")
    denied = denied_recipients(transmission.envelope, client.config.username, policy.send)
    if denied:
        raise SendError(f"recipients not allowed by policy: {', '.join(denied)}")
    if len(transmission.envelope) > policy.send.max_recipients:
        raise SendError(
            f"too many recipients: {len(transmission.envelope)} > {policy.send.max_recipients}"
        )
    if len(transmission.payload) > policy.send.max_message_bytes:
        raise SendError(
            f"message is too large to send ({len(transmission.payload)} bytes; "
            f"limit {policy.send.max_message_bytes})"
        )
    allowed, reason = state.can_send(policy.send)
    if not allowed:
        raise SendError(reason)
    reasons = guard_reasons(raw, transmission, policy.send, state)
    if reasons:
        raise SendError("send refused: " + "; ".join(reasons))


def _delete_message_payload(message_id: str, folder: str) -> dict[str, Any]:
    return {"action": "delete_message", "message_id": message_id, "folder": folder}


def _delete_phrase(message_id: str) -> str:
    return f"permanently delete {message_id}"


def _resolve_trash(client: BridgeClient, requested: str) -> str:
    trash = client.folder_by_flag("\\Trash")
    if trash is None:
        raise MailboxError("no Trash folder was found")
    if requested and requested != trash:
        raise MailboxError(
            f"permanent deletion is only allowed from Trash ({trash!r})"
        )
    return trash


NOTIFICATIONS_URI = "mail://inbox"


def _default_watcher_factory(
    policy: Policy,
) -> Callable[[Callable[[], None]], InboxWatcher]:
    def factory(on_event: Callable[[], None]) -> InboxWatcher:
        return InboxWatcher(
            connect=lambda: get_client().open_connection(),
            on_event=on_event,
            folder=policy.notifications.folder,
            min_interval_seconds=policy.notifications.min_interval_seconds,
        )

    return factory


def _notifications_lifespan(
    holder: dict[str, MCPServer[Any]],
    factory: Callable[[Callable[[], None]], InboxWatcher],
) -> Callable[[Any], Any]:
    @asynccontextmanager
    async def lifespan(_lowlevel: Any) -> Any:
        loop = asyncio.get_running_loop()
        tasks: set[asyncio.Task[None]] = set()

        def on_event() -> None:
            def publish() -> None:
                task = loop.create_task(
                    holder["server"]._subscriptions.publish(
                        ResourceUpdated(uri=NOTIFICATIONS_URI)
                    )
                )
                tasks.add(task)
                task.add_done_callback(tasks.discard)

            with suppress(RuntimeError):
                loop.call_soon_threadsafe(publish)

        watcher = factory(on_event)
        watcher.start()
        try:
            yield None
        finally:
            watcher.stop()
            for task in list(tasks):
                task.cancel()

    return lifespan


def _select_attachment(
    parts: Sequence[AttachmentPart],
    filename: str | None,
    index: int | None,
) -> AttachmentPart:
    if index is not None:
        if index < 0 or index >= len(parts):
            raise SandboxError(f"attachment index {index} out of range (0-{len(parts) - 1})")
        return parts[index]
    if filename is not None:
        matches = [part for part in parts if part.filename == filename]
        if not matches:
            names = ", ".join(repr(part.filename) for part in parts)
            raise SandboxError(f"no attachment named {filename!r}; available: {names}")
        if len(matches) > 1:
            raise SandboxError(f"multiple attachments named {filename!r}; pass an index")
        return matches[0]
    if len(parts) == 1:
        return parts[0]
    names = ", ".join(repr(part.filename) for part in parts)
    raise SandboxError(f"multiple attachments; pass filename or index: {names}")


def _unique_filename(filename: str, counts: dict[str, int]) -> str:
    if filename not in counts:
        counts[filename] = 0
        return filename
    counts[filename] += 1
    base, dot, extension = filename.rpartition(".")
    suffix = counts[filename]
    return f"{base}-{suffix}.{extension}" if dot else f"{filename}-{suffix}"


def _sandbox_attachments(
    policy: Policy, names: Sequence[str], *, max_total: int
) -> list[DraftAttachment]:
    attachments: list[DraftAttachment] = []
    counts: dict[str, int] = {}
    total = 0
    for name in names:
        stored = read_from_sandbox(
            sandbox_directory(policy), name, max_bytes=policy.files.max_bytes
        )
        total += stored.size_bytes
        if total > max_total:
            raise SandboxError(
                f"attachments exceed the message size limit ({total} > {max_total})"
            )
        filename = _unique_filename(stored.filename, counts)
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        attachments.append(
            DraftAttachment(filename=filename, content_type=content_type, data=stored.data)
        )
    return attachments


def _original_attachments(
    client: BridgeClient, policy: Policy, message_id: str, folder: str
) -> list[DraftAttachment]:
    parts = client.get_attachment_parts(message_id, folder=folder)
    attachments: list[DraftAttachment] = []
    counts: dict[str, int] = {}
    total = 0
    for part in parts:
        total += len(part.data)
        if total > policy.send.max_message_bytes:
            raise SandboxError(
                "original attachments exceed the message size limit "
                f"({total} > {policy.send.max_message_bytes})"
            )
        filename = _unique_filename(safe_filename(part.filename, "attachment"), counts)
        attachments.append(
            DraftAttachment(
                filename=filename,
                content_type=part.content_type or "application/octet-stream",
                data=part.data,
            )
        )
    return attachments


def _undo_preview(entry: MoveEntry) -> PreparedAction:
    preview = {
        "action": "undo_move",
        "entry": {
            "id": entry.id,
            "source": entry.source,
            "destination": entry.destination,
            "count": len(entry.message_ids),
            "message_ids": list(entry.message_ids),
        },
    }
    prepared = CONFIRMATIONS.prepare("undo_move", _undo_payload(entry.id), preview=preview)
    return _prepared_action(prepared)


def build_server(
    policy: Policy,
    watcher_factory: Callable[[Callable[[], None]], InboxWatcher] | None = None,
) -> MCPServer:
    capabilities = policy.capabilities
    send_state = SendState.from_policy(policy) if capabilities.send else None
    index_store = (
        MessageIndex(policy.index.path, policy.index.max_body_chars)
        if policy.index.enabled
        else None
    )
    server_holder: dict[str, MCPServer[Any]] = {}
    lifespan = None
    if policy.notifications.enabled:
        lifespan = _notifications_lifespan(
            server_holder, watcher_factory or _default_watcher_factory(policy)
        )
    draft_hint = ""
    if capabilities.draft:
        draft_hint = (
            " Draft tools are enabled: list_drafts, create_draft, preview_draft, and "
            "prepare/commit pairs for replying, forwarding, updating, and deleting "
            "drafts. Mutations only happen when committing a prepared action with the "
            "token returned by its prepare step."
        )
    organize_hint = ""
    if capabilities.organize:
        organize_hint = (
            " Organize tools are enabled: prepare/commit pairs for read/flagged state, "
            "moving messages (destination accepts a folder name or the aliases "
            "'archive' and 'trash'), label add/remove, and undoing the last move. Bulk "
            "operations are capped and always previewed before commit."
        )
    send_hint = ""
    if capabilities.send:
        send_hint = (
            " Send tools are enabled: prepare_send_draft / commit_send_draft submit an "
            "existing draft through Bridge SMTP after allowlist, quota, and loop-guard "
            "checks; the draft is removed only after submission succeeds."
        )
    delete_hint = ""
    if capabilities.delete:
        delete_hint = (
            " Delete tools are enabled: prepare_delete_message / commit_delete_message "
            "permanently erase one message from Trash. There is no bulk deletion and no "
            "empty-trash tool; the commit requires the exact confirmation phrase from "
            "the preview."
        )
    index_hint = ""
    if policy.index.enabled:
        index_hint = (
            " The local full-text index is enabled: sync_index indexes recent messages "
            "into a local SQLite FTS5 store and search_index queries it offline."
        )
    notifications_hint = ""
    if policy.notifications.enabled:
        notifications_hint = (
            f" New-mail notifications are enabled: the server watches "
            f"{policy.notifications.folder!r} with IMAP IDLE and publishes "
            f"{NOTIFICATIONS_URI} resource-updated events that carry no content."
        )
    server = MCPServer(
        name="protonmail",
        title="Proton Mail",
        version=__version__,
        lifespan=lifespan,
        instructions=(
            "Access to a Proton Mail mailbox through a local Proton Bridge instance. "
            f"Active policy: mode={policy.mode}, capabilities: {capabilities.describe()}. "
            "Use list_folders to discover folders and labels, list_emails for recent mail, "
            "search_emails for full-text search, and read_email to read one message by its "
            "Message-ID. Tools outside the active mode are not registered."
            + draft_hint
            + organize_hint
            + send_hint
            + delete_hint
            + index_hint
            + notifications_hint
        ),
    )
    server_holder["server"] = server

    @server.tool(annotations=READ_ONLY)
    @_audited("list_folders")
    def list_folders() -> list[Folder]:
        """List every folder and label of the mailbox. Use this first when you do not know
        where a message lives; the returned names are valid values for other tools."""
        try:
            return get_client().list_folders()
        except (ConfigError, MailboxError) as exc:
            raise _guard(exc) from exc

    @server.tool(annotations=READ_ONLY)
    @_audited("list_emails")
    def list_emails(
        folder: str = "INBOX",
        limit: int = 20,
        unread_only: bool = False,
        since_days: int | None = None,
        sender: str | None = None,
        subject: str | None = None,
        before: str | None = None,
    ) -> EmailPage:
        """List the most recent messages of a folder, newest first.

        Args:
            folder: Folder or label name, as returned by list_folders.
            limit: Maximum number of messages to return (1-100).
            unread_only: Only return unread messages when true.
            since_days: Only return messages from the last N days when set.
            sender: Only return messages whose From header contains this text.
            subject: Only return messages whose Subject header contains this text.
            before: Cursor from a previous page's next_cursor to continue listing.
        """
        try:
            return get_client().list_emails(
                folder=folder,
                limit=_clamp(limit),
                unread_only=unread_only,
                since_days=since_days,
                sender=sender,
                subject=subject,
                before=before,
            )
        except (ConfigError, MailboxError) as exc:
            raise _guard(exc) from exc

    @server.tool(annotations=READ_ONLY)
    @_audited("search_emails")
    def search_emails(
        query: str = "",
        folder: str = "INBOX",
        limit: int = 20,
        sender: str | None = None,
        recipient: str | None = None,
        subject: str | None = None,
        since_days: int | None = None,
        before_days: int | None = None,
        unread_only: bool = False,
        has_attachment: bool = False,
    ) -> SearchResult:
        """Search messages, newest first. All filters combine with AND.

        Args:
            query: Full-text across headers and body; empty matches everything.
            folder: Folder or label name to search in, as returned by list_folders.
            limit: Maximum number of messages to return (1-100).
            sender: Substring match on the From header.
            recipient: Substring match on the To header.
            subject: Substring match on the Subject header.
            since_days: Only messages received in the last N days.
            before_days: Only messages received before N days ago.
            unread_only: Only unread messages when true.
            has_attachment: Only messages carrying an attachment. IMAP cannot search on
                this, so up to 200 newest candidates are checked with BODYSTRUCTURE;
                the result reports how many were scanned and whether the set was
                truncated.
        """
        try:
            return get_client().search_emails(
                query,
                folder=folder,
                limit=_clamp(limit),
                sender=sender,
                recipient=recipient,
                subject=subject,
                since_days=since_days,
                before_days=before_days,
                unread_only=unread_only,
                has_attachment=has_attachment,
            )
        except (ConfigError, MailboxError) as exc:
            raise _guard(exc) from exc

    @server.tool(annotations=READ_ONLY)
    @_audited("read_email")
    def read_email(message_id: str, folder: str = "INBOX", max_chars: int = 20000) -> EmailContent:
        """Read one full message, identified by the Message-ID returned by list_emails or
        search_emails. Returns headers, decoded text body, attachment names, and flags.

        Args:
            message_id: Full Message-ID header value, including angle brackets.
            folder: Folder the message is in.
            max_chars: Maximum body characters to return; longer bodies are truncated.
        """
        try:
            return get_client().get_message(message_id, folder=folder, max_chars=max_chars)
        except MessageNotFoundError as exc:
            raise ToolError(f"{exc}. Try list_folders to find the right folder.") from exc
        except (ConfigError, MailboxError) as exc:
            raise _guard(exc) from exc

    @server.tool(annotations=READ_ONLY)
    @_audited("get_status")
    def get_status(folder: str | None = None) -> list[FolderStatus]:
        """Counts of total and unread messages per folder, without fetching messages.

        Args:
            folder: A single folder name, or omit to get every selectable folder.
        """
        try:
            return get_client().get_status(folder)
        except (ConfigError, MailboxError) as exc:
            raise _guard(exc) from exc

    @server.tool(annotations=READ_ONLY)
    @_audited("daily_digest")
    def daily_digest(folder: str = "INBOX", days: int = 1, limit: int = 20) -> Digest:
        """Compact digest of a folder: unread count plus recent message summaries
        (subjects and senders only, no bodies).

        Args:
            folder: Folder to summarize.
            days: Window in days (default 1).
            limit: Maximum messages in the digest (1-100).
        """
        try:
            client = get_client()
            statuses = client.get_status(folder)
            page = client.list_emails(
                folder=folder, limit=_clamp(limit), since_days=max(0, int(days))
            )
            return Digest(
                folder=folder,
                since_days=max(0, int(days)),
                unread=statuses[0].unread if statuses else 0,
                total_recent=len(page.messages),
                messages=page.messages,
            )
        except (ConfigError, MailboxError) as exc:
            raise _guard(exc) from exc

    @server.tool(annotations=READ_ONLY)
    @_audited("get_thread")
    def get_thread(message_id: str, folder: str = "INBOX", limit: int = 50) -> list[EmailSummary]:
        """Reconstruct a conversation around a message using References/In-Reply-To,
        searching All Mail. Returns messages in chronological order.

        Args:
            message_id: Message-ID of any message in the thread.
            folder: Folder containing that message (used as a fallback if All Mail is absent).
            limit: Maximum messages in the thread (1-100).
        """
        try:
            return get_client().get_thread(message_id, folder=folder, limit=_clamp(limit))
        except (ConfigError, MailboxError) as exc:
            raise _guard(exc) from exc

    @server.tool(annotations=READ_ONLY)
    @_audited("list_attachments")
    def list_attachments(message_id: str, folder: str = "INBOX") -> list[Attachment]:
        """List a message's attachments (names, content types, sizes) without saving
        anything to disk.

        Args:
            message_id: Message-ID of the message.
            folder: Folder containing the message.
        """
        try:
            return get_client().get_message(message_id, folder=folder, max_chars=0).attachments
        except MessageNotFoundError as exc:
            raise ToolError(f"{exc}. Try list_folders to find the right folder.") from exc
        except (ConfigError, MailboxError) as exc:
            raise _guard(exc) from exc

    @server.tool(annotations=LOCAL_WRITE)
    @_audited("save_attachment")
    def save_attachment(
        message_id: str,
        folder: str = "INBOX",
        filename: str | None = None,
        index: int | None = None,
    ) -> SavedFile:
        """Save one attachment into the local sandbox directory. Files are never
        overwritten and never leave the sandbox.

        Args:
            message_id: Message-ID of the message.
            folder: Folder containing the message.
            filename: Exact attachment filename; omit when the message has exactly one.
            index: Attachment position (0-based) when filenames are ambiguous.
        """
        try:
            client = get_client()
            parts = client.get_attachment_parts(message_id, folder=folder)
            if not parts:
                raise SandboxError("this message has no attachments")
            part = _select_attachment(parts, filename, index)
            stored = write_in_sandbox(
                sandbox_directory(policy),
                part.filename,
                part.data,
                max_bytes=policy.files.max_bytes,
            )
            return SavedFile(
                filename=stored.filename,
                path=stored.path,
                size_bytes=stored.size_bytes,
                content_type=part.content_type,
            )
        except MessageNotFoundError as exc:
            raise ToolError(f"{exc}. Try list_folders to find the right folder.") from exc
        except (ConfigError, MailboxError, SandboxError) as exc:
            raise _guard(exc) from exc

    @server.tool(annotations=LOCAL_WRITE)
    @_audited("export_email")
    def export_email(
        message_id: str,
        folder: str = "INBOX",
        filename: str | None = None,
    ) -> SavedFile:
        """Export a full message as .eml into the local sandbox directory.

        Args:
            message_id: Message-ID of the message.
            folder: Folder containing the message.
            filename: Optional target filename; defaults to the Message-ID plus .eml.
        """
        try:
            raw = get_client().get_raw(message_id, folder=folder)
            name = filename if filename else f"{message_id.strip('<>') or 'message'}.eml"
            stored = write_in_sandbox(
                sandbox_directory(policy),
                name,
                raw,
                max_bytes=policy.files.max_bytes,
            )
            return SavedFile(
                filename=stored.filename,
                path=stored.path,
                size_bytes=stored.size_bytes,
                content_type="message/rfc822",
            )
        except MessageNotFoundError as exc:
            raise ToolError(f"{exc}. Try list_folders to find the right folder.") from exc
        except (ConfigError, MailboxError, SandboxError) as exc:
            raise _guard(exc) from exc

    if policy.index.enabled:

        @server.tool(annotations=LOCAL_WRITE)
        @_audited("sync_index")
        def sync_index(folder: str = "", limit: int = 100) -> IndexSyncResult:
            """Index recent messages into the local full-text store (opt-in). The index
            is a plaintext SQLite FTS5 database; attachments are never indexed and body
            text is truncated per policy.

            Args:
                folder: A single folder to index; empty indexes every selectable folder
                    except the ones excluded by policy.
                limit: Maximum messages per folder (1-100).
            """
            try:
                client = get_client()
                store = index_store
                if store is None:
                    raise MailboxError("the local index is not enabled")
                if folder:
                    if folder in policy.index.excluded_folders:
                        raise MailboxError(f"folder is excluded from the index: {folder!r}")
                    folders = [folder]
                else:
                    folders = [
                        item.name
                        for item in client.list_folders()
                        if item.selectable
                        and item.name not in policy.index.excluded_folders
                    ]
                indexed = 0
                for name in folders:
                    indexed += store.sync_folder(client, name, _clamp(limit))
                return IndexSyncResult(indexed=indexed, folders=folders, total=store.count())
            except (ConfigError, MailboxError) as exc:
                raise _guard(exc) from exc

        @server.tool(annotations=READ_ONLY)
        @_audited("search_index")
        def search_index(query: str, folder: str = "", limit: int = 20) -> list[IndexHit]:
            """Search the local index (fast and offline). The query is treated as a
            literal phrase; pair it with sync_index to keep the index fresh.

            Args:
                query: Text to find in indexed subjects, senders, recipients and bodies.
                folder: Restrict to one indexed folder; empty searches all indexed ones.
                limit: Maximum hits (1-100).
            """
            try:
                store = index_store
                if store is None:
                    raise MailboxError("the local index is not enabled")
                return store.search(query, folder or None, _clamp(limit))
            except (ConfigError, MailboxError) as exc:
                raise _guard(exc) from exc

    if capabilities.draft:

        @server.tool(annotations=READ_ONLY)
        @_audited("list_drafts")
        def list_drafts(limit: int = 20) -> list[EmailSummary]:
            """List drafts, newest first. Use the returned uid with the other draft tools.

            Args:
                limit: Maximum number of drafts to return (1-100).
            """
            try:
                return get_client().list_drafts(limit=_clamp(limit))
            except (ConfigError, MailboxError) as exc:
                raise _guard(exc) from exc

        @server.tool(annotations=WRITE_ACTION)
        @_audited("create_draft")
        def create_draft(
            to: str = "",
            cc: str = "",
            bcc: str = "",
            subject: str = "",
            body: str = "",
            in_reply_to: str = "",
            references: str = "",
            attachments: list[str] | None = None,
        ) -> DraftCreated:
            """Create a new draft in the Drafts folder. Additive: existing drafts are never
            modified. To change a draft, use prepare_update_draft/commit_update_draft.

            Args:
                to: Comma-separated recipients; may be empty for a note to self.
                cc: Comma-separated carbon-copy recipients.
                bcc: Comma-separated blind carbon-copy recipients.
                subject: Subject line.
                body: Plain-text body.
                in_reply_to: Optional Message-ID this draft replies to.
                references: Optional space-separated Message-ID chain.
                attachments: Sandbox filenames to attach; nothing outside the sandbox.
            """
            try:
                client = get_client()
                payload = {
                    "action": "create_draft",
                    "to": to,
                    "cc": cc,
                    "bcc": bcc,
                    "subject": subject,
                    "body": body,
                    "in_reply_to": in_reply_to,
                    "references": references,
                    "attachments": list(attachments or []),
                }
                key = _idempotency_key("create_draft", payload)
                remembered = _remembered_draft(key, client)
                if remembered is not None:
                    return remembered
                composed = build_draft(
                    client.config.username,
                    to=validate_recipients(to, header="To"),
                    cc=validate_recipients(cc, header="Cc"),
                    bcc=validate_recipients(bcc, header="Bcc"),
                    subject=subject,
                    body=body,
                    in_reply_to=in_reply_to,
                    references=references,
                    attachments=_sandbox_attachments(
                        policy, attachments or [], max_total=policy.send.max_message_bytes
                    ),
                )
                uid = client.append_to_drafts(composed.raw)
                created = DraftCreated(
                    uid=uid,
                    message_id=composed.message_id,
                    folder=client.find_drafts_folder(),
                    subject=subject,
                )
                _remember_draft(key, created)
                return created
            except (ConfigError, MailboxError, ComposeError, SandboxError) as exc:
                raise _guard(exc) from exc

        @server.tool(annotations=WRITE_ACTION)
        @_audited("prepare_update_draft")
        def prepare_update_draft(
            uid: int,
            to: str = "",
            cc: str = "",
            bcc: str = "",
            subject: str = "",
            body: str = "",
            attachments: list[str] | None = None,
        ) -> PreparedAction:
            """Prepare replacing an existing draft's content. Nothing changes until
            commit_update_draft is called with the same arguments and the returned token.

            Args:
                uid: Draft UID, as returned by list_drafts.
                to: New comma-separated recipients.
                cc: New comma-separated carbon-copy recipients.
                bcc: New comma-separated blind carbon-copy recipients.
                subject: New subject line.
                body: New plain-text body.
                attachments: Sandbox filenames to attach; nothing outside the sandbox.
            """
            try:
                existing = get_client().get_draft(uid, max_chars=200)
                payload = _update_payload(uid, to, cc, bcc, subject, body, attachments)
                preview = {
                    "target": {
                        "uid": uid,
                        "message_id": existing.message_id,
                        "subject": existing.subject,
                    },
                    "replacement": {
                        "to": to,
                        "cc": cc,
                        "bcc": bcc,
                        "subject": subject,
                        "body_preview": body[:200],
                        "attachments": list(attachments or []),
                    },
                }
                return _prepared_action(CONFIRMATIONS.prepare("update_draft", payload, preview=preview))
            except (ConfigError, MailboxError, ConfirmationError) as exc:
                raise _guard(exc) from exc

        @server.tool(annotations=DESTRUCTIVE_WRITE)
        @_audited("commit_update_draft")
        def commit_update_draft(
            token: str,
            uid: int,
            to: str = "",
            cc: str = "",
            bcc: str = "",
            subject: str = "",
            body: str = "",
            attachments: list[str] | None = None,
        ) -> DraftCreated:
            """Commit a prepared draft update: creates the replacement draft, then removes
            the old one. The token and every argument must match prepare_update_draft.

            Args:
                token: Token returned by prepare_update_draft.
                uid: Draft UID.
                to: New comma-separated recipients.
                cc: New comma-separated carbon-copy recipients.
                bcc: New comma-separated blind carbon-copy recipients.
                subject: New subject line.
                body: New plain-text body.
                attachments: Sandbox filenames to attach; nothing outside the sandbox.
            """
            try:
                CONFIRMATIONS.commit(
                    token, _update_payload(uid, to, cc, bcc, subject, body, attachments)
                )
                client = get_client()
                composed = build_draft(
                    client.config.username,
                    to=validate_recipients(to, header="To"),
                    cc=validate_recipients(cc, header="Cc"),
                    bcc=validate_recipients(bcc, header="Bcc"),
                    subject=subject,
                    body=body,
                    attachments=_sandbox_attachments(
                        policy, attachments or [], max_total=policy.send.max_message_bytes
                    ),
                )
                new_uid = client.replace_draft(uid, composed.raw)
                return DraftCreated(
                    uid=new_uid,
                    message_id=composed.message_id,
                    folder=client.find_drafts_folder(),
                    subject=subject,
                )
            except (ConfigError, MailboxError, ComposeError, SandboxError, ConfirmationError) as exc:
                raise _guard(exc) from exc

        @server.tool(annotations=WRITE_ACTION)
        @_audited("prepare_delete_draft")
        def prepare_delete_draft(uid: int) -> PreparedAction:
            """Prepare deleting a draft. Nothing changes until commit_delete_draft is
            called with the same uid and the returned token.

            Args:
                uid: Draft UID, as returned by list_drafts.
            """
            try:
                existing = get_client().get_draft(uid, max_chars=200)
                preview = {
                    "target": {
                        "uid": uid,
                        "message_id": existing.message_id,
                        "subject": existing.subject,
                        "to": existing.recipients,
                    }
                }
                return _prepared_action(
                    CONFIRMATIONS.prepare("delete_draft", _delete_payload(uid), preview=preview)
                )
            except (ConfigError, MailboxError, ConfirmationError) as exc:
                raise _guard(exc) from exc

        @server.tool(annotations=DESTRUCTIVE_WRITE)
        @_audited("commit_delete_draft")
        def commit_delete_draft(token: str, uid: int) -> DraftDeleted:
            """Commit a prepared draft deletion. The token and uid must match
            prepare_delete_draft.

            Args:
                token: Token returned by prepare_delete_draft.
                uid: Draft UID.
            """
            try:
                CONFIRMATIONS.commit(token, _delete_payload(uid))
                client = get_client()
                existing = client.get_draft(uid, max_chars=200)
                client.delete_draft(uid)
                return DraftDeleted(uid=uid, message_id=existing.message_id)
            except (ConfigError, MailboxError, ConfirmationError) as exc:
                raise _guard(exc) from exc

        @server.tool(annotations=WRITE_ACTION)
        @_audited("prepare_reply_draft")
        def prepare_reply_draft(
            message_id: str,
            folder: str = "INBOX",
            body: str = "",
            reply_all: bool = False,
            attachments: list[str] | None = None,
        ) -> PreparedAction:
            """Prepare a reply draft to an existing message, with quoting and threading
            headers. Nothing is saved until commit_reply_draft is called with the same
            arguments and the returned token.

            Args:
                message_id: Message-ID of the message being replied to.
                folder: Folder containing that message.
                body: Optional reply text placed above the quoted original.
                reply_all: Also add the original To/Cc recipients (self excluded).
                attachments: Sandbox filenames to attach; nothing outside the sandbox.
            """
            try:
                client = get_client()
                original = client.get_message(message_id, folder=folder, max_chars=20000)
                to, cc = reply_recipients(original, client.config.username, reply_all)
                _sandbox_attachments(
                    policy, attachments or [], max_total=policy.send.max_message_bytes
                )
                preview = {
                    "target": {
                        "message_id": original.message_id,
                        "subject": original.subject,
                        "sender": original.sender,
                    },
                    "reply": {
                        "to": to,
                        "cc": cc,
                        "subject": reply_subject(original.subject),
                        "body_preview": body[:200],
                        "attachments": list(attachments or []),
                    },
                }
                return _prepared_action(
                    CONFIRMATIONS.prepare(
                        "reply_draft",
                        _reply_payload(message_id, folder, body, reply_all, attachments),
                        preview=preview,
                    )
                )
            except (ConfigError, MailboxError, SandboxError, ConfirmationError) as exc:
                raise _guard(exc) from exc

        @server.tool(annotations=DESTRUCTIVE_WRITE)
        @_audited("commit_reply_draft")
        def commit_reply_draft(
            token: str,
            message_id: str,
            folder: str = "INBOX",
            body: str = "",
            reply_all: bool = False,
            attachments: list[str] | None = None,
        ) -> DraftCreated:
            """Commit a prepared reply draft. The token and every argument must match
            prepare_reply_draft.

            Args:
                token: Token returned by prepare_reply_draft.
                message_id: Message-ID of the message being replied to.
                folder: Folder containing that message.
                body: Optional reply text placed above the quoted original.
                reply_all: Also add the original To/Cc recipients (self excluded).
                attachments: Sandbox filenames to attach; nothing outside the sandbox.
            """
            try:
                payload = _reply_payload(message_id, folder, body, reply_all, attachments)
                CONFIRMATIONS.commit(token, payload)
                client = get_client()
                key = _idempotency_key("reply_draft", payload)
                remembered = _remembered_draft(key, client)
                if remembered is not None:
                    return remembered
                original = client.get_message(message_id, folder=folder, max_chars=20000)
                composed = build_reply(
                    original,
                    client.config.username,
                    body=body,
                    reply_all=reply_all,
                    attachments=_sandbox_attachments(
                        policy, attachments or [], max_total=policy.send.max_message_bytes
                    ),
                )
                uid = client.append_to_drafts(composed.raw)
                created = DraftCreated(
                    uid=uid,
                    message_id=composed.message_id,
                    folder=client.find_drafts_folder(),
                    subject=reply_subject(original.subject),
                )
                _remember_draft(key, created)
                return created
            except (ConfigError, MailboxError, ComposeError, SandboxError, ConfirmationError) as exc:
                raise _guard(exc) from exc

        @server.tool(annotations=WRITE_ACTION)
        @_audited("prepare_forward_draft")
        def prepare_forward_draft(
            message_id: str,
            folder: str = "INBOX",
            to: str = "",
            body: str = "",
            include_attachments: bool = False,
            attachments: list[str] | None = None,
        ) -> PreparedAction:
            """Prepare a forward draft containing the original message. Nothing is saved
            until commit_forward_draft is called with the same arguments and the returned
            token.

            Args:
                message_id: Message-ID of the message being forwarded.
                folder: Folder containing that message.
                to: Comma-separated recipients; may be empty.
                body: Optional text placed above the forwarded block.
                include_attachments: Re-attach the original message's attachments.
                attachments: Additional sandbox filenames to attach.
            """
            try:
                client = get_client()
                original = client.get_message(message_id, folder=folder, max_chars=20000)
                sandbox = _sandbox_attachments(
                    policy, attachments or [], max_total=policy.send.max_message_bytes
                )
                originals: list[DraftAttachment] = []
                if include_attachments:
                    originals = _original_attachments(
                        client, policy, message_id, folder
                    )
                preview = {
                    "target": {
                        "message_id": original.message_id,
                        "subject": original.subject,
                        "sender": original.sender,
                    },
                    "forward": {
                        "to": validate_recipients(to, header="To"),
                        "subject": forward_subject(original.subject),
                        "body_preview": body[:200],
                        "attachments": {
                            "sandbox": [item.filename for item in sandbox],
                            "original": [item.filename for item in originals],
                        },
                    },
                }
                return _prepared_action(
                    CONFIRMATIONS.prepare(
                        "forward_draft",
                        _forward_payload(
                            message_id, folder, to, body, include_attachments, attachments
                        ),
                        preview=preview,
                    )
                )
            except (ConfigError, MailboxError, SandboxError, ConfirmationError) as exc:
                raise _guard(exc) from exc

        @server.tool(annotations=DESTRUCTIVE_WRITE)
        @_audited("commit_forward_draft")
        def commit_forward_draft(
            token: str,
            message_id: str,
            folder: str = "INBOX",
            to: str = "",
            body: str = "",
            include_attachments: bool = False,
            attachments: list[str] | None = None,
        ) -> DraftCreated:
            """Commit a prepared forward draft. The token and every argument must match
            prepare_forward_draft.

            Args:
                token: Token returned by prepare_forward_draft.
                message_id: Message-ID of the message being forwarded.
                folder: Folder containing that message.
                to: Comma-separated recipients; may be empty.
                body: Optional text placed above the forwarded block.
                include_attachments: Re-attach the original message's attachments.
                attachments: Additional sandbox filenames to attach.
            """
            try:
                payload = _forward_payload(
                    message_id, folder, to, body, include_attachments, attachments
                )
                CONFIRMATIONS.commit(token, payload)
                client = get_client()
                key = _idempotency_key("forward_draft", payload)
                remembered = _remembered_draft(key, client)
                if remembered is not None:
                    return remembered
                original = client.get_message(message_id, folder=folder, max_chars=20000)
                composed_attachments = _sandbox_attachments(
                    policy, attachments or [], max_total=policy.send.max_message_bytes
                )
                if include_attachments:
                    composed_attachments.extend(
                        _original_attachments(client, policy, message_id, folder)
                    )
                composed = build_forward(
                    original,
                    client.config.username,
                    to=validate_recipients(to, header="To"),
                    body=body,
                    attachments=composed_attachments,
                )
                uid = client.append_to_drafts(composed.raw)
                created = DraftCreated(
                    uid=uid,
                    message_id=composed.message_id,
                    folder=client.find_drafts_folder(),
                    subject=forward_subject(original.subject),
                )
                _remember_draft(key, created)
                return created
            except (ConfigError, MailboxError, ComposeError, SandboxError, ConfirmationError) as exc:
                raise _guard(exc) from exc

        @server.tool(annotations=READ_ONLY)
        @_audited("preview_draft")
        def preview_draft(
            to: str = "",
            cc: str = "",
            bcc: str = "",
            subject: str = "",
            body: str = "",
            in_reply_to: str = "",
            references: str = "",
        ) -> DraftPreview:
            """Render the exact draft that create_draft would save, without saving
            anything. Recipients outside the account's domain are flagged in
            external_recipients.

            Args:
                to: Comma-separated recipients; may be empty.
                cc: Comma-separated carbon-copy recipients.
                bcc: Comma-separated blind carbon-copy recipients.
                subject: Subject line.
                body: Plain-text body.
                in_reply_to: Optional Message-ID this draft replies to.
                references: Optional space-separated Message-ID chain.
            """
            try:
                client = get_client()
                composed = build_draft(
                    client.config.username,
                    to=validate_recipients(to, header="To"),
                    cc=validate_recipients(cc, header="Cc"),
                    bcc=validate_recipients(bcc, header="Bcc"),
                    subject=subject,
                    body=body,
                    in_reply_to=in_reply_to,
                    references=references,
                )
                view = draft_view(composed, client.config.username)
                return DraftPreview(
                    headers=view.headers,
                    recipients=view.recipients,
                    external_recipients=view.external_recipients,
                    body_text=view.body_text,
                    raw=view.raw_text,
                )
            except (ConfigError, MailboxError, ComposeError) as exc:
                raise _guard(exc) from exc

    if capabilities.organize:

        @server.tool(annotations=WRITE_ACTION)
        @_audited("prepare_set_flags")
        def prepare_set_flags(
            message_ids: list[str], action: str, folder: str = "INBOX"
        ) -> PreparedAction:
            """Prepare a seen/flagged state change for one or more messages. Nothing
            changes until commit_set_flags is called with the same arguments and the
            returned token.

            Args:
                message_ids: Message-ID values, as returned by list_emails/search_emails.
                action: One of read, unread, flag, unflag.
                folder: Folder containing the messages.
            """
            try:
                ids = _dedupe(message_ids)
                _check_bulk(policy, ids)
                if action not in FLAG_ACTIONS:
                    raise MailboxError(
                        f"action must be one of: {', '.join(sorted(FLAG_ACTIONS))}"
                    )
                client = get_client()
                resolved = _resolve_source(client, policy, folder)
                preview = {
                    "action": "set_flags",
                    "flag_action": action,
                    "folder": resolved,
                    "count": len(ids),
                    "message_ids": ids,
                }
                return _prepared_action(
                    CONFIRMATIONS.prepare(
                        "set_flags", _flags_payload(ids, action, resolved), preview=preview
                    )
                )
            except (ConfigError, MailboxError, ConfirmationError) as exc:
                raise _guard(exc) from exc

        @server.tool(annotations=DESTRUCTIVE_WRITE)
        @_audited("commit_set_flags")
        def commit_set_flags(
            token: str, message_ids: list[str], action: str, folder: str = "INBOX"
        ) -> OrganizeResult:
            """Commit a prepared seen/flagged state change. The token and every argument
            must match prepare_set_flags.

            Args:
                token: Token returned by prepare_set_flags.
                message_ids: Message-ID values.
                action: One of read, unread, flag, unflag.
                folder: Folder containing the messages.
            """
            try:
                ids = _dedupe(message_ids)
                _check_bulk(policy, ids)
                if action not in FLAG_ACTIONS:
                    raise MailboxError(
                        f"action must be one of: {', '.join(sorted(FLAG_ACTIONS))}"
                    )
                client = get_client()
                resolved = _resolve_source(client, policy, folder)
                CONFIRMATIONS.commit(token, _flags_payload(ids, action, resolved))
                add, remove = FLAG_ACTIONS[action]
                updated, missing = client.set_flags(ids, resolved, add=add, remove=remove)
                return OrganizeResult(
                    action="set_flags",
                    folder=resolved,
                    updated=updated,
                    missing=missing,
                )
            except (ConfigError, MailboxError, ConfirmationError) as exc:
                raise _guard(exc) from exc

        @server.tool(annotations=WRITE_ACTION)
        @_audited("prepare_move")
        def prepare_move(
            message_ids: list[str], source: str = "INBOX", destination: str = ""
        ) -> PreparedAction:
            """Prepare moving messages to another folder. Nothing changes until
            commit_move is called with the same arguments and the returned token.

            Args:
                message_ids: Message-ID values, as returned by list_emails/search_emails.
                source: Folder containing the messages.
                destination: Target folder name, or the aliases 'archive' and 'trash'.
            """
            try:
                ids = _dedupe(message_ids)
                _check_bulk(policy, ids)
                client = get_client()
                resolved_source = _resolve_source(client, policy, source)
                resolved_destination = _resolve_destination(client, policy, destination)
                if resolved_destination == resolved_source:
                    raise MailboxError("source and destination are the same folder")
                preview = {
                    "action": "move",
                    "source": resolved_source,
                    "destination": resolved_destination,
                    "count": len(ids),
                    "message_ids": ids,
                }
                return _prepared_action(
                    CONFIRMATIONS.prepare(
                        "move",
                        _move_payload(ids, resolved_source, resolved_destination),
                        preview=preview,
                    )
                )
            except (ConfigError, MailboxError, ConfirmationError) as exc:
                raise _guard(exc) from exc

        @server.tool(annotations=DESTRUCTIVE_WRITE)
        @_audited("commit_move")
        def commit_move(
            token: str,
            message_ids: list[str],
            source: str = "INBOX",
            destination: str = "",
        ) -> OrganizeResult:
            """Commit a prepared move. The token and every argument must match
            prepare_move.

            Args:
                token: Token returned by prepare_move.
                message_ids: Message-ID values.
                source: Folder containing the messages.
                destination: Target folder name, or the aliases 'archive' and 'trash'.
            """
            try:
                ids = _dedupe(message_ids)
                _check_bulk(policy, ids)
                client = get_client()
                resolved_source = _resolve_source(client, policy, source)
                resolved_destination = _resolve_destination(client, policy, destination)
                if resolved_destination == resolved_source:
                    raise MailboxError("source and destination are the same folder")
                CONFIRMATIONS.commit(
                    token, _move_payload(ids, resolved_source, resolved_destination)
                )
                updated, missing = client.move_messages(
                    ids, resolved_source, resolved_destination
                )
                if updated:
                    JOURNAL.record(resolved_source, resolved_destination, updated)
                return OrganizeResult(
                    action="move",
                    folder=resolved_source,
                    destination=resolved_destination,
                    updated=updated,
                    missing=missing,
                )
            except (ConfigError, MailboxError, ConfirmationError) as exc:
                raise _guard(exc) from exc

        @server.tool(annotations=WRITE_ACTION)
        @_audited("prepare_label_change")
        def prepare_label_change(
            message_ids: list[str],
            label: str,
            folder: str = "INBOX",
            add: bool = True,
        ) -> PreparedAction:
            """Prepare adding or removing a Proton label on messages. Labels are the
            folders under 'Labels/'; the message stays in its folder. Nothing changes
            until commit_label_change is called with the same arguments and the token.

            Args:
                message_ids: Message-ID values, as returned by list_emails/search_emails.
                label: Label folder, for example 'Labels/Important'.
                folder: Folder containing the messages (used when adding).
                add: True to add the label, False to remove it.
            """
            try:
                ids = _dedupe(message_ids)
                _check_bulk(policy, ids)
                client = get_client()
                resolved_folder = _resolve_source(client, policy, folder)
                resolved_label = _resolve_label(client, policy, label)
                preview = {
                    "action": "add_label" if add else "remove_label",
                    "label": resolved_label,
                    "folder": resolved_folder,
                    "count": len(ids),
                    "message_ids": ids,
                }
                return _prepared_action(
                    CONFIRMATIONS.prepare(
                        "label_change",
                        _label_payload(ids, resolved_folder, resolved_label, add),
                        preview=preview,
                    )
                )
            except (ConfigError, MailboxError, ConfirmationError) as exc:
                raise _guard(exc) from exc

        @server.tool(annotations=DESTRUCTIVE_WRITE)
        @_audited("commit_label_change")
        def commit_label_change(
            token: str,
            message_ids: list[str],
            label: str,
            folder: str = "INBOX",
            add: bool = True,
        ) -> OrganizeResult:
            """Commit a prepared label change. The token and every argument must match
            prepare_label_change.

            Args:
                token: Token returned by prepare_label_change.
                message_ids: Message-ID values.
                label: Label folder, for example 'Labels/Important'.
                folder: Folder containing the messages (used when adding).
                add: True to add the label, False to remove it.
            """
            try:
                ids = _dedupe(message_ids)
                _check_bulk(policy, ids)
                client = get_client()
                resolved_folder = _resolve_source(client, policy, folder)
                resolved_label = _resolve_label(client, policy, label)
                CONFIRMATIONS.commit(
                    token, _label_payload(ids, resolved_folder, resolved_label, add)
                )
                if add:
                    updated, missing = client.add_label(ids, resolved_folder, resolved_label)
                    action = "add_label"
                else:
                    updated, missing = client.remove_label(ids, resolved_label)
                    action = "remove_label"
                return OrganizeResult(
                    action=action,
                    folder=resolved_folder,
                    destination=resolved_label,
                    updated=updated,
                    missing=missing,
                )
            except (ConfigError, MailboxError, ConfirmationError) as exc:
                raise _guard(exc) from exc

        @server.tool(annotations=WRITE_ACTION)
        @_audited("prepare_undo_move")
        def prepare_undo_move() -> PreparedAction:
            """Prepare undoing the most recent journaled move. Nothing changes until
            commit_undo_move is called with the returned entry id and token."""
            try:
                entry = JOURNAL.last()
                if entry is None:
                    raise MailboxError("no move to undo")
                return _undo_preview(entry)
            except (ConfigError, MailboxError, ConfirmationError) as exc:
                raise _guard(exc) from exc

        @server.tool(annotations=DESTRUCTIVE_WRITE)
        @_audited("commit_undo_move")
        def commit_undo_move(token: str, entry_id: int) -> OrganizeResult:
            """Commit undoing a recently journaled move. The token and entry_id must
            match prepare_undo_move.

            Args:
                token: Token returned by prepare_undo_move.
                entry_id: Move journal entry id from the prepare preview.
            """
            try:
                CONFIRMATIONS.commit(token, _undo_payload(entry_id))
                entry = JOURNAL.get(entry_id)
                if entry is None:
                    raise MailboxError(f"unknown move journal entry {entry_id}")
                if entry.undone:
                    raise MailboxError(f"move journal entry {entry_id} is already undone")
                client = get_client()
                updated, missing = client.move_messages(
                    list(entry.message_ids), entry.destination, entry.source
                )
                JOURNAL.mark_undone(entry_id)
                return OrganizeResult(
                    action="undo_move",
                    folder=entry.destination,
                    destination=entry.source,
                    updated=updated,
                    missing=missing,
                )
            except (ConfigError, MailboxError, ConfirmationError) as exc:
                raise _guard(exc) from exc

    if capabilities.send:

        @server.tool(annotations=WRITE_ACTION)
        @_audited("prepare_send_draft")
        def prepare_send_draft(message_id: str, folder: str = "") -> PreparedAction:
            """Prepare sending an existing draft through Bridge SMTP. Runs the recipient
            allowlist, quota, size and loop-guard checks and returns a preview plus a
            single-use token; nothing is sent until commit_send_draft is called with the
            same arguments and the token.

            Args:
                message_id: Message-ID of the draft, as returned by list_drafts.
                folder: Drafts folder; empty uses the folder flagged \\Drafts.
            """
            try:
                client = get_client()
                state = send_state
                if state is None:
                    raise SendError("send state is not initialized")
                resolved_folder = _resolve_draft_folder(client, folder)
                uid, raw = client.get_draft_raw(message_id, resolved_folder)
                transmission = transmission_from_raw(raw)
                _check_sendable(client, policy, state, raw, transmission)
                preview = {
                    "draft": {
                        "message_id": message_id,
                        "folder": resolved_folder,
                        "uid": uid,
                        "subject": transmission.subject,
                    },
                    "recipients": {
                        "to": list(transmission.to),
                        "cc": list(transmission.cc),
                        "bcc": list(transmission.bcc),
                        "envelope": list(transmission.envelope),
                        "external": external_recipients(
                            transmission.envelope, client.config.username
                        ),
                    },
                    "attachments": list(transmission.attachments),
                    "body_preview": transmission.body[:200],
                    "payload_hash": transmission.payload_hash,
                    "quota": state.quota_status(policy.send),
                    "warnings": list(transmission.warnings),
                }
                return _prepared_action(
                    CONFIRMATIONS.prepare(
                        "send_draft",
                        _send_payload(message_id, resolved_folder),
                        preview=preview,
                    )
                )
            except (ConfigError, MailboxError, SendError, ConfirmationError) as exc:
                raise _guard(exc) from exc

        @server.tool(annotations=DESTRUCTIVE_WRITE)
        @_audited("commit_send_draft")
        def commit_send_draft(token: str, message_id: str, folder: str = "") -> DraftSent:
            """Commit a prepared send: re-runs every check, submits the draft through
            Bridge SMTP, records the quota/idempotency state, then removes the draft. The
            token and every argument must match prepare_send_draft.

            Args:
                token: Token returned by prepare_send_draft.
                message_id: Message-ID of the draft.
                folder: Drafts folder; empty uses the folder flagged \\Drafts.
            """
            try:
                client = get_client()
                state = send_state
                if state is None:
                    raise SendError("send state is not initialized")
                resolved_folder = _resolve_draft_folder(client, folder)
                CONFIRMATIONS.commit(token, _send_payload(message_id, resolved_folder))
                key = send_key(message_id, resolved_folder)
                if state.is_duplicate(key, policy.send.duplicate_window_seconds):
                    return DraftSent(message_id=message_id, duplicate=True)
                uid, raw = client.get_draft_raw(message_id, resolved_folder)
                transmission = transmission_from_raw(raw)
                _check_sendable(client, policy, state, raw, transmission)
                get_smtp_sender().send(transmission.payload, transmission.envelope)
                state.record_send(message_id)
                state.remember_key(key)
                state.remember_body(body_digest(transmission.body))
                warnings: list[str] = []
                try:
                    client.delete_draft(uid)
                except MailboxError as exc:
                    warnings.append(f"sent, but the draft could not be deleted: {exc}")
                return DraftSent(
                    message_id=message_id,
                    recipients=list(transmission.envelope),
                    warnings=warnings,
                )
            except (ConfigError, MailboxError, SendError, ConfirmationError) as exc:
                raise _guard(exc) from exc

    if capabilities.delete:

        @server.tool(annotations=WRITE_ACTION)
        @_audited("prepare_delete_message")
        def prepare_delete_message(message_id: str, folder: str = "") -> PreparedAction:
            """Prepare permanently deleting one message. The message must currently be in
            Trash. Nothing is erased until commit_delete_message is called with the exact
            confirmation phrase from the preview and the returned token.

            Args:
                message_id: Message-ID of the message to erase.
                folder: Must be the Trash folder; empty uses the folder flagged \\Trash.
            """
            try:
                client = get_client()
                resolved = _resolve_trash(client, folder)
                content = client.get_message(message_id, folder=resolved, max_chars=200)
                preview = {
                    "target": {
                        "message_id": content.message_id,
                        "subject": content.subject,
                        "sender": content.sender,
                        "date": content.date or content.received,
                        "folder": resolved,
                    },
                    "irreversible": True,
                    "confirm_phrase": _delete_phrase(message_id),
                }
                return _prepared_action(
                    CONFIRMATIONS.prepare(
                        "delete_message",
                        _delete_message_payload(message_id, resolved),
                        preview=preview,
                    )
                )
            except (ConfigError, MailboxError, ConfirmationError) as exc:
                raise _guard(exc) from exc

        @server.tool(annotations=DESTRUCTIVE_WRITE)
        @_audited("commit_delete_message")
        def commit_delete_message(
            token: str,
            message_id: str,
            confirm: str,
            folder: str = "",
        ) -> MessageDeleted:
            """Commit a prepared permanent deletion: irreversibly erases the message
            from Trash. The token, message_id and confirmation phrase must match
            prepare_delete_message.

            Args:
                token: Token returned by prepare_delete_message.
                message_id: Message-ID of the message to erase.
                confirm: Must be exactly 'permanently delete <message_id>'.
                folder: Must be the Trash folder; empty uses the folder flagged \\Trash.
            """
            try:
                client = get_client()
                resolved = _resolve_trash(client, folder)
                if confirm != _delete_phrase(message_id):
                    raise MailboxError(
                        "confirmation phrase does not match; expected "
                        f"'permanently delete {message_id}'"
                    )
                CONFIRMATIONS.commit(token, _delete_message_payload(message_id, resolved))
                client.delete_message_permanently(message_id, resolved)
                return MessageDeleted(message_id=message_id, folder=resolved)
            except (ConfigError, MailboxError, ConfirmationError) as exc:
                raise _guard(exc) from exc

    @server.resource(
        "mail://folders",
        name="folders",
        description="Every folder and label, with IMAP flags and selectability",
        mime_type="application/json",
    )
    def resource_folders() -> list[dict[str, Any]]:
        return [folder.model_dump() for folder in get_client().list_folders()]

    @server.resource(
        "mail://status",
        name="status",
        description="Total and unread counts per selectable folder",
        mime_type="application/json",
    )
    def resource_status() -> list[dict[str, Any]]:
        return [status.model_dump() for status in get_client().get_status()]

    @server.resource(
        "mail://message/{message_id}",
        name="message",
        description="A full message by Message-ID; percent-encode the angle brackets",
        mime_type="application/json",
    )
    def resource_message(message_id: str) -> dict[str, Any]:
        decoded = unquote(message_id)
        client = get_client()
        folder = client.folder_by_flag("\\All") or "INBOX"
        try:
            return client.get_message(decoded, folder=folder).model_dump()
        except MessageNotFoundError:
            drafts = client.find_drafts_folder()
            return client.get_message(decoded, folder=drafts).model_dump()

    @server.resource(
        "mail://thread/{message_id}",
        name="thread",
        description="A conversation reconstructed from References and In-Reply-To",
        mime_type="application/json",
    )
    def resource_thread(message_id: str) -> list[dict[str, Any]]:
        decoded = unquote(message_id)
        return [summary.model_dump() for summary in get_client().get_thread(decoded)]

    if policy.notifications.enabled:

        @server.resource(
            NOTIFICATIONS_URI,
            name="inbox",
            description=(
                "Unread and total counts plus recent message identifiers; no bodies"
            ),
            mime_type="application/json",
        )
        def resource_inbox() -> dict[str, Any]:
            client = get_client()
            statuses = client.get_status(policy.notifications.folder)
            page = client.list_emails(folder=policy.notifications.folder, limit=5)
            return {
                "folder": policy.notifications.folder,
                "unread": statuses[0].unread if statuses else 0,
                "total": statuses[0].total if statuses else 0,
                "recent": [
                    {
                        "message_id": item.message_id,
                        "received": item.received,
                        "unread": item.unread,
                    }
                    for item in page.messages
                ],
            }

    return server


POLICY = load_policy()
CONFIRMATIONS = ConfirmationManager(ttl_seconds=POLICY.confirmation_ttl_seconds)
IDEMPOTENCY = IdempotencyStore(window_seconds=POLICY.idempotency_window_seconds)
JOURNAL = MoveJournal()
server = build_server(POLICY)
