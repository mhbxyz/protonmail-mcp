from __future__ import annotations

from protonmail_mcp.parsing import (
    attachment_parts,
    extract_attachments,
    extract_body,
    extract_links,
    full_from_message,
    header_message_id,
    html_to_markdown,
    html_to_text,
    strip_quoted,
    summary_from_header,
    thread_parent_ids,
    truncate,
)

SIMPLE_EML = b"""From: Alice <alice@example.com>
To: Bob <bob@proton.me>
Cc: carol@example.com
Subject: =?utf-8?Q?Rapport=20mensuel?=
Date: Fri, 03 Oct 2026 10:00:00 +0200
Message-ID: <abc123@example.com>
Content-Type: text/plain; charset=utf-8
Content-Transfer-Encoding: 8bit

Bonjour Bob,
Voici le rapport.
"""

MULTIPART_EML = b"""From: Alice <alice@example.com>
To: bob@proton.me
Subject: Facture
Date: Sat, 04 Oct 2026 09:30:00 +0200
Message-ID: <facture-42@example.com>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="BOUND"

--BOUND
Content-Type: text/plain; charset=utf-8

Merci pour votre achat.
--BOUND
Content-Type: text/html; charset=utf-8

<html><body><p>Merci pour votre <b>achat</b>.</p></body></html>
--BOUND
Content-Type: application/pdf; name="facture.pdf"
Content-Disposition: attachment; filename="facture.pdf"
Content-Transfer-Encoding: base64

JVBERi0xLjQK
--BOUND--
"""

HTML_EML = b"""From: Newsletter <news@example.com>
To: bob@proton.me
Subject: Promo
Date: Sun, 05 Oct 2026 08:00:00 +0200
Message-ID: <promo@example.com>
Content-Type: text/html; charset=utf-8

<html><head><style>p{color:red}</style></head><body>
<p>Bonjour,</p><p>Voici une <b>promo</b>&nbsp;!</p>
</body></html>
"""

ENCODED_EML = b"""From: =?utf-8?q?CIC_=C3=89pargne_Salariale?= <notif@example.fr>
To: Jean Dupont <jean@proton.me>
Subject: Test encodage
Date: Mon, 05 Oct 2026 12:00:00 +0200
Message-ID: <enc@example.fr>

Corps.
"""


def test_full_from_simple_message() -> None:
    email = full_from_message(SIMPLE_EML, uid=1, folder="INBOX", flags=[], size=len(SIMPLE_EML), max_chars=20000)
    assert email.subject == "Rapport mensuel"
    assert email.sender == "Alice <alice@example.com>"
    assert email.recipients == "Bob <bob@proton.me>"
    assert email.cc == "carol@example.com"
    assert email.message_id == "<abc123@example.com>"
    assert email.unread is True
    assert "Voici le rapport." in email.body_text
    assert email.truncated is False
    assert email.attachments == []


def test_read_flags() -> None:
    email = full_from_message(
        SIMPLE_EML, uid=1, folder="INBOX", flags=["\\Seen", "\\Flagged"], size=1, max_chars=100
    )
    assert email.unread is False
    assert email.flagged is True


def test_multipart_prefers_plain_text_and_lists_attachments() -> None:
    email = full_from_message(
        MULTIPART_EML, uid=7, folder="INBOX", flags=[], size=len(MULTIPART_EML), max_chars=20000
    )
    assert email.body_text.strip() == "Merci pour votre achat."
    attachments = email.attachments
    assert len(attachments) == 1
    assert attachments[0].filename == "facture.pdf"
    assert attachments[0].content_type == "application/pdf"
    assert attachments[0].size_bytes > 0


def test_html_only_falls_back_to_text() -> None:
    email = full_from_message(HTML_EML, uid=2, folder="INBOX", flags=[], size=1, max_chars=20000)
    assert "Bonjour," in email.body_text
    assert "promo" in email.body_text
    assert "<b>" not in email.body_text
    assert "color:red" not in email.body_text


def test_truncation() -> None:
    email = full_from_message(SIMPLE_EML, uid=1, folder="INBOX", flags=[], size=1, max_chars=5)
    assert email.body_text == "Bonjo"
    assert email.truncated is True


def test_encoded_display_names_are_decoded() -> None:
    email = full_from_message(ENCODED_EML, uid=1, folder="INBOX", flags=[], size=1, max_chars=100)
    assert email.sender == "CIC Épargne Salariale <notif@example.fr>"
    assert email.recipients == "Jean Dupont <jean@proton.me>"


def test_summary_from_header() -> None:
    summary = summary_from_header(
        SIMPLE_EML,
        uid=3,
        folder="INBOX",
        flags=[],
        size=42,
        received="2026-10-03 10:00:00",
    )
    assert summary.message_id == "<abc123@example.com>"
    assert summary.subject == "Rapport mensuel"
    assert summary.unread is True
    assert summary.size_bytes == 42
    assert summary.received == "2026-10-03 10:00:00"


def test_helpers() -> None:
    assert truncate("abcdef", 3) == ("abc", True)
    assert truncate("abc", 0) == ("abc", False)
    assert "ligne 1" in html_to_text("<p>ligne 1</p><p>ligne 2</p>")
    from email import message_from_bytes

    message = message_from_bytes(MULTIPART_EML)
    assert "Merci" in extract_body(message)
    assert len(extract_attachments(message)) == 1


def test_attachment_parts_extracts_payloads() -> None:
    parts = attachment_parts(MULTIPART_EML)
    assert len(parts) == 1
    assert parts[0].filename == "facture.pdf"
    assert parts[0].content_type == "application/pdf"
    assert parts[0].data


def test_html_to_markdown_keeps_links_and_lists() -> None:
    markdown = html_to_markdown(
        '<p>Hello</p><ul><li>One</li><li>Two</li></ul>'
        '<a href="https://x.test/a">link</a>'
    )
    assert "Hello" in markdown
    assert "- One" in markdown
    assert "[link](https://x.test/a)" in markdown


def test_extract_links_dedupes() -> None:
    links = extract_links(
        text="see https://a.test and https://a.test",
        html='<a href="https://b.test">x</a>',
    )
    assert links == ["https://b.test", "https://a.test"]


def test_strip_quoted_and_signature() -> None:
    text, removed = strip_quoted("Merci !\n\nOn Mon, Alice wrote:\n> old")
    assert removed is True
    assert text == "Merci !"
    text2, removed2 = strip_quoted("Body\n-- \nSig")
    assert removed2 is True
    assert text2 == "Body"
    text3, removed3 = strip_quoted("Just body")
    assert removed3 is False


def test_thread_helpers() -> None:
    raw = b"References: <root@x> <mid@x>\nIn-Reply-To: <mid@x>\n\n"
    assert thread_parent_ids(raw) == ["<root@x>", "<mid@x>"]
    assert header_message_id(b"Message-ID: <m@x>\n\n") == "<m@x>"


def test_full_from_message_exposes_rendering_metadata() -> None:
    email = full_from_message(HTML_EML, uid=3, folder="INBOX", flags=[], size=1, max_chars=20000)
    assert email.content_trust == "untrusted"
    assert email.quoted_removed is False
    assert "Bonjour," in email.body_text
