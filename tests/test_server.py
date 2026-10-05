from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

from mcp import Client

from protonmail_mcp.bridge import MailboxError
from protonmail_mcp.models import EmailContent, EmailSummary, Folder
from protonmail_mcp.policy import Capabilities, Policy
from protonmail_mcp.server import build_server, server, set_client


class FakeMailbox:
    def __init__(self) -> None:
        self.config = SimpleNamespace(username="me@proton.me", endpoint="127.0.0.1:1143")
        self.created: list[bytes] = []
        self.replaced: list[int] = []
        self.deleted: list[int] = []

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

    def find_drafts_folder(self) -> str:
        return "Drafts"

    def list_drafts(self, limit: int = 20) -> list[EmailSummary]:
        return [EmailSummary(message_id="<draft-5@test>", folder="Drafts", uid=5, subject="Draft 5")]

    def get_draft(self, uid: int, max_chars: int = 20000) -> EmailContent:
        return EmailContent(
            message_id=f"<draft-{uid}@test>",
            folder="Drafts",
            uid=uid,
            subject=f"Draft {uid}",
            recipients="alice@example.com",
        )

    def append_to_drafts(self, raw: bytes) -> int:
        self.created.append(raw)
        return 42

    def replace_draft(self, uid: int, raw: bytes) -> int:
        self.replaced.append(uid)
        return 43

    def delete_draft(self, uid: int) -> None:
        self.deleted.append(uid)


class BrokenMailbox(FakeMailbox):
    def list_folders(self) -> list[Folder]:
        raise MailboxError("bridge is down")


def call(name: str, arguments: dict[str, Any] | None = None) -> Any:
    async def run() -> Any:
        async with Client(server) as client:
            return await client.call_tool(name, arguments or {})

    return asyncio.run(run())


def call_built(built: Any, name: str, arguments: dict[str, Any] | None = None) -> Any:
    async def run() -> Any:
        async with Client(built) as client:
            return await client.call_tool(name, arguments or {})

    return asyncio.run(run())


def tool_payload(result: Any) -> Any:
    data = result.structured_content
    if data is None:
        data = json.loads(result.content[0].text)
    return data["result"] if isinstance(data, dict) and "result" in data else data


def draft_server() -> Any:
    policy = Policy(
        mode="draft",
        capabilities=Capabilities(draft=True),
        confirmation_ttl_seconds=300,
        source="test",
    )
    return build_server(policy)


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


def test_build_server_registers_read_tools_and_reports_mode() -> None:
    policy = Policy(
        mode="read",
        capabilities=Capabilities(),
        confirmation_ttl_seconds=300,
        source="test",
    )
    built = build_server(policy)
    names = {tool.name for tool in asyncio.run(built.list_tools())}
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


def test_draft_tools_are_gated_by_capability() -> None:
    read_policy = Policy(
        mode="read",
        capabilities=Capabilities(),
        confirmation_ttl_seconds=300,
        source="test",
    )
    read_tools = {tool.name for tool in asyncio.run(build_server(read_policy).list_tools())}
    assert "list_drafts" not in read_tools
    assert "create_draft" not in read_tools
    assert "commit_delete_draft" not in read_tools

    draft_tools = {tool.name for tool in asyncio.run(draft_server().list_tools())}
    assert {
        "list_drafts",
        "create_draft",
        "prepare_update_draft",
        "commit_update_draft",
        "prepare_delete_draft",
        "commit_delete_draft",
    } <= draft_tools


def test_create_draft() -> None:
    mailbox = FakeMailbox()
    cleanup = with_mailbox(mailbox)
    try:
        result = call_built(
            draft_server(),
            "create_draft",
            {"to": "alice@example.com", "subject": "Hello", "body": "Bonjour"},
        )
    finally:
        cleanup()
    assert result.is_error in (False, None)
    assert tool_payload(result)["uid"] == 42
    assert len(mailbox.created) == 1
    assert b"alice@example.com" in mailbox.created[0]


def test_create_draft_rejects_invalid_recipient() -> None:
    cleanup = with_mailbox(FakeMailbox())
    try:
        result = call_built(draft_server(), "create_draft", {"to": "not-an-email"})
    finally:
        cleanup()
    assert result.is_error is True
    assert "invalid email address" in result.content[0].text


def test_delete_draft_requires_matching_token() -> None:
    built = draft_server()
    mailbox = FakeMailbox()
    cleanup = with_mailbox(mailbox)
    try:
        prepared = call_built(built, "prepare_delete_draft", {"uid": 5})
        token = tool_payload(prepared)["token"]

        mismatched = call_built(built, "commit_delete_draft", {"token": token, "uid": 6})
        assert mismatched.is_error is True
        assert mailbox.deleted == []

        unknown = call_built(built, "commit_delete_draft", {"token": "bogus", "uid": 5})
        assert unknown.is_error is True
        assert mailbox.deleted == []

        fresh = tool_payload(call_built(built, "prepare_delete_draft", {"uid": 5}))["token"]
        committed = call_built(built, "commit_delete_draft", {"token": fresh, "uid": 5})
    finally:
        cleanup()
    assert committed.is_error in (False, None)
    assert mailbox.deleted == [5]


def test_update_draft_requires_matching_token() -> None:
    built = draft_server()
    mailbox = FakeMailbox()
    cleanup = with_mailbox(mailbox)
    try:
        prepared = call_built(
            built,
            "prepare_update_draft",
            {"uid": 5, "to": "bob@example.com", "subject": "New", "body": "Body"},
        )
        token = tool_payload(prepared)["token"]

        tampered = call_built(
            built,
            "commit_update_draft",
            {"token": token, "uid": 5, "to": "bob@example.com", "subject": "New", "body": "Tampered"},
        )
        assert tampered.is_error is True
        assert mailbox.replaced == []

        fresh = tool_payload(
            call_built(
                built,
                "prepare_update_draft",
                {"uid": 5, "to": "bob@example.com", "subject": "New", "body": "Body"},
            )
        )["token"]
        committed = call_built(
            built,
            "commit_update_draft",
            {"token": fresh, "uid": 5, "to": "bob@example.com", "subject": "New", "body": "Body"},
        )
    finally:
        cleanup()
    assert committed.is_error in (False, None)
    assert mailbox.replaced == [5]
    assert tool_payload(committed)["uid"] == 43
