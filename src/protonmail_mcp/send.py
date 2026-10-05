from __future__ import annotations

import re
import smtplib
import ssl
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from email import message_from_bytes
from email.message import Message
from email.policy import default as default_policy
from email.utils import getaddresses
from hashlib import sha256

from .config import BridgeConfig
from .confirmations import payload_digest
from .parsing import extract_attachments, extract_body
from .policy import SendPolicy
from .state import SendState

_NO_REPLY_LOCALS = {"noreply", "no-reply", "donotreply", "do-not-reply"}
_MESSAGE_ID_RE = re.compile(r"<[^<>\s]+>")


class SendError(RuntimeError):
    """A message cannot be sent because of policy, guards, or SMTP failure."""


def _normalize(address: str) -> str:
    return address.strip().lower()


def _is_self(address: str, account: str) -> bool:
    normalized = _normalize(address)
    account_normalized = _normalize(account)
    if normalized == account_normalized:
        return True
    local, _, domain = account_normalized.partition("@")
    candidate_local, _, candidate_domain = normalized.partition("@")
    base_local = candidate_local.split("+", 1)[0]
    return base_local == local and candidate_domain == domain


def _allowed_domains(policy: SendPolicy) -> set[str]:
    return {item.strip().lower().lstrip("@.") for item in policy.allowed_domains}


def recipient_allowed(address: str, account: str, policy: SendPolicy) -> bool:
    normalized = _normalize(address)
    if policy.allow_self and _is_self(normalized, account):
        return True
    if normalized in {_normalize(item) for item in policy.allowed_recipients}:
        return True
    domain = normalized.partition("@")[2]
    return domain in _allowed_domains(policy)


def denied_recipients(
    addresses: Sequence[str], account: str, policy: SendPolicy
) -> list[str]:
    return [
        address for address in addresses if not recipient_allowed(address, account, policy)
    ]


def external_recipients(addresses: Sequence[str], account: str) -> list[str]:
    account_domain = _normalize(account).partition("@")[2]
    return [
        address
        for address in addresses
        if _normalize(address).partition("@")[2] != account_domain
    ]


def _addresses(message: Message, field: str) -> tuple[str, ...]:
    value = message.get(field)
    if not value:
        return ()
    return tuple(
        _normalize(address) for _, address in getaddresses([str(value)]) if address
    )


@dataclass(frozen=True, slots=True)
class Transmission:
    payload: bytes
    envelope: tuple[str, ...]
    to: tuple[str, ...]
    cc: tuple[str, ...]
    bcc: tuple[str, ...]
    subject: str
    body: str
    attachments: tuple[str, ...]
    payload_hash: str
    warnings: tuple[str, ...]


def transmission_from_raw(raw: bytes) -> Transmission:
    message = message_from_bytes(raw, policy=default_policy)
    to = _addresses(message, "To")
    cc = _addresses(message, "Cc")
    bcc = _addresses(message, "Bcc")
    envelope = tuple(dict.fromkeys(to + cc + bcc))
    warnings: list[str] = []
    reply_to = _addresses(message, "Reply-To")
    sender = _addresses(message, "From")
    if reply_to and sender and set(reply_to) != set(sender):
        warnings.append("Reply-To differs from From")
    if bcc:
        del message["Bcc"]
    payload = message.as_bytes()
    return Transmission(
        payload=payload,
        envelope=envelope,
        to=to,
        cc=cc,
        bcc=bcc,
        subject=str(message.get("Subject") or ""),
        body=extract_body(message),
        attachments=tuple(
            attachment.filename for attachment in extract_attachments(message)
        ),
        payload_hash=sha256(payload).hexdigest(),
        warnings=tuple(warnings),
    )


def body_digest(body: str) -> str:
    return sha256(body.strip().encode("utf-8", errors="replace")).hexdigest()


def send_key(draft_message_id: str, folder: str) -> str:
    return payload_digest(
        {"action": "send_draft", "message_id": draft_message_id, "folder": folder}
    )


def guard_reasons(
    raw: bytes,
    transmission: Transmission,
    policy: SendPolicy,
    state: SendState,
    now: datetime | None = None,
) -> list[str]:
    message = message_from_bytes(raw, policy=default_policy)
    reasons: list[str] = []
    auto_submitted = str(message.get("Auto-Submitted") or "").strip().lower()
    if auto_submitted and auto_submitted != "no":
        reasons.append("Auto-Submitted header present")
    precedence = str(message.get("Precedence") or "").strip().lower()
    if precedence in {"bulk", "list", "junk"}:
        reasons.append(f"Precedence: {precedence} header present")
    if any(str(key).lower().startswith("list-") for key in message.keys()):
        reasons.append("mailing-list headers present")
    for header in ("X-Autoreply", "X-Autorespond", "X-Autoresponder"):
        if message.get(header):
            reasons.append(f"{header} header present")
    for address in transmission.envelope:
        local = address.partition("@")[0]
        if local in _NO_REPLY_LOCALS:
            reasons.append(f"no-reply recipient: {address}")
    depth = len(_MESSAGE_ID_RE.findall(str(message.get("References") or "")))
    if depth > policy.max_thread_depth:
        reasons.append(f"thread depth {depth} exceeds {policy.max_thread_depth}")
    if state.body_seen(body_digest(transmission.body), policy.duplicate_window_seconds, now):
        reasons.append("an identical body was sent recently")
    return reasons


class SmtpSender:
    def __init__(self, config: BridgeConfig) -> None:
        self._config = config

    def send(self, payload: bytes, recipients: Sequence[str]) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        if not self._config.verify_tls:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        try:
            with smtplib.SMTP(
                self._config.host,
                self._config.smtp_port,
                timeout=self._config.timeout,
            ) as server:
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
                server.login(self._config.username, self._config.password)
                server.sendmail(self._config.username, list(recipients), payload)
        except (smtplib.SMTPException, OSError) as exc:
            raise SendError(f"SMTP submission failed: {exc}") from exc
