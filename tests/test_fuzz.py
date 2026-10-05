from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from protonmail_mcp.bridge import _append_uid
from protonmail_mcp.compose import ComposeError, validate_recipients
from protonmail_mcp.parsing import full_from_message


@given(st.binary(max_size=4096))
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_full_from_message_never_crashes(raw: bytes) -> None:
    email = full_from_message(raw, uid=1, folder="INBOX", flags=[], size=len(raw), max_chars=1000)
    assert isinstance(email.body_text, str)


@given(st.text(max_size=200))
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_validate_recipients_never_produces_header_injection(value: str) -> None:
    try:
        result = validate_recipients(value, header="To")
    except ComposeError:
        return
    assert "\r" not in result
    assert "\n" not in result


@given(st.binary(max_size=256))
@settings(max_examples=200, deadline=None)
def test_append_uid_returns_int_or_none(raw: bytes) -> None:
    result = _append_uid(raw)
    assert result is None or result >= 0
