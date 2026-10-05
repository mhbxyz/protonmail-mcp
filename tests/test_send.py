from __future__ import annotations

import smtplib
from datetime import UTC, datetime
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default as default_policy
from pathlib import Path

import pytest

from protonmail_mcp.config import BridgeConfig
from protonmail_mcp.policy import SendPolicy
from protonmail_mcp.send import (
    SendError,
    SmtpSender,
    body_digest,
    denied_recipients,
    external_recipients,
    guard_reasons,
    recipient_allowed,
    send_key,
    transmission_from_raw,
)
from protonmail_mcp.state import SendState


def base_policy(**overrides: object) -> SendPolicy:
    values: dict[str, object] = {
        "allow_self": True,
        "allowed_recipients": ("bob@ext.test",),
        "allowed_domains": ("corp.example",),
    }
    values.update(overrides)
    return SendPolicy(**values)  # type: ignore[arg-type]


def test_self_address_and_plus_addressing() -> None:
    policy = base_policy()
    assert recipient_allowed("me@proton.me", "me@proton.me", policy)
    assert recipient_allowed("me+tag@proton.me", "me@proton.me", policy)
    assert recipient_allowed("ME@PROTON.ME", "me@proton.me", policy)


def test_allowlists_are_case_insensitive() -> None:
    policy = base_policy()
    assert recipient_allowed("BOB@EXT.TEST", "me@proton.me", policy)
    assert recipient_allowed("x@corp.example", "me@proton.me", policy)
    assert recipient_allowed("x@CORP.EXAMPLE", "me@proton.me", policy)


def test_domain_match_is_exact_not_subdomain() -> None:
    policy = base_policy()
    assert not recipient_allowed("x@sub.corp.example", "me@proton.me", policy)
    assert not recipient_allowed("evil@other.test", "me@proton.me", policy)


def test_allow_self_can_be_disabled() -> None:
    policy = base_policy(allow_self=False)
    assert not recipient_allowed("me@proton.me", "me@proton.me", policy)


def test_denied_recipients_returns_offenders() -> None:
    policy = base_policy()
    denied = denied_recipients(
        ["me@proton.me", "bob@ext.test", "evil@other.test", "x@corp.example"],
        "me@proton.me",
        policy,
    )
    assert denied == ["evil@other.test"]


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def raw_draft(
    to: str = "me@proton.me",
    bcc: str = "me@proton.me",
    body: str = "Corps",
    extra: dict[str, str] | None = None,
) -> bytes:
    message = EmailMessage()
    message["From"] = "me@proton.me"
    message["To"] = to
    if bcc:
        message["Bcc"] = bcc
    message["Subject"] = "Hello"
    message.set_content(body)
    for key, value in (extra or {}).items():
        message[key] = value
    return message.as_bytes()


def make_state(tmp_path: Path) -> SendState:
    return SendState(tmp_path / "state.db", clock=FakeClock())


def test_transmission_strips_bcc_and_builds_envelope() -> None:
    transmission = transmission_from_raw(
        raw_draft(to="a@ext.test", bcc="b@ext.test")
    )
    assert transmission.envelope == ("a@ext.test", "b@ext.test")
    assert transmission.to == ("a@ext.test",)
    assert transmission.bcc == ("b@ext.test",)
    header_block = transmission.payload.split(b"\n\n", 1)[0]
    assert b"Bcc" not in header_block
    assert transmission.payload_hash


def test_transmission_dedupes_recipients() -> None:
    transmission = transmission_from_raw(
        raw_draft(to="A@Ext.test, b@ext.test", bcc="a@ext.test")
    )
    assert transmission.envelope == ("a@ext.test", "b@ext.test")


def test_transmission_warns_on_reply_to_mismatch() -> None:
    message = message_from_bytes(raw_draft(), policy=default_policy)
    message["Reply-To"] = "other@ext.test"
    transmission = transmission_from_raw(message.as_bytes())
    assert "Reply-To differs from From" in transmission.warnings


