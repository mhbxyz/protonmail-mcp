from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pytest
from imapclient.exceptions import IMAPClientAbortError, IMAPClientError

from protonmail_mcp.bridge import (
    MAX_MESSAGE_BYTES,
    BridgeClient,
    MailboxError,
    MessageNotFoundError,
)
from protonmail_mcp.config import BridgeConfig

HEADER = b"""From: Alice <alice@example.com>
To: bob@proton.me
Subject: Hello
Date: Fri, 03 Oct 2026 10:00:00 +0200
Message-ID: <hello@example.com>

"""


def make_config() -> BridgeConfig:
    return BridgeConfig(
        host="127.0.0.1",
        imap_port=1143,
        smtp_port=1025,
        username="me@proton.me",
        password="secret",
        timeout=5.0,
        verify_tls=False,
    )


class FakeIMAPClient:
    def __init__(
        self,
        folders: list[tuple[Any, Any, str]] | None = None,
        search_results: list[int] | None = None,
        fetch_results: dict[int, dict[bytes, Any]] | None = None,
    ) -> None:
        self.folders = folders or []
        self.search_results = search_results or []
        self.fetch_results = fetch_results or {}
        self.selected: tuple[str, bool] | None = None
        self.last_criteria: Any = None
        self.logged_out = False
        self.appended: list[tuple[str, bytes, tuple[Any, ...], Any]] = []
        self.append_response: Any = b"[APPENDUID 1 7] APPEND"
        self.added_flags: list[tuple[list[int], list[str]]] = []
        self.expunged: list[list[int] | None] = []

    def list_folders(self) -> list[tuple[Any, Any, str]]:
        return self.folders

    def select_folder(self, folder: str, readonly: bool = False) -> dict[str, Any]:
        self.selected = (folder, readonly)
        return {}

    def search(self, criteria: Any) -> list[int]:
        self.last_criteria = criteria
        return self.search_results

    def fetch(self, uids: list[int], data: list[str]) -> dict[int, dict[bytes, Any]]:
        return {uid: self.fetch_results[uid] for uid in uids if uid in self.fetch_results}

    def logout(self) -> None:
        self.logged_out = True

    def append(self, folder: str, msg: bytes, flags: tuple[Any, ...] = (), msg_time: Any = None) -> Any:
        self.appended.append((folder, bytes(msg), tuple(flags), msg_time))
        return self.append_response

    def add_flags(self, messages: list[int], flags: list[str], silent: bool = False) -> None:
        self.added_flags.append((list(messages), list(flags)))

    def expunge(self, messages: list[int] | None = None) -> tuple[()]:
        self.expunged.append(list(messages) if messages else None)
        return ()


def attach(client: BridgeClient, fake: Any) -> BridgeClient:
    client._connect = lambda: fake  # type: ignore[method-assign]
    return client


def test_list_folders_parses_and_sorts() -> None:
    fake = FakeIMAPClient(
        folders=[
            ((b"\\HasNoChildren",), b"/", "Projets"),
            ((b"\\HasNoChildren",), b"/", "INBOX"),
            ((b"\\Noselect",), b"/", "Poubelle"),
        ]
    )
    client = attach(BridgeClient(make_config()), fake)
    folders = client.list_folders()
    assert [folder.name for folder in folders] == ["INBOX", "Poubelle", "Projets"]
    assert folders[0].selectable is True
    assert folders[1].selectable is False


def test_list_emails_uses_readonly_and_newest_first() -> None:
    fake = FakeIMAPClient(
        search_results=[1, 2, 3],
        fetch_results={
            1: {
                b"FLAGS": (b"\\Seen",),
                b"RFC822.SIZE": 100,
                b"RFC822.HEADER": HEADER,
                b"INTERNALDATE": datetime(2026, 10, 5, 9, 0),
            },
            2: {
                b"FLAGS": (),
                b"RFC822.SIZE": 50,
                b"RFC822.HEADER": HEADER,
                b"INTERNALDATE": datetime(2026, 10, 4, 9, 0),
            },
        },
    )
    client = attach(BridgeClient(make_config()), fake)
    emails = client.list_emails(limit=2)
    assert [email.uid for email in emails] == [1, 2]
    assert fake.selected == ("INBOX", True)
    assert emails[0].unread is False
    assert emails[1].unread is True
    assert emails[0].received == "2026-10-05 09:00:00"


