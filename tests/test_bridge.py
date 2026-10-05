from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pytest
from imapclient.exceptions import IMAPClientAbortError, IMAPClientError

from protonmail_mcp.bridge import BridgeClient, MailboxError, MessageNotFoundError
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
