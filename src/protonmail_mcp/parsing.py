from __future__ import annotations

import re
from collections.abc import Sequence
from email import message_from_bytes
from email.header import decode_header
from email.message import Message
from email.policy import default as default_policy
from email.utils import formataddr, getaddresses
from html.parser import HTMLParser

from .models import Attachment, EmailContent, EmailSummary

_BLOCK_TAGS = {
    "address",
    "article",
    "blockquote",
    "div",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "li",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "tr",
    "ul",
}
_SKIP_TAGS = {"script", "style", "head", "title"}
_BLANK_LINES = re.compile(r"\n{3,}")
_TRAILING_SPACES = re.compile(r"[ \t]+\n")
_PHRASE_SPECIALS = re.compile(r'[()<>@,;:\\".\[\]]')


class _HtmlToText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "br" or tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        joined = "".join(self._parts)
        collapsed = _BLANK_LINES.sub("\n\n", joined)
        return _TRAILING_SPACES.sub("\n", collapsed).strip()


def html_to_text(html: str) -> str:
    parser = _HtmlToText()
    parser.feed(html)
    parser.close()
    return parser.text()


def header_value(message: Message, name: str) -> str:
    value = message.get(name)
    return str(value).strip() if value else ""


def format_addresses(value: str | None) -> str:
    if not value:
        return ""
    return ", ".join(formataddr(pair) for pair in getaddresses([value]) if pair[0] or pair[1])


def _decode_words(value: str) -> str:
    parts: list[str] = []
    for chunk, charset in decode_header(value):
        if isinstance(chunk, bytes):
            try:
                parts.append(chunk.decode(charset or "utf-8", errors="replace"))
            except LookupError:
                parts.append(chunk.decode("utf-8", errors="replace"))
        else:
            parts.append(chunk)
    return "".join(parts)


def _format_address(display_name: str, addr_spec: str) -> str:
    display_name = display_name.strip()
    if not addr_spec:
        return display_name
    if not display_name:
        return addr_spec
    if _PHRASE_SPECIALS.search(display_name):
        escaped = display_name.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}" <{addr_spec}>'
    return f"{display_name} <{addr_spec}>"


def address_header(message: Message, name: str) -> str:
    header = message.get(name)
    if header is None:
        return ""
    addresses = getattr(header, "addresses", None)
    if addresses is None:
        return format_addresses(str(header))
    formatted = []
    for address in addresses:
        display_name = _decode_words(str(address.display_name)) if address.display_name else ""
        entry = _format_address(display_name, str(address.addr_spec))
        if entry:
            formatted.append(entry)
    return ", ".join(formatted)


def _is_attachment(part: Message) -> bool:
    getter = getattr(part, "get_content_disposition", None)
    disposition = getter() if getter else None
    if not disposition:
        raw = part.get("Content-Disposition") or ""
        disposition = raw.split(";", 1)[0]
    return disposition.strip().lower() == "attachment"


def _part_text(part: Message) -> str:
    getter = getattr(part, "get_content", None)
    if getter is not None:
        try:
            content = getter()
            if isinstance(content, str):
                return content
        except (LookupError, UnicodeDecodeError, ValueError):
            pass
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        return ""
    charset = part.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace")


def extract_body(message: Message) -> str:
    plain: Message | None = None
    html: Message | None = None
    for part in message.walk():
        if part.is_multipart() or _is_attachment(part):
            continue
        content_type = part.get_content_type()
        if content_type == "text/plain" and plain is None:
            plain = part
        elif content_type == "text/html" and html is None:
            html = part
    if plain is not None:
        return _part_text(plain)
    if html is not None:
        return html_to_text(_part_text(html))
    return ""


def extract_attachments(message: Message) -> list[Attachment]:
    attachments: list[Attachment] = []
    for part in message.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        if not filename and not _is_attachment(part):
            continue
        payload = part.get_payload(decode=True)
        attachments.append(
            Attachment(
                filename=filename or "(unnamed)",
                content_type=part.get_content_type(),
                size_bytes=len(payload) if isinstance(payload, bytes) else 0,
            )
        )
    return attachments


def truncate(text: str, max_chars: int) -> tuple[str, bool]:
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def summary_from_header(
    raw_header: bytes,
    *,
    uid: int,
    folder: str,
    flags: Sequence[str],
    size: int,
    received: str = "",
) -> EmailSummary:
    message = message_from_bytes(raw_header, policy=default_policy)
    return EmailSummary(
        message_id=header_value(message, "Message-ID"),
        folder=folder,
        uid=uid,
        subject=header_value(message, "Subject"),
        sender=address_header(message, "From"),
        recipients=address_header(message, "To"),
        date=header_value(message, "Date"),
        received=received,
        unread="\\Seen" not in flags,
        flagged="\\Flagged" in flags,
        size_bytes=size,
    )


def full_from_message(
    raw: bytes,
    *,
    uid: int,
    folder: str,
    flags: Sequence[str],
    size: int,
    max_chars: int,
    received: str = "",
) -> EmailContent:
    message = message_from_bytes(raw, policy=default_policy)
    body, truncated = truncate(extract_body(message), max_chars)
    return EmailContent(
        message_id=header_value(message, "Message-ID"),
        folder=folder,
        uid=uid,
        subject=header_value(message, "Subject"),
        sender=address_header(message, "From"),
        recipients=address_header(message, "To"),
        date=header_value(message, "Date"),
        received=received,
        unread="\\Seen" not in flags,
        flagged="\\Flagged" in flags,
        size_bytes=size,
        cc=address_header(message, "Cc"),
        reply_to=address_header(message, "Reply-To"),
        references=header_value(message, "References"),
        body_text=body,
        truncated=truncated,
        attachments=extract_attachments(message),
    )
