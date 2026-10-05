from __future__ import annotations

import asyncio
import json
from email import message_from_bytes
from email.message import EmailMessage as StdEmailMessage
from email.policy import default as default_policy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from mcp import Client

from protonmail_mcp.bridge import MailboxError, MessageNotFoundError
from protonmail_mcp.models import (
    Attachment,
    EmailContent,
    EmailPage,
    EmailSummary,
    Folder,
    FolderStatus,
)
from protonmail_mcp.parsing import attachment_parts
from protonmail_mcp.policy import (
    Capabilities,
    FilesPolicy,
    IndexPolicy,
    Policy,
    SendPolicy,
)
from protonmail_mcp.send import SendError
from protonmail_mcp.server import (
    build_server,
    server,
    set_client,
    set_smtp_sender,
)


class FakeMailbox:
    def __init__(self) -> None:
        self.config = SimpleNamespace(username="me@proton.me", endpoint="127.0.0.1:1143")
        self.created: list[bytes] = []
        self.replaced: list[int] = []
        self.deleted: list[int] = []
        self.next_uid = 42
        self.draft_uids: set[int] = {5}
        self.flag_changes: list[tuple[list[str], str, tuple[str, ...], tuple[str, ...]]] = []
        self.moves: list[tuple[list[str], str, str]] = []
        self.labels_added: list[tuple[list[str], str, str]] = []
        self.labels_removed: list[tuple[list[str], str]] = []
        self.raw_message: bytes = (
            b"From: a@b.c\nTo: d@e.f\nSubject: s\nMessage-ID: <raw@test>\n\nbody\n"
        )
        self.draft_raw: bytes = b""
        self.thread: list[EmailSummary] = []
        self.attachments: list[Attachment] = []
        self.permanently_deleted: list[tuple[str, str]] = []

    def list_folders(self) -> list[Folder]:
        return [
            Folder(name="INBOX"),
            Folder(name="Archive", flags=["\\Archive"]),
            Folder(name="Trash", flags=["\\Trash"]),
            Folder(name="Drafts", flags=["\\Drafts"]),
            Folder(name="All Mail", flags=["\\All"]),
            Folder(name="Starred", flags=["\\Flagged"]),
            Folder(name="Labels", selectable=False),
            Folder(name="Labels/Important"),
            Folder(name="Labels/To pay"),
            Folder(name="Folders/Finance"),
        ]

    def list_emails(self, **kwargs: Any) -> EmailPage:
        return EmailPage(
            folder=kwargs.get("folder", "INBOX"),
            messages=[
                EmailSummary(
                    message_id="<hello@example.com>",
                    folder="INBOX",
                    uid=1,
                    subject="Bonjour",
                    sender="Alice <alice@example.com>",
                )
            ],
        )

    def get_status(self, folder: str | None = None) -> list[FolderStatus]:
        return [FolderStatus(name=folder or "INBOX", total=10, unread=3)]

    def search_emails(self, query: str, folder: str = "INBOX", limit: int = 20) -> list[EmailSummary]:
        return []

    def get_message(self, message_id: str, folder: str = "INBOX", max_chars: int = 20000) -> EmailContent:
        return EmailContent(
            message_id=message_id,
            folder=folder,
            uid=1,
            subject="Hello",
            sender="Alice <alice@example.com>",
            recipients="bob@example.com",
            body_text="Original body",
            attachments=self.attachments,
        )

    def close(self) -> None:
        pass

    def find_drafts_folder(self) -> str:
        return "Drafts"

    def folder_by_flag(self, flag: str) -> str | None:
        for folder in self.list_folders():
            if flag in folder.flags:
                return folder.name
        return None

    def list_drafts(self, limit: int = 20) -> list[EmailSummary]:
        return [EmailSummary(message_id="<draft-5@test>", folder="Drafts", uid=5, subject="Draft 5")]

    def get_draft(self, uid: int, max_chars: int = 20000) -> EmailContent:
        if uid not in self.draft_uids:
            raise MessageNotFoundError(f"Draft UID {uid} not found")
        return EmailContent(
            message_id=f"<draft-{uid}@test>",
            folder="Drafts",
            uid=uid,
            subject=f"Draft {uid}",
            recipients="alice@example.com",
        )

    def append_to_drafts(self, raw: bytes) -> int:
        uid = self.next_uid
        self.next_uid += 1
        self.draft_uids.add(uid)
        self.created.append(raw)
        return uid

    def replace_draft(self, uid: int, raw: bytes) -> int:
        if uid not in self.draft_uids:
            raise MessageNotFoundError(f"Draft UID {uid} not found")
        self.draft_uids.discard(uid)
        self.replaced.append(uid)
        new_uid = self.next_uid
        self.next_uid += 1
        self.draft_uids.add(new_uid)
        return new_uid

    def delete_draft(self, uid: int) -> None:
        if uid not in self.draft_uids:
            raise MessageNotFoundError(f"Draft UID {uid} not found")
        self.draft_uids.discard(uid)
        self.deleted.append(uid)

    def delete_message_permanently(self, message_id: str, folder: str) -> int:
        self.permanently_deleted.append((message_id, folder))
        return 7

    def set_flags(
        self,
        message_ids: list[str],
        folder: str,
        add: tuple[str, ...] = (),
        remove: tuple[str, ...] = (),
    ) -> tuple[list[str], list[str]]:
        self.flag_changes.append((list(message_ids), folder, tuple(add), tuple(remove)))
        return list(message_ids), []

    def move_messages(
        self, message_ids: list[str], source: str, destination: str
    ) -> tuple[list[str], list[str]]:
        self.moves.append((list(message_ids), source, destination))
        return list(message_ids), []

    def add_label(
        self, message_ids: list[str], folder: str, label: str
    ) -> tuple[list[str], list[str]]:
        self.labels_added.append((list(message_ids), folder, label))
        return list(message_ids), []

    def remove_label(self, message_ids: list[str], label: str) -> tuple[list[str], list[str]]:
        self.labels_removed.append((list(message_ids), label))
        return list(message_ids), []

    def get_draft_raw(
        self, message_id: str, folder: str | None = None
    ) -> tuple[int, bytes]:
        if 5 not in self.draft_uids:
            raise MessageNotFoundError(f"No draft with Message-ID {message_id!r}")
        return 5, self.draft_raw

    def get_raw(self, message_id: str, folder: str = "INBOX") -> bytes:
        return self.raw_message

    def get_attachment_parts(self, message_id: str, folder: str = "INBOX") -> list[Any]:
        return attachment_parts(self.raw_message)

    def get_thread(
        self, message_id: str, folder: str = "INBOX", limit: int = 50
    ) -> list[EmailSummary]:
        return self.thread


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