def test_guard_reasons_flags_automated_headers(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    policy = SendPolicy()
    raw = raw_draft(extra={"Auto-Submitted": "auto-replied"})
    reasons = guard_reasons(raw, transmission_from_raw(raw), policy, state)
    assert any("Auto-Submitted" in reason for reason in reasons)


def test_guard_reasons_flags_no_reply_and_lists(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    policy = SendPolicy()
    raw = raw_draft(to="noreply@ext.test", extra={"List-Id": "<list.test>"})
    reasons = guard_reasons(raw, transmission_from_raw(raw), policy, state)
    assert any("no-reply" in reason for reason in reasons)
    assert any("mailing-list" in reason for reason in reasons)


def test_guard_reasons_flags_bulk_precedence(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    policy = SendPolicy()
    raw = raw_draft(extra={"Precedence": "bulk"})
    reasons = guard_reasons(raw, transmission_from_raw(raw), policy, state)
    assert any("Precedence" in reason for reason in reasons)


def test_guard_reasons_flags_thread_depth(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    policy = SendPolicy(max_thread_depth=2)
    references = " ".join(f"<m{index}@x>" for index in range(4))
    raw = raw_draft(extra={"References": references})
    reasons = guard_reasons(raw, transmission_from_raw(raw), policy, state)
    assert any("thread depth" in reason for reason in reasons)


def test_guard_reasons_flags_duplicate_body(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    policy = SendPolicy(duplicate_window_seconds=600)
    raw = raw_draft(body="Identique")
    transmission = transmission_from_raw(raw)
    state.remember_body(body_digest(transmission.body))
    reasons = guard_reasons(raw, transmission, policy, state)
    assert any("identical body" in reason for reason in reasons)


def test_external_recipients() -> None:
    assert external_recipients(
        ["me@proton.me", "x@other.test", "me+tag@proton.me"], "me@proton.me"
    ) == ["x@other.test"]


def test_send_key_is_stable_and_distinct() -> None:
    assert send_key("<a@x>", "Drafts") == send_key("<a@x>", "Drafts")
    assert send_key("<a@x>", "Drafts") != send_key("<b@x>", "Drafts")


class FakeSMTP:
    instances: list[FakeSMTP] = []

    def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
        self.host = host
        self.port = port
        self.calls: list[object] = []
        FakeSMTP.instances.append(self)

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        self.calls.append("quit")
        return False

    def ehlo(self) -> None:
        self.calls.append("ehlo")

    def starttls(self, context: object = None) -> None:
        self.calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        self.calls.append(("login", user))

    def sendmail(self, sender: str, recipients: list[str], payload: bytes) -> None:
        self.calls.append(("sendmail", sender, list(recipients), payload))


def make_config() -> BridgeConfig:
    return BridgeConfig(
        host="127.0.0.1",
        imap_port=1143,
        smtp_port=1025,
        username="me@proton.me",
        password="secret",
        timeout=5.0,
        verify_tls=False,
    )


def test_smtp_sender_starttls_login_and_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeSMTP.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    SmtpSender(make_config()).send(b"payload", ["a@ext.test", "b@ext.test"])
    server = FakeSMTP.instances[0]
    assert server.calls[0] == "ehlo"
    assert "starttls" in server.calls
    assert ("login", "me@proton.me") in server.calls
    send_calls = [
        call
        for call in server.calls
        if isinstance(call, tuple) and call[0] == "sendmail"
    ]
    assert send_calls[0][1] == "me@proton.me"
    assert send_calls[0][2] == ["a@ext.test", "b@ext.test"]
    assert send_calls[0][3] == b"payload"


def test_smtp_sender_wraps_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingSMTP(FakeSMTP):
        def sendmail(self, sender: str, recipients: list[str], payload: bytes) -> None:
            raise smtplib.SMTPException("boom")

    monkeypatch.setattr(smtplib, "SMTP", FailingSMTP)
    with pytest.raises(SendError, match="SMTP submission failed"):
        SmtpSender(make_config()).send(b"x", ["a@ext.test"])
