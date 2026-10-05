from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from mcp import Client

from protonmail_mcp.bridge import MailboxError
from protonmail_mcp.models import EmailSummary, Folder
from protonmail_mcp.server import server, set_client


class FakeMailbox:
    def __init__(self) -> None:
        self.config = SimpleNamespace(username="me@proton.me", endpoint="127.0.0.1:1143")

    def list_folders(self) -> list[Folder]:
        return [Folder(name="INBOX"), Folder(name="Projets")]

    def list_emails(self, **kwargs: Any) -> list[EmailSummary]:
        return [
            EmailSummary(
                message_id="<hello@example.com>",
                folder="INBOX",
                uid=1,
                subject="Bonjour",
                sender="Alice <alice@example.com>",
            )
        ]

    def search_emails(self, query: str, folder: str = "INBOX", limit: int = 20) -> list[EmailSummary]:
        return []

    def get_message(self, message_id: str, folder: str = "INBOX", max_chars: int = 20000) -> Any:
        raise MailboxError(f"unknown {message_id}")

    def close(self) -> None:
        pass


class BrokenMailbox(FakeMailbox):
    def list_folders(self) -> list[Folder]:
        raise MailboxError("bridge is down")


def call(name: str, arguments: dict[str, Any] | None = None) -> Any:
    async def run() -> Any:
        async with Client(server) as client:
            return await client.call_tool(name, arguments or {})

    return asyncio.run(run())


def with_mailbox(mailbox: FakeMailbox):
    set_client(mailbox)  # type: ignore[arg-type]

    def cleanup() -> None:
        set_client(None)

    return cleanup


def list_tools() -> list[Any]:
    return asyncio.run(server.list_tools())


def test_registered_tools() -> None:
    names = {tool.name for tool in list_tools()}
    assert {"list_folders", "list_emails", "search_emails", "read_email"} <= names


def test_all_tools_are_read_only() -> None:
    for tool in list_tools():
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True


def test_list_folders() -> None:
    cleanup = with_mailbox(FakeMailbox())
    try:
        result = call("list_folders")
    finally:
        cleanup()
    assert result.is_error in (False, None)
    assert "INBOX" in result.content[0].text


def test_list_emails_passes_filters() -> None:
    mailbox = FakeMailbox()
    cleanup = with_mailbox(mailbox)
    try:
        result = call("list_emails", {"unread_only": True, "limit": 5})
    finally:
        cleanup()
    assert result.is_error in (False, None)
    assert "Bonjour" in result.content[0].text


def test_tool_errors_are_reported() -> None:
    cleanup = with_mailbox(BrokenMailbox())
    try:
        result = call("list_folders")
    finally:
        cleanup()
    assert result.is_error is True
    assert "bridge is down" in result.content[0].text