def organize_server() -> Any:
    policy = Policy(
        mode="organize",
        capabilities=Capabilities(draft=True, organize=True),
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


def test_all_tools_are_read_only_except_local_export() -> None:
    for tool in list_tools():
        assert tool.annotations is not None
        if tool.name in {"save_attachment", "export_email"}:
            assert tool.annotations.destructive_hint in (False, None), tool.name
        else:
            assert tool.annotations.read_only_hint is True, tool.name


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


def test_get_status() -> None:
    cleanup = with_mailbox(FakeMailbox())
    try:
        result = call("get_status", {"folder": "INBOX"})
    finally:
        cleanup()
    assert result.is_error in (False, None)
    assert "INBOX" in result.content[0].text


def test_daily_digest() -> None:
    cleanup = with_mailbox(FakeMailbox())
    try:
        result = call("daily_digest", {"days": 2})
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
    assert tool_payload(committed)["uid"] == 42


def test_reply_draft_flow() -> None:
    built = draft_server()
    mailbox = FakeMailbox()
    cleanup = with_mailbox(mailbox)
    try:
        prepared = tool_payload(
            call_built(built, "prepare_reply_draft", {"message_id": "<m@x>", "body": "Merci"})
        )
        assert prepared["preview"]["reply"]["to"] == "Alice <alice@example.com>"
        assert prepared["preview"]["reply"]["subject"] == "Re: Hello"

        mismatched = call_built(
            built,
            "commit_reply_draft",
            {"token": prepared["token"], "message_id": "<m@x>", "body": "Tampered"},
        )
        assert mismatched.is_error is True
        assert mailbox.created == []

        fresh = tool_payload(
            call_built(built, "prepare_reply_draft", {"message_id": "<m@x>", "body": "Merci"})
        )["token"]
        committed = call_built(
            built,
            "commit_reply_draft",
            {"token": fresh, "message_id": "<m@x>", "body": "Merci"},
        )
    finally:
        cleanup()
    assert committed.is_error in (False, None)
    assert len(mailbox.created) == 1
    assert b"Re: Hello" in mailbox.created[0]


def test_forward_draft_flow() -> None:
    built = draft_server()
    mailbox = FakeMailbox()
    cleanup = with_mailbox(mailbox)
    try:
        invalid = call_built(
            built,
            "prepare_forward_draft",
            {"message_id": "<m@x>", "to": "not-an-email"},
        )
        assert invalid.is_error is True

        prepared = tool_payload(
            call_built(
                built,
                "prepare_forward_draft",
                {"message_id": "<m@x>", "to": "friend@example.com"},
            )
        )
        committed = call_built(
            built,
            "commit_forward_draft",
            {
                "token": prepared["token"],
                "message_id": "<m@x>",
                "to": "friend@example.com",
            },
        )
    finally:
        cleanup()
    assert committed.is_error in (False, None)
    assert len(mailbox.created) == 1
    assert b"Fwd: Hello" in mailbox.created[0]


ORGANIZE_IDS = ["<a@example.com>", "<b@example.com>"]


def test_organize_tools_are_gated_by_capability() -> None:
    read_policy = Policy(
        mode="read",
        capabilities=Capabilities(),
        confirmation_ttl_seconds=300,
        source="test",
    )
    read_tools = {tool.name for tool in asyncio.run(build_server(read_policy).list_tools())}
    assert "commit_move" not in read_tools
    assert "prepare_set_flags" not in read_tools

    organize_tools = {tool.name for tool in asyncio.run(organize_server().list_tools())}
    assert {
        "prepare_set_flags",
        "commit_set_flags",
        "prepare_move",
        "commit_move",
        "prepare_label_change",
        "commit_label_change",
        "prepare_undo_move",
        "commit_undo_move",
    } <= organize_tools


def test_set_flags_flow() -> None:
    built = organize_server()
    mailbox = FakeMailbox()
    cleanup = with_mailbox(mailbox)
    try:
        prepared = tool_payload(
            call_built(
                built,
                "prepare_set_flags",
                {"message_ids": ORGANIZE_IDS, "action": "read"},
            )
        )
        assert prepared["preview"]["folder"] == "INBOX"
        committed = call_built(
            built,
            "commit_set_flags",
            {"token": prepared["token"], "message_ids": ORGANIZE_IDS, "action": "read"},
        )
    finally:
        cleanup()
    assert committed.is_error in (False, None)
    assert mailbox.flag_changes == [(ORGANIZE_IDS, "INBOX", ("\\Seen",), ())]
    assert tool_payload(committed)["updated"] == ORGANIZE_IDS


def test_set_flags_rejects_invalid_action_and_protected_folder() -> None:
    cleanup = with_mailbox(FakeMailbox())
    try:
        bad_action = call_built(
            organize_server(),
            "prepare_set_flags",
            {"message_ids": ORGANIZE_IDS, "action": "burn"},
        )
        protected = call_built(
            organize_server(),
            "prepare_set_flags",
            {"message_ids": ORGANIZE_IDS, "action": "read", "folder": "Drafts"},
        )
    finally:
        cleanup()
    assert bad_action.is_error is True
    assert protected.is_error is True
    assert "protect" in protected.content[0].text


def test_move_flow_and_undo() -> None:
    built = organize_server()
    mailbox = FakeMailbox()
    cleanup = with_mailbox(mailbox)
    try:
        prepared = tool_payload(
            call_built(
                built,
                "prepare_move",
                {"message_ids": ORGANIZE_IDS, "destination": "archive"},
            )
        )
        assert prepared["preview"]["destination"] == "Archive"
        mismatched = call_built(
            built,
            "commit_move",
            {"token": prepared["token"], "message_ids": ORGANIZE_IDS, "destination": "trash"},
        )
        assert mismatched.is_error is True

        fresh = tool_payload(
            call_built(
                built,
                "prepare_move",
                {"message_ids": ORGANIZE_IDS, "destination": "archive"},
            )
        )["token"]
        moved = call_built(
            built,
            "commit_move",
            {"token": fresh, "message_ids": ORGANIZE_IDS, "destination": "archive"},
        )
        assert moved.is_error in (False, None)

        undo_prepared = tool_payload(call_built(built, "prepare_undo_move", {}))
        entry_id = undo_prepared["preview"]["entry"]["id"]
        bad_undo = call_built(
            built,
            "commit_undo_move",
            {"token": undo_prepared["token"], "entry_id": entry_id + 1},
        )
        assert bad_undo.is_error is True

        undo_fresh = tool_payload(call_built(built, "prepare_undo_move", {}))
        undone = call_built(
            built,
            "commit_undo_move",
            {"token": undo_fresh["token"], "entry_id": entry_id},
        )
    finally:
        cleanup()
    assert undone.is_error in (False, None)
    assert mailbox.moves == [
        (ORGANIZE_IDS, "INBOX", "Archive"),
        (ORGANIZE_IDS, "Archive", "INBOX"),
    ]
    assert tool_payload(undone)["destination"] == "INBOX"


def test_move_enforces_cap_and_starred_protection() -> None:
    cleanup = with_mailbox(FakeMailbox())
    try:
        too_many = call_built(
            organize_server(),
            "prepare_move",
            {"message_ids": [f"<m{i}@x>" for i in range(51)], "destination": "archive"},
        )
        starred = call_built(
            organize_server(),
            "prepare_move",
            {"message_ids": ORGANIZE_IDS, "destination": "Starred"},
        )
    finally:
        cleanup()
    assert too_many.is_error is True
    assert "too many messages" in too_many.content[0].text
    assert starred.is_error is True
    assert "flag/unflag" in starred.content[0].text


def test_label_flow_add_and_remove() -> None:
    built = organize_server()
    mailbox = FakeMailbox()
    cleanup = with_mailbox(mailbox)
    try:
        prepared_add = tool_payload(
            call_built(
                built,
                "prepare_label_change",
                {"message_ids": ORGANIZE_IDS, "label": "Labels/To pay"},
            )
        )
        added = call_built(
            built,
            "commit_label_change",
            {
                "token": prepared_add["token"],
                "message_ids": ORGANIZE_IDS,
                "label": "Labels/To pay",
            },
        )
        prepared_remove = tool_payload(
            call_built(
                built,
                "prepare_label_change",
                {"message_ids": ORGANIZE_IDS, "label": "Labels/To pay", "add": False},
            )
        )
        removed = call_built(
            built,
            "commit_label_change",
            {
                "token": prepared_remove["token"],
                "message_ids": ORGANIZE_IDS,
                "label": "Labels/To pay",
                "add": False,
            },
        )
        rejected = call_built(
            organize_server(),
            "prepare_label_change",
            {"message_ids": ORGANIZE_IDS, "label": "Folders/Finance"},
        )
    finally:
        cleanup()
    assert added.is_error in (False, None)
    assert removed.is_error in (False, None)
    assert rejected.is_error is True
    assert mailbox.labels_added == [(ORGANIZE_IDS, "INBOX", "Labels/To pay")]
    assert mailbox.labels_removed == [(ORGANIZE_IDS, "Labels/To pay")]


def test_undo_without_moves_is_an_error() -> None:
    cleanup = with_mailbox(FakeMailbox())
    try:
        result = call_built(organize_server(), "prepare_undo_move", {})
    finally:
        cleanup()
    assert result.is_error is True
    assert "no move to undo" in result.content[0].text


def sandbox_server(tmp_path: Path) -> Any:
    policy = Policy(
        mode="read",
        capabilities=Capabilities(),
        confirmation_ttl_seconds=300,
        source="test",
        files=FilesPolicy(directory=str(tmp_path / "files"), max_bytes=1024),
    )
    return build_server(policy)


def raw_with_attachment(filename: str) -> bytes:
    message = StdEmailMessage()
    message["From"] = "a@b.c"
    message["To"] = "d@e.f"
    message["Subject"] = "s"
    message.set_content("body")
    message.add_attachment(
        b"payload", maintype="application", subtype="octet-stream", filename=filename
    )
    return message.as_bytes()


def test_export_email_writes_into_sandbox(tmp_path: Path) -> None:
    mailbox = FakeMailbox()
    cleanup = with_mailbox(mailbox)
    try:
        first = call_built(
            sandbox_server(tmp_path), "export_email", {"message_id": "<m@x>"}
        )
        second = call_built(
            sandbox_server(tmp_path), "export_email", {"message_id": "<m@x>"}
        )
    finally:
        cleanup()
    assert first.is_error in (False, None)
    payload = tool_payload(first)
    saved = Path(payload["path"])
    assert saved.read_bytes() == mailbox.raw_message
    assert saved.is_relative_to((tmp_path / "files").resolve())
    assert second.is_error is True
    assert "overwrite" in second.content[0].text


def test_save_attachment_contains_traversal(tmp_path: Path) -> None:
    mailbox = FakeMailbox()
    mailbox.raw_message = raw_with_attachment("../../evil.bin")
    cleanup = with_mailbox(mailbox)
    try:
        result = call_built(
            sandbox_server(tmp_path), "save_attachment", {"message_id": "<m@x>"}
        )
    finally:
        cleanup()
    assert result.is_error in (False, None)
    payload = tool_payload(result)
    assert payload["filename"] == "evil.bin"
    saved = Path(payload["path"])
    assert saved.is_relative_to((tmp_path / "files").resolve())
    assert saved.read_bytes() == b"payload"


def test_save_attachment_requires_selection_when_multiple(tmp_path: Path) -> None:
    mailbox = FakeMailbox()
    message = StdEmailMessage()
    message.set_content("body")
    message.add_attachment(b"1", maintype="application", subtype="pdf", filename="a.pdf")
    message.add_attachment(b"2", maintype="application", subtype="pdf", filename="b.pdf")
    mailbox.raw_message = message.as_bytes()
    cleanup = with_mailbox(mailbox)
    try:
        ambiguous = call_built(
            sandbox_server(tmp_path), "save_attachment", {"message_id": "<m@x>"}
        )
        by_name = call_built(
            sandbox_server(tmp_path),
            "save_attachment",
            {"message_id": "<m@x>", "filename": "b.pdf"},
        )
    finally:
        cleanup()
    assert ambiguous.is_error is True
    assert "multiple attachments" in ambiguous.content[0].text
    assert by_name.is_error in (False, None)
    assert Path(tool_payload(by_name)["path"]).read_bytes() == b"2"


def test_list_attachments_metadata(tmp_path: Path) -> None:
    mailbox = FakeMailbox()
    mailbox.attachments = [
        Attachment(filename="x.pdf", content_type="application/pdf", size_bytes=3)
    ]
    cleanup = with_mailbox(mailbox)
    try:
        result = call_built(
            sandbox_server(tmp_path), "list_attachments", {"message_id": "<m@x>"}
        )
    finally:
        cleanup()
    assert result.is_error in (False, None)
    assert "x.pdf" in result.content[0].text


def test_get_thread_tool() -> None:
    mailbox = FakeMailbox()
    mailbox.thread = [
        EmailSummary(message_id="<root@x>", folder="All Mail", uid=1, subject="Root")
    ]
    cleanup = with_mailbox(mailbox)
    try:
        result = call("get_thread", {"message_id": "<root@x>"})
    finally:
        cleanup()
    assert result.is_error in (False, None)
    assert "Root" in result.content[0].text


def test_get_smtp_sender_uses_client_config() -> None:
    from protonmail_mcp import server as server_module

    set_client(FakeMailbox())
    try:
        sender = server_module.get_smtp_sender()
        assert sender is not None
        assert server_module.get_smtp_sender() is sender
    finally:
        set_client(None)
        server_module.set_smtp_sender(None)


def send_server(tmp_path: Path, **overrides: Any) -> Any:
    send_policy = SendPolicy(state_path=str(tmp_path / "state.db"), **overrides)
    policy = Policy(
        mode="send",
        capabilities=Capabilities(draft=True, organize=True, send=True),
        confirmation_ttl_seconds=300,
        source="test",
        send=send_policy,
    )
    return build_server(policy)


def sendable_draft(
    to: str = "me@proton.me",
    bcc: str = "me@proton.me",
    body: str = "Corps",
    extra: dict[str, str] | None = None,
) -> bytes:
    message = StdEmailMessage()
    message["From"] = "me@proton.me"
    message["To"] = to
    if bcc:
        message["Bcc"] = bcc
    message["Subject"] = "Hello"
    message["Message-ID"] = "<draft-send@test>"
    message.set_content(body)
    for key, value in (extra or {}).items():
        message[key] = value
    return message.as_bytes()


class FakeSmtpSender:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[bytes, tuple[str, ...]]] = []
        self.fail = fail

    def send(self, payload: bytes, recipients: Any) -> None:
        if self.fail:
            raise SendError("SMTP submission failed: simulated")
        self.sent.append((payload, tuple(recipients)))


