from __future__ import annotations

import pytest

from protonmail_mcp.idempotency import DraftReference, IdempotencyStore


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def reference(uid: int = 42) -> DraftReference:
    return DraftReference(uid=uid, message_id=f"<draft-{uid}@test>", folder="Drafts", subject="x")


def test_remember_and_lookup() -> None:
    store = IdempotencyStore(clock=FakeClock())
    assert store.lookup("k") is None
    store.remember("k", reference())
    assert store.lookup("k") == reference()


def test_expiry() -> None:
    clock = FakeClock()
    store = IdempotencyStore(window_seconds=10, clock=clock)
    store.remember("k", reference())
    clock.now = 10.0
    assert store.lookup("k") is None


def test_disabled_window_never_stores() -> None:
    store = IdempotencyStore(window_seconds=0)
    assert store.enabled is False
    store.remember("k", reference())
    assert store.lookup("k") is None


def test_forget_and_clear() -> None:
    store = IdempotencyStore(clock=FakeClock())
    store.remember("a", reference(1))
    store.remember("b", reference(2))
    store.forget("a")
    assert store.lookup("a") is None
    assert store.lookup("b") is not None
    store.clear()
    assert store.lookup("b") is None


def test_max_entries_evicts_oldest() -> None:
    clock = FakeClock()
    store = IdempotencyStore(window_seconds=100, max_entries=1, clock=clock)
    store.remember("a", reference(1))
    clock.now = 1.0
    store.remember("b", reference(2))
    assert store.lookup("a") is None
    assert store.lookup("b") is not None


def test_invalid_arguments() -> None:
    with pytest.raises(ValueError):
        IdempotencyStore(window_seconds=-1)
    with pytest.raises(ValueError):
        IdempotencyStore(max_entries=0)