def test_list_emails_sorts_by_receive_date() -> None:
    fake = FakeIMAPClient(
        search_results=[1, 2],
        fetch_results={
            1: {b"RFC822.HEADER": HEADER, b"INTERNALDATE": datetime(2026, 10, 1, 9, 0)},
            2: {b"RFC822.HEADER": HEADER, b"INTERNALDATE": datetime(2026, 10, 3, 9, 0)},
        },
    )
    emails = attach(BridgeClient(make_config()), fake).list_emails(limit=2)
    assert [email.uid for email in emails] == [2, 1]


def test_list_criteria() -> None:
    criteria = BridgeClient._list_criteria(True, 7, "alice@example.com", "facture")
    assert criteria[0] == "UNSEEN"
    assert criteria[1] == "SINCE"
    assert criteria[2] == date.today() - timedelta(days=7)
    assert criteria[3:] == ["FROM", "alice@example.com", "SUBJECT", "facture"]
    assert BridgeClient._list_criteria(False, None, None, None) == ["ALL"]


def test_get_message_not_found() -> None:
    fake = FakeIMAPClient(search_results=[])
    client = attach(BridgeClient(make_config()), fake)
    with pytest.raises(MessageNotFoundError):
        client.get_message("<missing@example.com>")


def test_get_message_returns_full_content() -> None:
    fake = FakeIMAPClient(
        search_results=[9],
        fetch_results={
            9: {
                b"FLAGS": (b"\\Flagged",),
                b"RFC822.SIZE": 321,
                b"BODY[]": HEADER + b"Corps du message.\n",
            }
        },
    )
    client = attach(BridgeClient(make_config()), fake)
    email = client.get_message("<hello@example.com>")
    assert email.uid == 9
    assert email.subject == "Hello"
    assert "Corps du message." in email.body_text
    assert email.flagged is True
    assert email.message_id == "<hello@example.com>"


def test_reconnects_after_abort() -> None:
    good = FakeIMAPClient(folders=[((b"\\HasNoChildren",), b"/", "INBOX")])

    class AbortingClient(FakeIMAPClient):
        def list_folders(self) -> list[tuple[Any, Any, str]]:
            raise IMAPClientAbortError("connection lost")

    connections = [AbortingClient(), good]
    client = BridgeClient(make_config())
    client._connect = lambda: connections.pop(0)  # type: ignore[method-assign]
    folders = client.list_folders()
    assert [folder.name for folder in folders] == ["INBOX"]


def test_protocol_error_becomes_mailbox_error() -> None:
    class BrokenClient(FakeIMAPClient):
        def list_folders(self) -> list[tuple[Any, Any, str]]:
            raise IMAPClientError("NO")

    client = attach(BridgeClient(make_config()), BrokenClient())
    with pytest.raises(MailboxError):
        client.list_folders()


DRAFTS_FOLDERS = [
    ((b"\\Drafts", b"\\Marked"), b"/", "Brouillons"),
    ((b"\\HasNoChildren",), b"/", "INBOX"),
]


def test_find_drafts_folder_by_special_use_flag() -> None:
    client = attach(BridgeClient(make_config()), FakeIMAPClient(folders=DRAFTS_FOLDERS))
    assert client.find_drafts_folder() == "Brouillons"


def test_find_drafts_folder_fallback() -> None:
    folders = [((b"\\HasNoChildren",), b"/", "INBOX")]
    client = attach(BridgeClient(make_config()), FakeIMAPClient(folders=folders))
    assert client.find_drafts_folder() == "Drafts"