def test_send_draft_flow(tmp_path: Path) -> None:
    built = send_server(tmp_path)
    mailbox = FakeMailbox()
    mailbox.draft_raw = sendable_draft()
    sender = FakeSmtpSender()
    cleanup = with_mailbox(mailbox)
    set_smtp_sender(sender)  # type: ignore[arg-type]
    try:
        prepared = tool_payload(
            call_built(built, "prepare_send_draft", {"message_id": "<draft-send@test>"})
        )
        assert prepared["preview"]["recipients"]["envelope"] == ["me@proton.me"]
        assert prepared["preview"]["recipients"]["external"] == []
        assert prepared["preview"]["quota"]["hour_remaining"] >= 0

        mismatched = call_built(
            built,
            "commit_send_draft",
            {"token": prepared["token"], "message_id": "<other@test>"},
        )
        assert mismatched.is_error is True

        fresh = tool_payload(
            call_built(built, "prepare_send_draft", {"message_id": "<draft-send@test>"})
        )["token"]
        committed = call_built(
            built, "commit_send_draft", {"token": fresh, "message_id": "<draft-send@test>"}
        )
    finally:
        set_smtp_sender(None)
        cleanup()
    assert committed.is_error in (False, None)
    assert len(sender.sent) == 1
    payload, recipients = sender.sent[0]
    assert recipients == ("me@proton.me",)
    assert b"Bcc" not in payload.split(b"\n\n", 1)[0]
    assert mailbox.deleted == [5]
    assert tool_payload(committed)["duplicate"] is False


