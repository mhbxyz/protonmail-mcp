from __future__ import annotations

from email import message_from_bytes
from email.policy import default as default_policy

import pytest

from protonmail_mcp.compose import (
    ComposeError,
    DraftAttachment,
    build_draft,
    build_forward,
    build_reply,
    draft_view,
    forward_subject,
    reply_subject,
    validate_recipients,
)
from protonmail_mcp.models import Attachment, EmailContent


def test_validate_recipients_accepts_and_normalizes() -> None:
    assert validate_recipients("Alice <alice@example.com>", header="To") == "Alice <alice@example.com>"
    assert validate_recipients("a@example.com, b@example.com", header="To") == "a@example.com, b@example.com"
    assert validate_recipients("", header="To") == ""
    assert validate_recipients(None, header="To") == ""


def test_validate_recipients_rejects_invalid() -> None:
    with pytest.raises(ComposeError, match="invalid email address"):
        validate_recipients("not-an-email", header="To")
    with pytest.raises(ComposeError, match="invalid email address"):
        validate_recipients("@example.com", header="To")


def test_build_draft_round_trip() -> None:
    composed = build_draft(
        "me@proton.me",
        to="Alice <alice@example.com>",
        cc="bob@example.com",
        subject="Réunion — café",
        body="Bonjour,\n\nÇa tient ?\n",
        in_reply_to="<parent@example.com>",
        references="<root@example.com> <parent@example.com>",
    )
    message = message_from_bytes(composed.raw, policy=default_policy)
    assert str(message["Subject"]) == "Réunion — café"
    assert str(message["To"]) == "Alice <alice@example.com>"
    assert str(message["Cc"]) == "bob@example.com"
    assert str(message["In-Reply-To"]) == "<parent@example.com>"
    assert str(message["References"]) == "<root@example.com> <parent@example.com>"
    assert str(message["From"]) == "me@proton.me"
    assert str(message["Message-ID"]) == composed.message_id
    assert composed.message_id.endswith("@protonmail-mcp.invalid>")
    assert "Bonjour," in message.get_content()


def test_build_draft_without_recipients_omits_headers() -> None:
    composed = build_draft("me@proton.me", subject="Note", body="x")
    message = message_from_bytes(composed.raw, policy=default_policy)
    assert message["To"] is None
    assert message["Cc"] is None
    assert message["Bcc"] is None


def sample_original() -> EmailContent:
    return EmailContent(
        message_id="<parent@example.com>",
        folder="INBOX",
        uid=42,
        subject="Hello",
        sender="Alice <alice@example.com>",
        recipients="Bob <bob@example.com>",
        cc="Carol <carol@example.com>",
        references="<root@example.com>",
        date="Mon, 05 Oct 2026 10:00:00 +0200",
        body_text="Ligne 1\nLigne 2",
    )


def test_reply_subject_prefixing() -> None:
    assert reply_subject("Hello") == "Re: Hello"
    assert reply_subject("Re: Hello") == "Re: Hello"
    assert reply_subject("RE: Hello") == "RE: Hello"
    assert reply_subject("Re[2]: Hello") == "Re[2]: Hello"
    assert reply_subject("") == "Re:"


def test_forward_subject_prefixing() -> None:
    assert forward_subject("Hello") == "Fwd: Hello"
    assert forward_subject("Fwd: Hello") == "Fwd: Hello"
    assert forward_subject("FW: Hello") == "FW: Hello"
    assert forward_subject("") == "Fwd:"


def test_derived_subjects_strip_control_characters() -> None:
    assert "\n" not in reply_subject("Hello\r\nBcc: evil@example.com")
    assert "\n" not in forward_subject("Hello\r\nBcc: evil@example.com")


