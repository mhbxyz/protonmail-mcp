from __future__ import annotations

import functools
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .audit import AuditLog
from .bridge import BridgeClient, MailboxError, MessageNotFoundError
from .compose import (
    ComposeError,
    build_draft,
    build_forward,
    build_reply,
    draft_view,
    forward_subject,
    reply_recipients,
    reply_subject,
    validate_recipients,
)
from .config import BridgeConfig, ConfigError
from .confirmations import (
    ConfirmationError,
    ConfirmationManager,
    PreparedConfirmation,
    payload_digest,
)
from .idempotency import DraftReference, IdempotencyStore
from .journal import MoveEntry, MoveJournal
from .models import (
    DraftCreated,
    DraftDeleted,
    DraftPreview,
    EmailContent,
    EmailSummary,
    Folder,
    OrganizeResult,
    PreparedAction,
)
from .policy import Policy, load_policy

READ_ONLY = ToolAnnotations(read_only_hint=True)
WRITE_ACTION = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False
)
DESTRUCTIVE_WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False
)

_client_lock = threading.Lock()
_client: BridgeClient | None = None

AUDIT = AuditLog.from_env()


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
            _client = BridgeClient(BridgeConfig.from_env())
        return _client


def set_client(client: BridgeClient | None) -> None:
    global _client
    with _client_lock:
        _client = client


def _guard(exc: Exception) -> ToolError:
    return ToolError(str(exc))


def _clamp(limit: int) -> int:
    return max(1, min(int(limit), 100))


def _update_payload(uid: int, to: str, cc: str, bcc: str, subject: str, body: str) -> dict[str, Any]:
    return {
        "action": "update_draft",
        "uid": uid,
        "to": to,
        "cc": cc,
        "bcc": bcc,
        "subject": subject,
        "body": body,
    }


def _delete_payload(uid: int) -> dict[str, Any]:
    return {"action": "delete_draft", "uid": uid}


def _reply_payload(message_id: str, folder: str, body: str, reply_all: bool) -> dict[str, Any]:
    return {
        "action": "reply_draft",
        "message_id": message_id,
        "folder": folder,
        "body": body,
        "reply_all": reply_all,
    }