def test_send_refused_recipient_and_guard(tmp_path: Path) -> None:
    built = send_server(tmp_path)
    mailbox = FakeMailbox()
    cleanup = with_mailbox(mailbox)
    set_smtp_sender(FakeSmtpSender())  # type: ignore[arg-type]
    try:
        mailbox.draft_raw = sendable_draft(to="evil@other.test", bcc="")
        denied = call_built(
            built, "prepare_send_draft", {"message_id": "<draft-send@test>"}
        )
        mailbox.draft_raw = sendable_draft(extra={"Auto-Submitted": "auto-replied"})
        guarded = call_built(
            built, "prepare_send_draft", {"message_id": "<draft-send@test>"}
        )
    finally:
        set_smtp_sender(None)
        cleanup()
    assert denied.is_error is True
    assert "evil@other.test" in denied.content[0].text
    assert guarded.is_error is True
    assert "Auto-Submitted" in guarded.content[0].text


def test_send_quota_enforced(tmp_path: Path) -> None:
    built = send_server(tmp_path, max_per_hour=1, max_per_day=1)
    mailbox = FakeMailbox()
    mailbox.draft_raw = sendable_draft(body="Premier")
    sender = FakeSmtpSender()
    cleanup = with_mailbox(mailbox)
    set_smtp_sender(sender)  # type: ignore[arg-type]
    try:
        token = tool_payload(
            call_built(built, "prepare_send_draft", {"message_id": "<draft-send@test>"})
        )["token"]
        first = call_built(
            built, "commit_send_draft", {"token": token, "message_id": "<draft-send@test>"}
        )
        mailbox.draft_uids.add(5)
        mailbox.draft_raw = sendable_draft(body="Deuxieme")
        second = call_built(
            built, "prepare_send_draft", {"message_id": "<draft-send@test>"}
        )
    finally:
        set_smtp_sender(None)
        cleanup()
    assert first.is_error in (False, None)
    assert second.is_error is True
    assert "quota" in second.content[0].text
    assert len(sender.sent) == 1


