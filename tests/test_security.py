from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any

from mcp import Client

from protonmail_mcp.bridge import BridgeClient
from protonmail_mcp.config import BridgeConfig
from protonmail_mcp.confirmations import ConfirmationError, ConfirmationManager
from protonmail_mcp.models import EmailContent, EmailPage, Folder, FolderStatus, SearchResult
from protonmail_mcp.policy import Capabilities, Policy
from protonmail_mcp.server import build_server, set_client

READ_ONLY_TOOLS = {
    "list_folders",
    "list_emails",
    "search_emails",
    "read_email",
    "get_status",
    "daily_digest",
    "get_thread",
    "list_attachments",
}
LOCAL_WRITE_TOOLS = {"save_attachment", "export_email"}
ALWAYS_TOOLS = READ_ONLY_TOOLS | LOCAL_WRITE_TOOLS
DRAFT_TOOLS = {
    "list_drafts",
    "create_draft",
    "preview_draft",
    "prepare_update_draft",
    "commit_update_draft",
    "prepare_delete_draft",
    "commit_delete_draft",
    "prepare_reply_draft",
    "commit_reply_draft",
    "prepare_forward_draft",
    "commit_forward_draft",
}

HEADER = b"From: a@b.c\nTo: d@e.f\nSubject: x\nMessage-ID: <h@x>\n\n"


ORGANIZE_TOOLS = {
    "prepare_set_flags",
    "commit_set_flags",
    "prepare_move",
    "commit_move",
    "prepare_label_change",
    "commit_label_change",
    "prepare_undo_move",
    "commit_undo_move",
}
SEND_TOOLS = {"prepare_send_draft", "commit_send_draft"}
DELETE_TOOLS = {"prepare_delete_message", "commit_delete_message"}


def tools_of(capabilities: Capabilities) -> dict[str, Any]:
    policy = Policy(
        mode="custom",
        capabilities=capabilities,
        confirmation_ttl_seconds=300,
        source="test",
    )
    return {tool.name: tool for tool in asyncio.run(build_server(policy).list_tools())}


def test_capability_matrix_exposes_exact_tools() -> None:
    cases = {
        "read": (Capabilities(), ALWAYS_TOOLS),
        "draft": (Capabilities(draft=True), ALWAYS_TOOLS | DRAFT_TOOLS),
        "organize": (
            Capabilities(draft=True, organize=True),
            ALWAYS_TOOLS | DRAFT_TOOLS | ORGANIZE_TOOLS,
        ),
        "send": (
            Capabilities(draft=True, organize=True, send=True),
            ALWAYS_TOOLS | DRAFT_TOOLS | ORGANIZE_TOOLS | SEND_TOOLS,
        ),
        "delete": (
            Capabilities(True, True, True, True),
            ALWAYS_TOOLS | DRAFT_TOOLS | ORGANIZE_TOOLS | SEND_TOOLS | DELETE_TOOLS,
        ),
        "organize_only": (Capabilities(organize=True), ALWAYS_TOOLS | ORGANIZE_TOOLS),
        "send_only": (Capabilities(send=True), ALWAYS_TOOLS | SEND_TOOLS),
        "delete_only": (Capabilities(delete=True), ALWAYS_TOOLS | DELETE_TOOLS),
    }
    for name, (capabilities, expected) in cases.items():
        assert set(tools_of(capabilities)) == expected, name


def test_commit_tools_require_token_and_are_destructive() -> None:
    tools = tools_of(Capabilities(True, True, True, True))
    for name, tool in tools.items():
        if name.startswith("commit_"):
            assert "token" in tool.input_schema.get("properties", {}), name
            assert "token" in tool.input_schema.get("required", []), name
            assert tool.annotations is not None
            assert tool.annotations.destructive_hint is True, name
        if name.startswith("prepare_"):
            assert tool.annotations is not None
            assert tool.annotations.destructive_hint in (False, None), name


def test_prepare_commit_pairs_are_complete() -> None:
    names = set(tools_of(Capabilities(True, True, True, True)))
    commits = {name for name in names if name.startswith("commit_")}
    prepares = {name for name in names if name.startswith("prepare_")}
    assert commits
    assert {name.replace("commit_", "prepare_") for name in commits} == prepares


def test_read_tools_are_annotated_read_only() -> None:
    tools = tools_of(Capabilities(True, True, True, True))
    for name in READ_ONLY_TOOLS | {"list_drafts"}:
        assert tools[name].annotations is not None
        assert tools[name].annotations.read_only_hint is True, name


