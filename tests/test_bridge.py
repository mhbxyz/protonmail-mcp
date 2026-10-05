from __future__ import annotations

from datetime import date, datetime, timedelta
from email import message_from_bytes
from email.policy import default as default_policy
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
        search_queue: list[list[int]] | None = None,
    ) -> None:
        self.folders = folders or []
        self.search_results = search_results or []
        self.fetch_results = fetch_results or {}
        self.search_queue = list(search_queue) if search_queue is not None else None
        self.selected: tuple[str, bool] | None = None
        self.last_criteria: Any = None
        self.logged_out = False
        self.appended: list[tuple[str, bytes, tuple[Any, ...], Any]] = []
        self.append_response: Any = b"[APPENDUID 1 7] APPEND"
        self.added_flags: list[tuple[list[int], list[str]]] = []
        self.removed_flags: list[tuple[list[int], list[str]]] = []
        self.expunged: list[list[int] | None] = []
        self.moves: list[tuple[list[int], str]] = []
        self.copies: list[tuple[list[int], str]] = []

    def list_folders(self) -> list[tuple[Any, Any, str]]:
        return self.folders

    def select_folder(self, folder: str, readonly: bool = False) -> dict[str, Any]:
        self.selected = (folder, readonly)
        return {}

    def search(self, criteria: Any) -> list[int]:
        self.last_criteria = criteria
        if self.search_queue is not None:
            return self.search_queue.pop(0) if self.search_queue else []
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

    def remove_flags(self, messages: list[int], flags: list[str], silent: bool = False) -> None:
        self.removed_flags.append((list(messages), list(flags)))

    def move(self, messages: list[int], folder: str) -> dict[Any, Any]:
        self.moves.append((list(messages), folder))
        return {}

    def copy(self, messages: list[int], folder: str) -> dict[Any, Any]:
        self.copies.append((list(messages), folder))
        return {}

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
    page = client.list_emails(limit=2)
    emails = page.messages
    assert [email.uid for email in emails] == [1, 2]
    assert page.folder == "INBOX"
    assert page.next_cursor == "2"
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
    emails = attach(BridgeClient(make_config()), fake).list_emails(limit=2).messages
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


def test_folder_by_flag() -> None:
    client = attach(BridgeClient(make_config()), FakeIMAPClient(folders=DRAFTS_FOLDERS))
    assert client.folder_by_flag("\\Drafts") == "Brouillons"
    assert client.folder_by_flag("\\Trash") is None


def test_set_flags_adds_and_removes_by_message_id() -> None:
    fake = FakeIMAPClient(search_results=[3], fetch_results={3: {b"FLAGS": ()}})
    client = attach(BridgeClient(make_config()), fake)
    updated, missing = client.set_flags(
        ["<m@x>"], "INBOX", add=["\\Seen"], remove=["\\Flagged"]
    )
    assert updated == ["<m@x>"]
    assert missing == []
    assert fake.added_flags == [([3], ["\\Seen"])]
    assert fake.removed_flags == [([3], ["\\Flagged"])]


def test_set_flags_reports_missing() -> None:
    client = attach(BridgeClient(make_config()), FakeIMAPClient(search_results=[]))
    updated, missing = client.set_flags(["<gone@x>"], "INBOX", add=["\\Seen"])
    assert updated == []
    assert missing == ["<gone@x>"]


def test_move_messages() -> None:
    fake = FakeIMAPClient(search_results=[4])
    client = attach(BridgeClient(make_config()), fake)
    updated, missing = client.move_messages(["<m@x>"], "INBOX", "Archive")
    assert updated == ["<m@x>"]
    assert missing == []
    assert fake.moves == [([4], "Archive")]


def test_add_label_copies_and_skips_existing() -> None:
    fake = FakeIMAPClient(search_queue=[[], [4]])
    client = attach(BridgeClient(make_config()), fake)
    updated, missing = client.add_label(["<m@x>"], "INBOX", "Labels/To pay")
    assert updated == ["<m@x>"]
    assert missing == []
    assert fake.copies == [([4], "Labels/To pay")]

    existing = FakeIMAPClient(search_queue=[[7]])
    client2 = attach(BridgeClient(make_config()), existing)
    updated2, missing2 = client2.add_label(["<m@x>"], "INBOX", "Labels/To pay")
    assert updated2 == ["<m@x>"]
    assert missing2 == []
    assert existing.copies == []


def test_add_label_reports_missing() -> None:
    fake = FakeIMAPClient(search_queue=[[], []])
    client = attach(BridgeClient(make_config()), fake)
    updated, missing = client.add_label(["<gone@x>"], "INBOX", "Labels/To pay")
    assert updated == []
    assert missing == ["<gone@x>"]


def test_remove_label_deletes_the_label_entry() -> None:
    fake = FakeIMAPClient(search_queue=[[9]])
    client = attach(BridgeClient(make_config()), fake)
    updated, missing = client.remove_label(["<m@x>"], "Labels/To pay")
    assert updated == ["<m@x>"]
    assert missing == []
    assert fake.added_flags == [([9], ["\\Deleted"])]
    assert fake.expunged == [[9]]


def test_remove_label_reports_missing() -> None:
    fake = FakeIMAPClient(search_queue=[[]])
    client = attach(BridgeClient(make_config()), fake)
    updated, missing = client.remove_label(["<m@x>"], "Labels/To pay")
    assert updated == []
    assert missing == ["<m@x>"]