def test_send_delete_failure_and_duplicate_retry(tmp_path: Path) -> None:
    built = send_server(tmp_path)
    mailbox = FakeMailbox()
    mailbox.draft_raw = sendable_draft()
    sender = FakeSmtpSender()
    cleanup = with_mailbox(mailbox)
    set_smtp_sender(sender)  # type: ignore[arg-type]

    def failing_delete(uid: int) -> None:
        raise MailboxError("simulated deletion failure")

    mailbox.delete_draft = failing_delete  # type: ignore[method-assign]
    try:
        token = tool_payload(
            call_built(built, "prepare_send_draft", {"message_id": "<draft-send@test>"})
        )["token"]
        sent = call_built(
            built, "commit_send_draft", {"token": token, "message_id": "<draft-send@test>"}
        )
        same_body = call_built(
            built, "prepare_send_draft", {"message_id": "<draft-send@test>"}
        )
        mailbox.draft_raw = sendable_draft(body="Modifie")
        token2 = tool_payload(
            call_built(built, "prepare_send_draft", {"message_id": "<draft-send@test>"})
        )["token"]
        retry = call_built(
            built, "commit_send_draft", {"token": token2, "message_id": "<draft-send@test>"}
        )
    finally:
        set_smtp_sender(None)
        cleanup()
    assert sent.is_error in (False, None)
    assert tool_payload(sent)["warnings"]
    assert same_body.is_error is True
    assert "identical body" in same_body.content[0].text
    assert tool_payload(retry)["duplicate"] is True
    assert len(sender.sent) == 1


