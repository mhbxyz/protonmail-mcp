from __future__ import annotations

import threading

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .bridge import BridgeClient, MailboxError, MessageNotFoundError
from .config import BridgeConfig, ConfigError
from .models import EmailContent, EmailSummary, Folder

READ_ONLY = ToolAnnotations(read_only_hint=True)

server = MCPServer(
    name="protonmail",
    title="Proton Mail (read-only)",
    version="0.1.0",
    instructions=(
        "Read-only access to a Proton Mail mailbox through a local Proton Bridge instance. "
        "Use list_folders to discover folders and labels, list_emails for recent mail, "
        "search_emails for full-text search, and read_email to read one message by its "
        "Message-ID. No tool can send, delete, move, or modify anything."
    ),
)

_client_lock = threading.Lock()
_client: BridgeClient | None = None


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


@server.tool(annotations=READ_ONLY)
def list_folders() -> list[Folder]:
    """List every folder and label of the mailbox. Use this first when you do not know
    where a message lives; the returned names are valid values for other tools."""
    try:
        return get_client().list_folders()
    except (ConfigError, MailboxError) as exc:
        raise _guard(exc) from exc


@server.tool(annotations=READ_ONLY)
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
