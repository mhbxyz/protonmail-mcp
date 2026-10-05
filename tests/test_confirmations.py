from __future__ import annotations

import pytest

from protonmail_mcp.confirmations import (
    ConfirmationError,
    ConfirmationManager,
    payload_digest,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_prepare_and_commit_happy_path() -> None:
    manager = ConfirmationManager(clock=FakeClock())
    prepared = manager.prepare("send_email", {"to": "a@example.com", "body": "hi"})
    assert prepared.token
    assert prepared.action == "send_email"
    assert prepared.expires_in == 300
    assert prepared.preview == {"to": "a@example.com", "body": "hi"}
    assert manager.pending_count() == 1
    manager.commit(prepared.token, {"to": "a@example.com", "body": "hi"})
    assert manager.pending_count() == 0


def test_token_is_single_use() -> None:
    manager = ConfirmationManager(clock=FakeClock())
    prepared = manager.prepare("move_email", {"uid": 1})
    manager.commit(prepared.token, {"uid": 1})
    with pytest.raises(ConfirmationError, match="unknown, expired, or already used"):
        manager.commit(prepared.token, {"uid": 1})


def test_payload_key_order_does_not_matter() -> None:
    manager = ConfirmationManager(clock=FakeClock())
    prepared = manager.prepare("action", {"a": 1, "b": 2})
    manager.commit(prepared.token, {"b": 2, "a": 1})


def test_tampered_payload_revokes_token() -> None:
    manager = ConfirmationManager(clock=FakeClock())
    prepared = manager.prepare("send_email", {"amount": 1})
    with pytest.raises(ConfirmationError, match="does not match"):
        manager.commit(prepared.token, {"amount": 1000})
    with pytest.raises(ConfirmationError, match="unknown, expired, or already used"):
        manager.commit(prepared.token, {"amount": 1})


def test_expired_token_rejected() -> None:
    clock = FakeClock()
    manager = ConfirmationManager(ttl_seconds=10, clock=clock)
    prepared = manager.prepare("action", {"a": 1})
    clock.now = 10.0
    with pytest.raises(ConfirmationError, match="expired"):
        manager.commit(prepared.token, {"a": 1})


def test_expired_entries_are_purged() -> None:
    clock = FakeClock()
    manager = ConfirmationManager(ttl_seconds=10, clock=clock)
    manager.prepare("action", {"a": 1})
    clock.now = 100.0
    assert manager.pending_count() == 0


def test_discard() -> None:
    manager = ConfirmationManager(clock=FakeClock())
    prepared = manager.prepare("action", {"a": 1})
    assert manager.discard(prepared.token) is True
    assert manager.discard(prepared.token) is False
    assert manager.pending_count() == 0


def test_unknown_token_rejected() -> None:
    manager = ConfirmationManager(clock=FakeClock())
    with pytest.raises(ConfirmationError):
        manager.commit("not-a-token", {})


def test_max_pending_enforced() -> None:
    manager = ConfirmationManager(max_pending=1, clock=FakeClock())
    manager.prepare("action", {"a": 1})
    with pytest.raises(ConfirmationError, match="too many pending"):
        manager.prepare("action", {"a": 2})


def test_custom_preview() -> None:
    manager = ConfirmationManager(clock=FakeClock())
    prepared = manager.prepare("send_email", {"body": "x"}, preview={"summary": "1 recipient"})
    assert prepared.preview == {"summary": "1 recipient"}
    assert prepared.digest == payload_digest({"body": "x"})


def test_payload_digest_is_canonical() -> None:
    assert payload_digest({"a": 1, "b": [1, 2]}) == payload_digest({"b": [1, 2], "a": 1})
    assert payload_digest({"a": 1}) != payload_digest({"a": 2})


def test_invalid_constructor_arguments() -> None:
    with pytest.raises(ValueError):
        ConfirmationManager(ttl_seconds=0)
    with pytest.raises(ValueError):
        ConfirmationManager(max_pending=0)