def delete_server() -> Any:
    policy = Policy(
        mode="delete",
        capabilities=Capabilities(delete=True),
        confirmation_ttl_seconds=300,
        source="test",
    )
    return build_server(policy)


def test_delete_message_flow() -> None:
    built = delete_server()
    mailbox = FakeMailbox()
    cleanup = with_mailbox(mailbox)
    try:
        prepared = tool_payload(
            call_built(built, "prepare_delete_message", {"message_id": "<m@x>"})
        )
        assert prepared["preview"]["target"]["folder"] == "Trash"
        assert prepared["preview"]["irreversible"] is True
        phrase = prepared["preview"]["confirm_phrase"]
        assert phrase == "permanently delete <m@x>"

        wrong_phrase = call_built(
            built,
            "commit_delete_message",
            {"token": prepared["token"], "message_id": "<m@x>", "confirm": "yes"},
        )
        assert wrong_phrase.is_error is True
        assert mailbox.permanently_deleted == []

        committed = call_built(
            built,
            "commit_delete_message",
            {
                "token": prepared["token"],
                "message_id": "<m@x>",
                "confirm": phrase,
            },
        )
    finally:
        cleanup()
    assert committed.is_error in (False, None)
    assert mailbox.permanently_deleted == [("<m@x>", "Trash")]
    assert tool_payload(committed)["folder"] == "Trash"