def test_append_to_drafts_uses_appenduid() -> None:
    fake = FakeIMAPClient(folders=DRAFTS_FOLDERS)
    fake.append_response = b"[APPENDUID 115958431 42] APPEND"
    client = attach(BridgeClient(make_config()), fake)
    uid = client.append_to_drafts(b"raw message")
    assert uid == 42
    folder, raw, flags, _ = fake.appended[0]
    assert folder == "Brouillons"
    assert raw == b"raw message"
    assert "\\Draft" in flags


def test_append_to_drafts_without_appenduid_raises() -> None:
    fake = FakeIMAPClient(folders=DRAFTS_FOLDERS)
    fake.append_response = b"APPEND"
    client = attach(BridgeClient(make_config()), fake)
    with pytest.raises(MailboxError, match="APPENDUID"):
        client.append_to_drafts(b"x")


def test_delete_draft_targets_only_requested_uid() -> None:
    fake = FakeIMAPClient(folders=DRAFTS_FOLDERS, fetch_results={5: {b"FLAGS": ()}})
    client = attach(BridgeClient(make_config()), fake)
    client.delete_draft(5)
    assert fake.selected == ("Brouillons", False)
    assert fake.added_flags == [([5], ["\\Deleted"])]
    assert fake.expunged == [[5]]


def test_delete_draft_missing_uid_raises() -> None:
    fake = FakeIMAPClient(folders=DRAFTS_FOLDERS)
    client = attach(BridgeClient(make_config()), fake)
    with pytest.raises(MessageNotFoundError):
        client.delete_draft(99)


def test_replace_draft_appends_then_deletes_old() -> None:
    fake = FakeIMAPClient(folders=DRAFTS_FOLDERS, fetch_results={5: {b"FLAGS": ()}})
    fake.append_response = b"[APPENDUID 1 7] APPEND"
    client = attach(BridgeClient(make_config()), fake)
    new_uid = client.replace_draft(5, b"raw")
    assert new_uid == 7
    assert fake.appended[0][0] == "Brouillons"
    assert fake.added_flags == [([5], ["\\Deleted"])]
    assert fake.expunged == [[5]]


def test_get_draft_returns_content() -> None:
    fake = FakeIMAPClient(
        folders=DRAFTS_FOLDERS,
        fetch_results={
            5: {
                b"FLAGS": (),
                b"RFC822.SIZE": 10,
                b"BODY[]": HEADER + b"Corps du brouillon.\n",
            }
        },
    )
    client = attach(BridgeClient(make_config()), fake)
    draft = client.get_draft(5)
    assert draft.uid == 5
    assert draft.folder == "Brouillons"
    assert "Corps du brouillon." in draft.body_text


def test_list_drafts_uses_drafts_folder() -> None:
    fake = FakeIMAPClient(
        folders=DRAFTS_FOLDERS,
        search_results=[1],
        fetch_results={
            1: {b"FLAGS": (b"\\Draft",), b"RFC822.SIZE": 10, b"RFC822.HEADER": HEADER}
        },
    )
    client = attach(BridgeClient(make_config()), fake)
    drafts = client.list_drafts()
    assert [draft.uid for draft in drafts] == [1]
    assert fake.selected == ("Brouillons", True)


def test_get_message_rejects_oversized_message() -> None:
    fake = FakeIMAPClient(
        search_results=[9],
        fetch_results={
            9: {b"FLAGS": (), b"RFC822.SIZE": MAX_MESSAGE_BYTES + 1, b"BODY[]": b""}
        },
    )
    client = attach(BridgeClient(make_config()), fake)
    with pytest.raises(MailboxError, match="too large"):
        client.get_message("<hello@example.com>")


def test_get_draft_rejects_oversized_draft() -> None:
    fake = FakeIMAPClient(
        folders=DRAFTS_FOLDERS,
        fetch_results={
            5: {b"FLAGS": (), b"RFC822.SIZE": MAX_MESSAGE_BYTES + 1, b"BODY[]": b""}
        },
    )
    client = attach(BridgeClient(make_config()), fake)
    with pytest.raises(MailboxError, match="too large"):
        client.get_draft(5)
