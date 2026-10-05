from __future__ import annotations

from pathlib import Path
from typing import Any

from protonmail_mcp.index import MessageIndex
from protonmail_mcp.models import EmailContent, EmailPage, EmailSummary


class StubClient:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.messages = messages

    def list_emails(
        self, folder: str = "INBOX", limit: int = 20, **kwargs: Any
    ) -> EmailPage:
        selected = [item for item in self.messages if item["folder"] == folder]
        return EmailPage(
            folder=folder,
            messages=[
                EmailSummary(
                    message_id=item["message_id"],
                    folder=folder,
                    uid=index + 1,
                    subject=item.get("subject", ""),
                    sender=item.get("sender", ""),
                    received=item.get("received", ""),
                    unread=item.get("unread", True),
                )
                for index, item in enumerate(selected[:limit])
            ],
        )

    def get_message(
        self, message_id: str, folder: str = "INBOX", max_chars: int = 20000
    ) -> EmailContent:
        for item in self.messages:
            if item["message_id"] == message_id:
                return EmailContent(
                    message_id=message_id,
                    folder=folder,
                    uid=1,
                    subject=item.get("subject", ""),
                    sender=item.get("sender", ""),
                    body_text=item.get("body", ""),
                )
        raise AssertionError(f"unknown message {message_id}")


def sample_messages() -> list[dict[str, Any]]:
    return [
        {
            "message_id": "<a@x>",
            "folder": "INBOX",
            "subject": "Facture octobre",
            "sender": "Alice <alice@x>",
            "body": "Merci de régler la facture avant vendredi.\n",
            "received": "2026-10-04 09:00:00",
            "unread": True,
        },
        {
            "message_id": "<b@x>",
            "folder": "INBOX",
            "subject": "Newsletter",
            "sender": "Bob <bob@x>",
            "body": "Les nouveautés du mois avec un code promo.\n",
            "received": "2026-10-03 09:00:00",
            "unread": False,
        },
        {
            "message_id": "<c@x>",
            "folder": "Archive",
            "subject": "Ancien fil",
            "sender": "Carol <carol@x>",
            "body": "Historique de la facture de septembre.\n",
            "received": "2026-09-01 09:00:00",
            "unread": False,
        },
    ]


def make_index(tmp_path: Path, max_body_chars: int = 10000) -> MessageIndex:
    return MessageIndex(tmp_path / "index.db", max_body_chars)


def test_sync_and_search(tmp_path: Path) -> None:
    index = make_index(tmp_path)
    client = StubClient(sample_messages())
    assert index.sync_folder(client, "INBOX", 10) == 2
    assert index.count() == 2

    hits = index.search("facture", None, 10)
    assert {hit.message_id for hit in hits} == {"<a@x>"}
    assert hits[0].folder == "INBOX"
    assert hits[0].unread is True
    assert "[" in hits[0].snippet and "]" in hits[0].snippet

    sender_hits = index.search("Alice", None, 10)
    assert [hit.message_id for hit in sender_hits] == ["<a@x>"]
    index.close()


def test_resync_is_idempotent(tmp_path: Path) -> None:
    index = make_index(tmp_path)
    client = StubClient(sample_messages())
    index.sync_folder(client, "INBOX", 10)
    index.sync_folder(client, "INBOX", 10)
    assert index.count() == 2
    index.close()


def test_folder_filter(tmp_path: Path) -> None:
    index = make_index(tmp_path)
    client = StubClient(sample_messages())
    index.sync_folder(client, "INBOX", 10)
    index.sync_folder(client, "Archive", 10)
    assert [hit.message_id for hit in index.search("facture", "INBOX", 10)] == ["<a@x>"]
    assert [hit.message_id for hit in index.search("facture", "Archive", 10)] == ["<c@x>"]
    index.close()


def test_body_truncation(tmp_path: Path) -> None:
    index = make_index(tmp_path, max_body_chars=10)
    client = StubClient(sample_messages())
    index.sync_folder(client, "INBOX", 10)
    assert index.search("Merci", None, 10)
    assert index.search("vendredi", None, 10) == []
    index.close()


def test_exotic_query_does_not_crash(tmp_path: Path) -> None:
    index = make_index(tmp_path)
    client = StubClient(sample_messages())
    index.sync_folder(client, "INBOX", 10)
    assert index.search('facture "octobre" OR (', None, 10) == []
    assert index.search("", None, 10) == []
    index.close()


def test_clear(tmp_path: Path) -> None:
    index = make_index(tmp_path)
    client = StubClient(sample_messages())
    index.sync_folder(client, "INBOX", 10)
    index.clear()
    assert index.count() == 0
    index.close()
