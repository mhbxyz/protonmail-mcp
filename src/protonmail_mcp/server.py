from __future__ import annotations

import functools
import threading
import time
from collections.abc import Callable
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
from .models import (
    DraftCreated,
    DraftDeleted,
    DraftPreview,
    EmailContent,
    EmailSummary,
    Folder,
    PreparedAction,
)
from .policy import Policy, load_policy

READ_ONLY = ToolAnnotations(read_only_hint=True)
WRITE_DRAFT = ToolAnnotations(
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

        @server.tool(annotations=WRITE_DRAFT)
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

        @server.tool(annotations=WRITE_DRAFT)
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

        @server.tool(annotations=WRITE_DRAFT)
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

        @server.tool(annotations=WRITE_DRAFT)
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

        @server.tool(annotations=WRITE_DRAFT)
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

    return server


POLICY = load_policy()
CONFIRMATIONS = ConfirmationManager(ttl_seconds=POLICY.confirmation_ttl_seconds)
IDEMPOTENCY = IdempotencyStore(window_seconds=POLICY.idempotency_window_seconds)
server = build_server(POLICY)
