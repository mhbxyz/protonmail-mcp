from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, getaddresses, make_msgid

from .models import EmailContent

MESSAGE_ID_DOMAIN = "protonmail-mcp.invalid"
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_REPLY_PREFIX = re.compile(r"^\s*re(\[\d+\])?\s*:", re.IGNORECASE)
_FORWARD_PREFIX = re.compile(r"^\s*fwd?\s*:", re.IGNORECASE)
_FORWARD_HEADER = "---------- Forwarded message ----------"


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


def _sanitize_header_text(value: str) -> str:
    return _CONTROL_CHARS.sub(" ", value).strip()


def reply_subject(subject: str) -> str:
    clean = _sanitize_header_text(subject)
    if not clean:
        return "Re:"
    if _REPLY_PREFIX.match(clean):
        return clean
    return f"Re: {clean}"


def forward_subject(subject: str) -> str:
    clean = _sanitize_header_text(subject)
    if not clean:
        return "Fwd:"
    if _FORWARD_PREFIX.match(clean):
        return clean
    return f"Fwd: {clean}"


def _address_pairs(value: str) -> list[tuple[str, str]]:
    if not value:
        return []
    try:
        parsed = getaddresses([value])
    except (UnicodeEncodeError, ValueError):
        return []
    return [(name, address) for name, address in parsed if address]


def reply_recipients(original: EmailContent, self_address: str, reply_all: bool) -> tuple[str, str]:
    primary = _address_pairs(original.reply_to or original.sender)
    if not primary:
        return "", ""
    self_key = self_address.strip().lower()
    seen: set[str] = set()
    to: list[tuple[str, str]] = []
    for name, address in primary:
        key = address.lower()
        if key == self_key or key in seen:
            continue
        to.append((_sanitize_header_text(name), address))
        seen.add(key)
    cc: list[tuple[str, str]] = []
    if reply_all:
        for name, address in _address_pairs(original.recipients) + _address_pairs(original.cc):
            key = address.lower()
            if key == self_key or key in seen:
                continue
            cc.append((_sanitize_header_text(name), address))
            seen.add(key)

    def render(pairs: list[tuple[str, str]]) -> str:
        return ", ".join(formataddr(pair) for pair in pairs)

    return render(to), render(cc)


def quote_original(original: EmailContent) -> str:
    attribution = (
        f"On {original.date or original.received or 'an unknown date'}, "
        f"{original.sender or 'an unknown sender'} wrote:"
    )
    body = original.body_text.strip()
    if not body:
        return attribution
    quoted = "\n".join(f"> {line}" if line else ">" for line in body.splitlines())
    return f"{attribution}\n{quoted}"


def forwarded_block(original: EmailContent) -> str:
    lines = [
        _FORWARD_HEADER,
        f"From: {original.sender}",
        f"Date: {original.date or original.received}",
        f"Subject: {_sanitize_header_text(original.subject)}",
        f"To: {original.recipients}",
    ]
    if original.cc:
        lines.append(f"Cc: {original.cc}")
    if original.attachments:
        names = ", ".join(attachment.filename for attachment in original.attachments)
        lines.append(f"Attachments: {names}")
    lines.append("")
    lines.append(original.body_text.strip())
    return "\n".join(lines)


def build_reply(
    original: EmailContent,
    sender: str,
    *,
    body: str = "",
    reply_all: bool = False,
) -> ComposedDraft:
    to, cc = reply_recipients(original, sender, reply_all)
    chain = " ".join(part for part in (original.references, original.message_id) if part)
    quoted = quote_original(original)
    text = f"{body.rstrip()}\n\n{quoted}" if body.strip() else quoted
    return build_draft(
        sender,
        to=to,
        cc=cc,
        subject=reply_subject(original.subject),
        body=text,
        in_reply_to=original.message_id,
        references=chain,
    )


def build_forward(
    original: EmailContent,
    sender: str,
    *,
    to: str = "",
    body: str = "",
) -> ComposedDraft:
    block = forwarded_block(original)
    text = f"{body.rstrip()}\n\n{block}" if body.strip() else block
    return build_draft(
        sender,
        to=to,
        subject=forward_subject(original.subject),
        body=text,
    )