def _forward_payload(message_id: str, folder: str, to: str, body: str) -> dict[str, Any]:
    return {
        "action": "forward_draft",
        "message_id": message_id,
        "folder": folder,
        "to": to,
        "body": body,
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


def build_server(policy: Policy) -> MCPServer:
    capabilities = policy.capabilities
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
    server = MCPServer(
        name="protonmail",
        title="Proton Mail",
        version="0.1.0",
        instructions=(
            "Access to a Proton Mail mailbox through a local Proton Bridge instance. "
            f"Active policy: mode={policy.mode}, capabilities: {capabilities.describe()}. "
            "Use list_folders to discover folders and labels, list_emails for recent mail, "
            "search_emails for full-text search, and read_email to read one message by its "
            "Message-ID. Tools outside the active mode are not registered."
            + draft_hint
            + organize_hint
        ),
    )

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
    ) -> list[EmailSummary]:
        """List the most recent messages of a folder, newest first.

        Args:
            folder: Folder or label name, as returned by list_folders.
            limit: Maximum number of messages to return (1-100).
            unread_only: Only return unread messages when true.
            since_days: Only return messages from the last N days when set.
            sender: Only return messages whose From header contains this text.
            subject: Only return messages whose Subject header contains this text.
        """
        try:
            return get_client().list_emails(
                folder=folder,
                limit=_clamp(limit),
                unread_only=unread_only,
                since_days=since_days,
                sender=sender,
                subject=subject,
            )
        except (ConfigError, MailboxError) as exc:
            raise _guard(exc) from exc

    @server.tool(annotations=READ_ONLY)
    @_audited("search_emails")
    def search_emails(query: str, folder: str = "INBOX", limit: int = 20) -> list[EmailSummary]:
        """Search messages whose headers or body contain a text query, newest first.

        Args:
            query: Text to search for across headers and body.
            folder: Folder or label name to search in, as returned by list_folders.
            limit: Maximum number of messages to return (1-100).
        """
        try:
            return get_client().search_emails(query, folder=folder, limit=_clamp(limit))
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
            except (ConfigError, MailboxError, ComposeError) as exc:
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
            """
            try:
                existing = get_client().get_draft(uid, max_chars=200)
                payload = _update_payload(uid, to, cc, bcc, subject, body)
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
            """
            try:
                CONFIRMATIONS.commit(token, _update_payload(uid, to, cc, bcc, subject, body))
                client = get_client()
                composed = build_draft(
                    client.config.username,
                    to=validate_recipients(to, header="To"),
                    cc=validate_recipients(cc, header="Cc"),
                    bcc=validate_recipients(bcc, header="Bcc"),
                    subject=subject,
                    body=body,
                )
                new_uid = client.replace_draft(uid, composed.raw)
                return DraftCreated(
                    uid=new_uid,
                    message_id=composed.message_id,
                    folder=client.find_drafts_folder(),
                    subject=subject,
                )
            except (ConfigError, MailboxError, ComposeError, ConfirmationError) as exc:
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
        ) -> PreparedAction:
            """Prepare a reply draft to an existing message, with quoting and threading
            headers. Nothing is saved until commit_reply_draft is called with the same
            arguments and the returned token.

            Args:
                message_id: Message-ID of the message being replied to.
                folder: Folder containing that message.
                body: Optional reply text placed above the quoted original.
                reply_all: Also add the original To/Cc recipients (self excluded).
            """
            try:
                client = get_client()
                original = client.get_message(message_id, folder=folder, max_chars=20000)
                to, cc = reply_recipients(original, client.config.username, reply_all)
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
                    },
                }
                return _prepared_action(
                    CONFIRMATIONS.prepare(
                        "reply_draft",
                        _reply_payload(message_id, folder, body, reply_all),
                        preview=preview,
                    )
                )
            except (ConfigError, MailboxError, ConfirmationError) as exc:
                raise _guard(exc) from exc

        @server.tool(annotations=DESTRUCTIVE_WRITE)
        @_audited("commit_reply_draft")
        def commit_reply_draft(
            token: str,
            message_id: str,
            folder: str = "INBOX",
            body: str = "",
            reply_all: bool = False,
        ) -> DraftCreated:
            """Commit a prepared reply draft. The token and every argument must match
            prepare_reply_draft.

            Args:
                token: Token returned by prepare_reply_draft.
                message_id: Message-ID of the message being replied to.
                folder: Folder containing that message.
                body: Optional reply text placed above the quoted original.
                reply_all: Also add the original To/Cc recipients (self excluded).
            """
            try:
                payload = _reply_payload(message_id, folder, body, reply_all)
                CONFIRMATIONS.commit(token, payload)
                client = get_client()
                key = _idempotency_key("reply_draft", payload)
                remembered = _remembered_draft(key, client)
                if remembered is not None:
                    return remembered
                original = client.get_message(message_id, folder=folder, max_chars=20000)
                composed = build_reply(
                    original, client.config.username, body=body, reply_all=reply_all
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
            except (ConfigError, MailboxError, ComposeError, ConfirmationError) as exc:
                raise _guard(exc) from exc

        @server.tool(annotations=WRITE_ACTION)
        @_audited("prepare_forward_draft")
        def prepare_forward_draft(
            message_id: str,
            folder: str = "INBOX",
            to: str = "",
            body: str = "",
        ) -> PreparedAction:
            """Prepare a forward draft containing the original message. Nothing is saved
            until commit_forward_draft is called with the same arguments and the returned
            token. Original attachments are listed by name but not attached.

            Args:
                message_id: Message-ID of the message being forwarded.
                folder: Folder containing that message.
                to: Comma-separated recipients; may be empty.
                body: Optional text placed above the forwarded block.
            """
            try:
                client = get_client()
                original = client.get_message(message_id, folder=folder, max_chars=20000)
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
                    },
                }
                return _prepared_action(
                    CONFIRMATIONS.prepare(
                        "forward_draft",
                        _forward_payload(message_id, folder, to, body),
                        preview=preview,
                    )
                )
            except (ConfigError, MailboxError, ConfirmationError) as exc:
                raise _guard(exc) from exc

        @server.tool(annotations=DESTRUCTIVE_WRITE)
        @_audited("commit_forward_draft")
        def commit_forward_draft(
            token: str,
            message_id: str,
            folder: str = "INBOX",
            to: str = "",
            body: str = "",
        ) -> DraftCreated:
            """Commit a prepared forward draft. The token and every argument must match
            prepare_forward_draft.

            Args:
                token: Token returned by prepare_forward_draft.
                message_id: Message-ID of the message being forwarded.
                folder: Folder containing that message.
                to: Comma-separated recipients; may be empty.
                body: Optional text placed above the forwarded block.
            """
            try:
                payload = _forward_payload(message_id, folder, to, body)
                CONFIRMATIONS.commit(token, payload)
                client = get_client()
                key = _idempotency_key("forward_draft", payload)
                remembered = _remembered_draft(key, client)
                if remembered is not None:
                    return remembered
                original = client.get_message(message_id, folder=folder, max_chars=20000)
                composed = build_forward(
                    original,
                    client.config.username,
                    to=validate_recipients(to, header="To"),
                    body=body,
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
            except (ConfigError, MailboxError, ComposeError, ConfirmationError) as exc:
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

    return server


POLICY = load_policy()
CONFIRMATIONS = ConfirmationManager(ttl_seconds=POLICY.confirmation_ttl_seconds)
IDEMPOTENCY = IdempotencyStore(window_seconds=POLICY.idempotency_window_seconds)
JOURNAL = MoveJournal()
server = build_server(POLICY)