def test_build_reply_headers_and_quote() -> None:
    composed = build_reply(sample_original(), "me@proton.me", body="Merci !")
    message = message_from_bytes(composed.raw, policy=default_policy)
    assert str(message["Subject"]) == "Re: Hello"
    assert str(message["To"]) == "Alice <alice@example.com>"
    assert message["Cc"] is None
    assert str(message["In-Reply-To"]) == "<parent@example.com>"
    assert str(message["References"]) == "<root@example.com> <parent@example.com>"
    body = message.get_content()
    assert body.startswith("Merci !")
    assert "> Ligne 1" in body
    assert "> Ligne 2" in body
    assert "alice@example.com" in body


def test_build_reply_all_excludes_self_and_dedupes() -> None:
    original = sample_original()
    original.recipients = "Bob <bob@example.com>, me@proton.me"
    original.cc = "Alice <alice@example.com>, Carol <carol@example.com>"
    composed = build_reply(original, "me@proton.me", reply_all=True)
    message = message_from_bytes(composed.raw, policy=default_policy)
    assert str(message["To"]) == "Alice <alice@example.com>"
    cc = str(message["Cc"])
    assert "bob@example.com" in cc
    assert "carol@example.com" in cc
    assert "me@proton.me" not in cc
    assert "alice@example.com" not in cc


def test_build_reply_prefers_reply_to() -> None:
    original = sample_original()
    original.reply_to = "Support <support@example.com>"
    composed = build_reply(original, "me@proton.me")
    message = message_from_bytes(composed.raw, policy=default_policy)
    assert str(message["To"]) == "Support <support@example.com>"


def test_build_forward() -> None:
    original = sample_original()
    original.attachments = [
        Attachment(filename="doc.pdf", content_type="application/pdf", size_bytes=10)
    ]
    composed = build_forward(original, "me@proton.me", to="friend@example.com", body="Pour info")
    message = message_from_bytes(composed.raw, policy=default_policy)
    assert str(message["Subject"]) == "Fwd: Hello"
    assert str(message["To"]) == "friend@example.com"
    assert message["In-Reply-To"] is None
    assert message["References"] is None
    body = message.get_content()
    assert body.startswith("Pour info")
    assert "---------- Forwarded message ----------" in body
    assert "Subject: Hello" in body
    assert "Attachments: doc.pdf" in body
    assert "Ligne 1" in body


def test_draft_view_flags_external_recipients() -> None:
    composed = build_draft(
        "me@proton.me",
        to="Alice <alice@example.com>",
        cc="bob@proton.me",
        subject="Hi",
        body="Body",
    )
    view = draft_view(composed, "me@proton.me")
    assert view.recipients == ["alice@example.com", "bob@proton.me"]
    assert view.external_recipients == ["alice@example.com"]
    assert view.body_text.strip() == "Body"
    assert "Subject: Hi" in view.raw_text
    assert view.headers["Subject"] == "Hi"


def test_build_draft_with_attachment() -> None:
    composed = build_draft(
        "me@proton.me",
        to="alice@example.com",
        subject="With file",
        body="See attached.",
        attachments=[
            DraftAttachment(
                filename="rapport.pdf",
                content_type="application/pdf",
                data=b"%PDF-1.4",
            )
        ],
    )
    message = message_from_bytes(composed.raw, policy=default_policy)
    parts = [part for part in message.walk() if part.get_filename()]
    assert [part.get_filename() for part in parts] == ["rapport.pdf"]
    assert parts[0].get_content_type() == "application/pdf"
    assert parts[0].get_payload(decode=True) == b"%PDF-1.4"


def test_build_forward_with_attachments() -> None:
    original = sample_original()
    original.attachments = [
        Attachment(filename="doc.pdf", content_type="application/pdf", size_bytes=4)
    ]
    composed = build_forward(
        original,
        "me@proton.me",
        to="friend@example.com",
        attachments=[
            DraftAttachment(
                filename="doc.pdf", content_type="application/pdf", data=b"data"
            )
        ],
    )
    message = message_from_bytes(composed.raw, policy=default_policy)
    names = [part.get_filename() for part in message.walk() if part.get_filename()]
    assert names == ["doc.pdf"]
    assert b"---------- Forwarded message ----------" in composed.raw
