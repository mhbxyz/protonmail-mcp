from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, getaddresses, make_msgid

MESSAGE_ID_DOMAIN = "protonmail-mcp.invalid"


class ComposeError(ValueError):
    """Draft content is invalid."""


@dataclass(frozen=True, slots=True)
class ComposedDraft:
    raw: bytes
    message_id: str


def validate_recipients(value: str | None, *, header: str) -> str:
    if not value or not value.strip():
        return ""
    cleaned: list[tuple[str, str]] = []
    for name, address in getaddresses([value]):
        if not name and not address:
            continue
        if "@" not in address or address.startswith("@") or address.endswith("@"):
            raise ComposeError(f"{header}: invalid email address {address or name!r}")
        cleaned.append((name, address))
    if not cleaned:
        raise ComposeError(f"{header}: no valid recipient found in {value!r}")
    return ", ".join(formataddr(pair) for pair in cleaned)


def build_draft(
    sender: str,
    *,
    to: str = "",
    cc: str = "",
    bcc: str = "",
    subject: str = "",
    body: str = "",
    in_reply_to: str = "",
    references: str = "",
) -> ComposedDraft:
    message = EmailMessage()
    message["From"] = sender
    if to:
        message["To"] = to
    if cc:
        message["Cc"] = cc
    if bcc:
        message["Bcc"] = bcc
    message["Subject"] = subject
    message["Date"] = format_datetime(datetime.now(UTC))
    message_id = make_msgid(domain=MESSAGE_ID_DOMAIN)
    message["Message-ID"] = message_id
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if references:
        message["References"] = references
    message.set_content(body)
    return ComposedDraft(raw=message.as_bytes(), message_id=message_id)