def test_delete_message_refuses_outside_trash() -> None:
    cleanup = with_mailbox(FakeMailbox())
    try:
        result = call_built(
            delete_server(),
            "prepare_delete_message",
            {"message_id": "<m@x>", "folder": "INBOX"},
        )
    finally:
        cleanup()
    assert result.is_error is True
    assert "Trash" in result.content[0].text


def test_delete_message_requires_token() -> None:
    built = delete_server()
    mailbox = FakeMailbox()
    cleanup = with_mailbox(mailbox)
    try:
        prepared = tool_payload(
            call_built(built, "prepare_delete_message", {"message_id": "<m@x>"})
        )
        unknown = call_built(
            built,
            "commit_delete_message",
            {
                "token": "bogus",
                "message_id": "<m@x>",
                "confirm": prepared["preview"]["confirm_phrase"],
            },
        )
    finally:
        cleanup()
    assert unknown.is_error is True
    assert mailbox.permanently_deleted == []


def test_delete_message_is_audited(tmp_path: Path) -> None:
    from protonmail_mcp import server as server_module
    from protonmail_mcp.audit import AuditLog

    log_path = tmp_path / "audit.jsonl"
    original = server_module.AUDIT
    server_module.AUDIT = AuditLog(log_path)
    mailbox = FakeMailbox()
    cleanup = with_mailbox(mailbox)
    try:
        prepared = tool_payload(
            call_built(delete_server(), "prepare_delete_message", {"message_id": "<m@x>"})
        )
        call_built(
            delete_server(),
            "commit_delete_message",
            {
                "token": prepared["token"],
                "message_id": "<m@x>",
                "confirm": prepared["preview"]["confirm_phrase"],
            },
        )
    finally:
        cleanup()
        server_module.AUDIT = original
    entries = [
        json.loads(line) for line in log_path.read_text().strip().splitlines()
    ]
    tools = [entry["tool"] for entry in entries]
    assert "prepare_delete_message" in tools
    assert "commit_delete_message" in tools
    assert all(entry["args_digest"] for entry in entries)


def index_server(tmp_path: Path, **overrides: Any) -> Any:
    index_policy = IndexPolicy(enabled=True, path=str(tmp_path / "index.db"), **overrides)
    policy = Policy(
        mode="read",
        capabilities=Capabilities(),
        confirmation_ttl_seconds=300,
        source="test",
        index=index_policy,
    )
    return build_server(policy)


def test_index_tools_sync_and_search(tmp_path: Path) -> None:
    built = index_server(tmp_path)
    cleanup = with_mailbox(FakeMailbox())
    try:
        synced = tool_payload(
            call_built(built, "sync_index", {"folder": "INBOX", "limit": 10})
        )
        hits = tool_payload(call_built(built, "search_index", {"query": "Original"}))
    finally:
        cleanup()
    assert synced["indexed"] == 1
    assert synced["total"] == 1
    assert hits and hits[0]["message_id"] == "<hello@example.com>"


def test_index_tools_skip_excluded_folders(tmp_path: Path) -> None:
    built = index_server(tmp_path, excluded_folders=("INBOX",))
    cleanup = with_mailbox(FakeMailbox())
    try:
        refused = call_built(built, "sync_index", {"folder": "INBOX"})
        synced = tool_payload(call_built(built, "sync_index", {}))
    finally:
        cleanup()
    assert refused.is_error is True
    assert "excluded" in refused.content[0].text
    assert "INBOX" not in synced["folders"]


def test_index_tools_absent_when_disabled() -> None:
    policy = Policy(
        mode="read",
        capabilities=Capabilities(),
        confirmation_ttl_seconds=300,
        source="test",
    )
    names = {tool.name for tool in asyncio.run(build_server(policy).list_tools())}
    assert "sync_index" not in names
    assert "search_index" not in names


def test_profile_mode_narrows_registered_tools(tmp_path: Path) -> None:
    from protonmail_mcp.policy import load_policy

    policy_path = tmp_path / "policy.toml"
    policy_path.write_text(
        '[profiles.w]\nusername = "w@x"\npassword_env = "PW_W"\nmode = "draft"\n'
    )
    policy = load_policy(
        env={
            "PROTONMAIL_MCP_POLICY": str(policy_path),
            "PROTONMAIL_MCP_PROFILE": "w",
        }
    )
    names = {tool.name for tool in asyncio.run(build_server(policy).list_tools())}
    assert "create_draft" in names
    assert "prepare_move" not in names
    assert "prepare_send_draft" not in names
    assert "prepare_delete_message" not in names


