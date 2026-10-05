from __future__ import annotations

from email import message_from_bytes
from email.policy import default as default_policy

import pytest

from protonmail_mcp.compose import ComposeError, build_draft, validate_recipients


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