def test_list_emails_pagination_cursor() -> None:
    fake = FakeIMAPClient(
        search_results=[1, 2, 3, 4],
        fetch_results={
            uid: {
                b"FLAGS": (),
                b"RFC822.SIZE": 10,
                b"RFC822.HEADER": HEADER,
                b"INTERNALDATE": datetime(2026, 10, 10 - uid, 9, 0),
            }
            for uid in (1, 2, 3, 4)
        },
    )
    client = attach(BridgeClient(make_config()), fake)
    first = client.list_emails(limit=2)
    assert [message.uid for message in first.messages] == [1, 2]
    assert first.next_cursor == "2"
    second = client.list_emails(limit=2, before="2")
    assert [message.uid for message in second.messages] == [3, 4]
    assert second.next_cursor is None


def test_list_emails_rejects_invalid_cursor() -> None:
    fake = FakeIMAPClient(search_results=[1, 2])
    client = attach(BridgeClient(make_config()), fake)
    with pytest.raises(MailboxError, match="cursor"):
        client.list_emails(before="99")
    with pytest.raises(MailboxError, match="invalid pagination cursor"):
        client.list_emails(before="abc")


def test_get_status_single_folder() -> None:
    class StatusFake(FakeIMAPClient):
        def folder_status(self, folder: str, what: Any = None) -> dict[bytes, int]:
            return {b"MESSAGES": 12, b"UNSEEN": 3}

    client = attach(BridgeClient(make_config()), StatusFake())
    statuses = client.get_status("INBOX")
    assert statuses[0].name == "INBOX"
    assert statuses[0].total == 12
    assert statuses[0].unread == 3


def test_get_status_all_folders_skips_unselectable() -> None:
    class StatusFake(FakeIMAPClient):
        def folder_status(self, folder: str, what: Any = None) -> dict[bytes, int]:
            return {b"MESSAGES": 1, b"UNSEEN": 0}

    folders = [
        ((b"\\HasNoChildren",), b"/", "INBOX"),
        ((b"\\Noselect",), b"/", "Folders"),
    ]
    client = attach(BridgeClient(make_config()), StatusFake(folders=folders))
    statuses = client.get_status()
    assert [status.name for status in statuses] == ["INBOX"]


def test_search_criteria_combination() -> None:
    criteria = BridgeClient._search_criteria(
        "invoice", True, 7, 2, "a@b.c", "d@e.f", "facture"
    )
    assert criteria == [
        "UNSEEN",
        "TEXT",
        "invoice",
        "FROM",
        "a@b.c",
        "TO",
        "d@e.f",
        "SUBJECT",
        "facture",
        "SINCE",
        date.today() - timedelta(days=7),
        "BEFORE",
        date.today() - timedelta(days=2),
    ]
    assert BridgeClient._search_criteria("", False, None, None, None, None, None) == ["ALL"]


def test_get_raw_returns_full_message() -> None:
    fake = FakeIMAPClient(
        search_results=[9],
        fetch_results={9: {b"RFC822.SIZE": 10, b"BODY[]": HEADER + b"Corps.\n"}},
    )
    client = attach(BridgeClient(make_config()), fake)
    assert client.get_raw("<hello@example.com>") == HEADER + b"Corps.\n"


class ThreadFake:
    def __init__(self, messages: dict[int, bytes]) -> None:
        self.messages = messages
        self.selected: str | None = None

    def list_folders(self) -> list[tuple[Any, Any, str]]:
        return []

    def select_folder(self, folder: str, readonly: bool = False) -> dict[str, Any]:
        self.selected = folder
        return {}

    def search(self, criteria: Any) -> list[int]:
        field, value = criteria[1], criteria[2]
        found: list[int] = []
        for uid, header in self.messages.items():
            parsed = message_from_bytes(header, policy=default_policy)
            if value in str(parsed.get(field, "")):
                found.append(uid)
        return found

    def fetch(self, uids: list[int], data: list[str]) -> dict[int, dict[bytes, Any]]:
        return {
            uid: {
                b"RFC822.HEADER": self.messages[uid],
                b"RFC822.SIZE": len(self.messages[uid]),
                b"FLAGS": (),
                b"INTERNALDATE": datetime(2026, 1, 1, 0, uid),
            }
            for uid in uids
            if uid in self.messages
        }

    def logout(self) -> None:
        pass


def test_get_thread_reconstructs_conversation() -> None:
    messages = {
        1: b"Message-ID: <root@x>\nSubject: Root\nFrom: a@x\n\n",
        2: (
            b"Message-ID: <mid@x>\nIn-Reply-To: <root@x>\nReferences: <root@x>\n"
            b"Subject: Re: Root\nFrom: b@x\n\n"
        ),
        3: (
            b"Message-ID: <leaf@x>\nIn-Reply-To: <mid@x>\nReferences: <root@x> <mid@x>\n"
            b"Subject: Re: Root\nFrom: a@x\n\n"
        ),
    }
    client = BridgeClient(make_config())
    client._connect = lambda: ThreadFake(messages)  # type: ignore[method-assign]
    thread = client.get_thread("<mid@x>")
    assert [message.message_id for message in thread] == ["<root@x>", "<mid@x>", "<leaf@x>"]
    assert [message.uid for message in thread] == [1, 2, 3]


def test_get_thread_missing_message_returns_empty() -> None:
    client = BridgeClient(make_config())
    client._connect = lambda: ThreadFake({})  # type: ignore[method-assign]
    assert client.get_thread("<missing@x>") == []