def test_resources_are_registered() -> None:
    resources = asyncio.run(server.list_resources())
    names = {resource.name for resource in resources}
    assert {"folders", "status"} <= names
    templates = asyncio.run(server.list_resource_templates())
    template_names = {template.name for template in templates}
    assert {"message", "thread"} <= template_names


def test_resource_folders_and_message() -> None:
    cleanup = with_mailbox(FakeMailbox())
    try:
        folders = asyncio.run(server.read_resource("mail://folders"))
        message = asyncio.run(server.read_resource("mail://message/%3Cm%40x%3E"))

        def text_of(contents: Any) -> str:
            content = contents[0].content
            return content.decode() if isinstance(content, bytes) else content

        assert "INBOX" in text_of(folders)
        assert "<m@x>" in text_of(message)
    finally:
        cleanup()


def test_notifications_lifespan_starts_watcher_and_publishes() -> None:
    from protonmail_mcp.policy import NotificationsPolicy

    started: list[bool] = []
    stopped: list[bool] = []
    callbacks: list[Any] = []

    class FakeWatcher:
        def __init__(self, on_event: Any) -> None:
            self.on_event = on_event

        def start(self) -> None:
            started.append(True)

        def stop(self) -> None:
            stopped.append(True)

    def factory(on_event: Any) -> Any:
        watcher = FakeWatcher(on_event)
        callbacks.append(watcher)
        return watcher

    policy = Policy(
        mode="read",
        capabilities=Capabilities(),
        confirmation_ttl_seconds=300,
        source="test",
        notifications=NotificationsPolicy(enabled=True),
    )
    built = build_server(policy, watcher_factory=factory)
    resources = asyncio.run(built.list_resources())
    assert "inbox" in {resource.name for resource in resources}

    class FakeBus:
        def __init__(self) -> None:
            self.events: list[Any] = []

        async def publish(self, event: Any) -> None:
            self.events.append(event)

    bus = FakeBus()
    built._subscriptions = bus  # type: ignore[assignment]

    async def run_lifespan() -> None:
        low = built._lowlevel_server
        async with low.lifespan(low):
            callbacks[0].on_event()
            await asyncio.sleep(0.05)

    asyncio.run(run_lifespan())
    assert started
    assert stopped
    assert [event.uri for event in bus.events] == ["mail://inbox"]


def test_notifications_resource_absent_when_disabled() -> None:
    resources = asyncio.run(server.list_resources())
    assert "inbox" not in {resource.name for resource in resources}


def test_create_draft_is_idempotent() -> None:
    built = draft_server()
    mailbox = FakeMailbox()
    cleanup = with_mailbox(mailbox)
    args = {"to": "alice@example.com", "subject": "Hello", "body": "Bonjour"}
    try:
        first = tool_payload(call_built(built, "create_draft", dict(args)))
        second = tool_payload(call_built(built, "create_draft", dict(args)))
    finally:
        cleanup()
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert second["uid"] == first["uid"]
    assert len(mailbox.created) == 1


def test_create_draft_recreates_after_manual_delete() -> None:
    built = draft_server()
    mailbox = FakeMailbox()
    cleanup = with_mailbox(mailbox)
    args = {"to": "alice@example.com", "subject": "X", "body": "Y"}
    try:
        first = tool_payload(call_built(built, "create_draft", dict(args)))
        mailbox.draft_uids.discard(first["uid"])
        second = tool_payload(call_built(built, "create_draft", dict(args)))
    finally:
        cleanup()
    assert second["duplicate"] is False
    assert second["uid"] != first["uid"]
    assert len(mailbox.created) == 2


def test_preview_draft_matches_created_draft() -> None:
    built = draft_server()
    mailbox = FakeMailbox()
    cleanup = with_mailbox(mailbox)
    args = {
        "to": "Alice <alice@example.com>",
        "cc": "me@proton.me",
        "subject": "Hello",
        "body": "Bonjour",
    }
    try:
        preview = tool_payload(call_built(built, "preview_draft", dict(args)))
        call_built(built, "create_draft", dict(args))
    finally:
        cleanup()
    assert preview["recipients"] == ["alice@example.com", "me@proton.me"]
    assert preview["external_recipients"] == ["alice@example.com"]
    preview_message = message_from_bytes(preview["raw"].encode(), policy=default_policy)
    created_message = message_from_bytes(mailbox.created[0], policy=default_policy)
    ignored = {"date", "message-id"}
    preview_headers = {
        str(key): str(value)
        for key, value in preview_message.items()
        if key.lower() not in ignored
    }
    created_headers = {
        str(key): str(value)
        for key, value in created_message.items()
        if key.lower() not in ignored
    }
    assert preview_headers == created_headers
    assert preview_message.get_content() == created_message.get_content()