def test_local_write_tools_are_not_destructive() -> None:
    tools = tools_of(Capabilities(True, True, True, True))
    for name in LOCAL_WRITE_TOOLS:
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.read_only_hint is False
        assert annotations.destructive_hint in (False, None), name


def test_tokens_have_sufficient_entropy() -> None:
    manager = ConfirmationManager(max_pending=32)
    tokens = [manager.prepare("action", {"i": index}).token for index in range(20)]
    assert len(set(tokens)) == 20
    assert all(len(token) >= 32 for token in tokens)


def test_concurrent_commit_exactly_one_wins() -> None:
    manager = ConfirmationManager()
    prepared = manager.prepare("action", {"a": 1})
    results: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            manager.commit(prepared.token, {"a": 1})
            outcome = "ok"
        except ConfirmationError:
            outcome = "rejected"
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count("ok") == 1
    assert results.count("rejected") == 4


class SearchRecorder:
    def __init__(self) -> None:
        self.selected: tuple[str, bool] | None = None
        self.criteria: Any = None

    def select_folder(self, folder: str, readonly: bool = False) -> dict[str, Any]:
        self.selected = (folder, readonly)
        return {}

    def search(self, criteria: Any) -> list[int]:
        self.criteria = criteria
        return [1]

    def fetch(self, uids: list[int], data: list[str]) -> dict[int, dict[bytes, Any]]:
        return {
            1: {
                b"FLAGS": (),
                b"RFC822.SIZE": len(HEADER),
                b"RFC822.HEADER": HEADER,
                b"BODY[]": HEADER + b"body\n",
            }
        }

    def logout(self) -> None:
        pass


def _recording_client() -> tuple[BridgeClient, SearchRecorder]:
    recorder = SearchRecorder()
    client = BridgeClient(
        BridgeConfig(
            host="127.0.0.1",
            imap_port=1143,
            smtp_port=1025,
            username="me@proton.me",
            password="secret",
            timeout=5.0,
            verify_tls=False,
        )
    )
    client._connect = lambda: recorder  # type: ignore[method-assign]
    return client, recorder


def test_hostile_message_id_stays_one_search_argument() -> None:
    client, recorder = _recording_client()
    hostile = '"\r\nA1 DELETE INBOX\r\n'
    client.get_message(hostile)
    assert recorder.criteria == ["HEADER", "Message-ID", hostile]
    assert recorder.selected == ("INBOX", True)


def test_hostile_search_query_stays_one_argument() -> None:
    client, recorder = _recording_client()
    hostile = 'x"\r\nA1 STORE 1 +FLAGS (\\Deleted)\r\n'
    client.search_emails(hostile)
    assert recorder.criteria == ["TEXT", hostile]


class StrictReadMailbox:
    def __init__(self) -> None:
        self.config = SimpleNamespace(username="me@proton.me", endpoint="127.0.0.1:1143")

    def list_folders(self) -> list[Folder]:
        return [Folder(name="INBOX")]

    def list_emails(self, **kwargs: Any) -> EmailPage:
        return EmailPage(folder="INBOX")

    def get_status(self, folder: str | None = None) -> list[FolderStatus]:
        return [FolderStatus(name="INBOX")]

    def search_emails(self, *args: Any, **kwargs: Any) -> SearchResult:
        return SearchResult()

    def get_message(self, message_id: str, folder: str = "INBOX", max_chars: int = 20000) -> EmailContent:
        return EmailContent(message_id=message_id, folder=folder, uid=1)

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"read-mode tool attempted mutation: {name}")


def test_read_tools_never_call_mutations() -> None:
    mailbox = StrictReadMailbox()
    set_client(mailbox)  # type: ignore[arg-type]
    try:
        policy = Policy(
            mode="read",
            capabilities=Capabilities(),
            confirmation_ttl_seconds=300,
            source="test",
        )
        built = build_server(policy)

        async def run() -> list[Any]:
            async with Client(built) as client:
                return [
                    await client.call_tool("list_folders", {}),
                    await client.call_tool("list_emails", {}),
                    await client.call_tool("search_emails", {"query": "x"}),
                    await client.call_tool("read_email", {"message_id": "<x@y>"}),
                ]

        results = asyncio.run(run())
    finally:
        set_client(None)
    for result in results:
        assert result.is_error in (False, None)
