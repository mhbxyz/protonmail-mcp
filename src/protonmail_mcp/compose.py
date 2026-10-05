from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, getaddresses, make_msgid

MESSAGE_ID_DOMAIN = "protonmail-mcp.invalid"
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class ComposeError(ValueError):
    """Draft content is invalid."""


@dataclass(frozen=True, slots=True)
class ComposedDraft:
    raw: bytes
    message_id: str


def _reject_control_chars(value: str, *, field: str) -> str:
    if _CONTROL_CHARS.search(value):
        raise ComposeError(f"{field}: control characters are not allowed")
    return value


def validate_recipients(value: str | None, *, header: str) -> str:
    if not value or not value.strip():
        return ""
    try:
        parsed = getaddresses([value])
    except (UnicodeEncodeError, ValueError) as exc:
        raise ComposeError(f"{header}: invalid address list") from exc
    cleaned: list[tuple[str, str]] = []
    for name, address in parsed:
        if not name and not address:
            continue
        if _CONTROL_CHARS.search(name) or _CONTROL_CHARS.search(address):
            raise ComposeError(f"{header}: control characters are not allowed in addresses")
        if "@" not in address or address.startswith("@") or address.endswith("@"):
            raise ComposeError(f"{header}: invalid email address {address or name!r}")
        cleaned.append((name, address))
    if not cleaned:
        raise ComposeError(f"{header}: no valid recipient found in {value!r}")
    try:
        return ", ".join(formataddr(pair) for pair in cleaned)
    except (UnicodeEncodeError, ValueError) as exc:
        raise ComposeError(f"{header}: invalid address list") from exc


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
    message["From"] = _reject_control_chars(sender, field="From")
    if to:
        message["To"] = _reject_control_chars(to, field="To")
    if cc:
        message["Cc"] = _reject_control_chars(cc, field="Cc")
    if bcc:
        message["Bcc"] = _reject_control_chars(bcc, field="Bcc")
    message["Subject"] = _reject_control_chars(subject, field="Subject")
    message["Date"] = format_datetime(datetime.now(UTC))
    message_id = make_msgid(domain=MESSAGE_ID_DOMAIN)
    message["Message-ID"] = message_id
    if in_reply_to:
        message["In-Reply-To"] = _reject_control_chars(in_reply_to, field="In-Reply-To")
    if references:
        message["References"] = _reject_control_chars(references, field="References")
    message.set_content(body)
    return ComposedDraft(raw=message.as_bytes(), message_id=message_id)
