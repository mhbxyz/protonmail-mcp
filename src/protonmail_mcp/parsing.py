from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from email import message_from_bytes
from email.header import decode_header
from email.message import Message
from email.policy import default as default_policy
from email.utils import formataddr, getaddresses
from html.parser import HTMLParser
from typing import Any

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


class _HtmlToMarkdown(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0
        self._link_stack: list[str | None] = []
        self._link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "a":
            href = dict(attrs).get("href") or ""
            self._link_stack.append(href or None)
            self._link_text.append("")
        elif tag == "li":
            self._parts.append("\n- ")
        elif tag == "br" or tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag == "a" and self._link_stack:
            href = self._link_stack.pop()
            text = self._link_text.pop()
            if href and text.strip():
                self._parts.append(f"[{text.strip()}]({href})")
            else:
                self._parts.append(text)
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._link_stack:
            self._link_text[-1] += data
        else:
            self._parts.append(data)

    def text(self) -> str:
        joined = "".join(self._parts)
        collapsed = _BLANK_LINES.sub("\n\n", joined)
        return _TRAILING_SPACES.sub("\n", collapsed).strip()


def html_to_markdown(html: str) -> str:
    parser = _HtmlToMarkdown()
    parser.feed(html)
    parser.close()
    return parser.text()


_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")
_HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


def extract_links(text: str = "", html: str = "") -> list[str]:
    links: list[str] = []
    links.extend(match.group(1) for match in _HREF_RE.finditer(html or ""))
    links.extend(match.group(0) for match in _URL_RE.finditer(text or ""))
    seen: set[str] = set()
    unique: list[str] = []
    for link in links:
        if link not in seen:
            seen.add(link)
            unique.append(link)
    return unique


_ATTRIBUTION = re.compile(r"^\s*(On .+wrote:|Le .+ a écrit\s*:|El .+ escribió:)", re.IGNORECASE)
_SIGNATURE = re.compile(r"^\s*--\s*$")
_QUOTED_LINE = re.compile(r"^\s*>")


def strip_quoted(text: str) -> tuple[str, bool]:
    kept: list[str] = []
    removed = False
    for line in text.splitlines():
        if _QUOTED_LINE.match(line) or _ATTRIBUTION.match(line) or _SIGNATURE.match(line):
            removed = True
            break
        kept.append(line)
    return "\n".join(kept).strip(), removed


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


def extract_bodies(message: Message) -> tuple[str, str]:
    plain = ""
    html = ""
    for part in message.walk():
        if part.is_multipart() or _is_attachment(part):
            continue
        content_type = part.get_content_type()
        if content_type == "text/plain" and not plain:
            plain = _part_text(part)
        elif content_type == "text/html" and not html:
            html = _part_text(part)
    return plain, html


def extract_body(message: Message) -> str:
    plain, html = extract_bodies(message)
    if plain:
        return plain
    if html:
        return html_to_text(html)
    return ""


@dataclass(frozen=True, slots=True)
class AttachmentPart:
    filename: str
    content_type: str
    data: bytes


def extract_attachment_parts(message: Message) -> list[AttachmentPart]:
    parts: list[AttachmentPart] = []
    for part in message.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        if not filename and not _is_attachment(part):
            continue
        payload = part.get_payload(decode=True)
        parts.append(
            AttachmentPart(
                filename=filename or "(unnamed)",
                content_type=part.get_content_type(),
                data=payload if isinstance(payload, bytes) else b"",
            )
        )
    return parts


def attachment_parts(raw: bytes) -> list[AttachmentPart]:
    return extract_attachment_parts(message_from_bytes(raw, policy=default_policy))


_MESSAGE_ID_RE = re.compile(r"<[^<>\s]+>")


def _normalize_message_ids(value: str) -> list[str]:
    ids = _MESSAGE_ID_RE.findall(value)
    if not ids:
        ids = [part for part in value.split() if "@" in part]
    return [item if item.startswith("<") else f"<{item}>" for item in ids]


def thread_parent_ids(raw_header: bytes) -> list[str]:
    message = message_from_bytes(raw_header, policy=default_policy)
    ids = _normalize_message_ids(header_value(message, "References"))
    ids.extend(_normalize_message_ids(header_value(message, "In-Reply-To")))
    seen: set[str] = set()
    unique: list[str] = []
    for message_id in ids:
        if message_id not in seen:
            seen.add(message_id)
            unique.append(message_id)
    return unique


def header_message_id(raw_header: bytes) -> str:
    return header_value(message_from_bytes(raw_header, policy=default_policy), "Message-ID")


def structure_has_attachment(structure: Any) -> bool:
    if not isinstance(structure, tuple) or not structure:
        return False
    first = structure[0]
    if isinstance(first, list):
        return any(structure_has_attachment(part) for part in first)
    if not isinstance(first, bytes):
        return False
    maintype = first.lower()
    subtype = (
        structure[1].lower()
        if len(structure) > 1 and isinstance(structure[1], bytes)
        else b""
    )
    if maintype == b"text" and subtype in (b"plain", b"html"):
        return _mentions_attachment_extension(structure)
    return True


def _mentions_attachment_extension(value: Any) -> bool:
    if isinstance(value, (list, tuple)):
        if (
            value
            and isinstance(value[0], bytes)
            and value[0].lower() in (b"attachment", b"inline", b"filename")
        ):
            return True
        return any(_mentions_attachment_extension(item) for item in value)
    return False


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
    plain, html = extract_bodies(message)
    base = plain if plain else html_to_text(html)
    stripped, quoted_removed = strip_quoted(base)
    body, truncated = truncate(stripped, max_chars)
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
        body_markdown=html_to_markdown(html) if html else "",
        links=extract_links(text=base, html=html),
        quoted_removed=quoted_removed,
        truncated=truncated,
        attachments=extract_attachments(message),
    )
