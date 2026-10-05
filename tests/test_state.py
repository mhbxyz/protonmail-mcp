from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from protonmail_mcp.policy import SendPolicy
from protonmail_mcp.state import SendState


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def make_state(tmp_path: Path, clock: FakeClock | None = None) -> SendState:
    return SendState(tmp_path / "state.db", clock=clock or FakeClock())


def test_quota_status_and_hour_window(tmp_path: Path) -> None:
    clock = FakeClock()
    state = make_state(tmp_path, clock)
    policy = SendPolicy(max_per_hour=2, max_per_day=5)
    ok, reason = state.can_send(policy)
    assert ok
    assert reason == ""
    state.record_send("<a@x>")
    state.record_send("<b@x>")
    ok, reason = state.can_send(policy)
    assert not ok
    assert "hourly" in reason
    clock.now += timedelta(hours=2)
    assert state.can_send(policy)[0]
    state.close()


def test_daily_quota(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    policy = SendPolicy(max_per_hour=100, max_per_day=1)
    state.record_send("<a@x>")
    ok, reason = state.can_send(policy)
    assert not ok
    assert "daily" in reason
    state.close()


def test_quota_status_reports_remaining(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    policy = SendPolicy(max_per_hour=3, max_per_day=10)
    state.record_send("<a@x>")
    status = state.quota_status(policy)
    assert status == {
        "hour_count": 1,
        "day_count": 1,
        "hour_remaining": 2,
        "day_remaining": 9,
    }
    state.close()


def test_persistence_across_instances(tmp_path: Path) -> None:
    clock = FakeClock()
    first = make_state(tmp_path, clock)
    first.record_send("<a@x>")
    first.close()
    second = make_state(tmp_path, clock)
    assert second.count_since(clock.now - timedelta(hours=1)) == 1
    second.close()


def test_idempotency_window(tmp_path: Path) -> None:
    clock = FakeClock()
    state = make_state(tmp_path, clock)
    assert not state.is_duplicate("key", 600)
    state.remember_key("key")
    assert state.is_duplicate("key", 600)
    clock.now += timedelta(seconds=601)
    assert not state.is_duplicate("key", 600)
    state.close()


def test_body_hash_window(tmp_path: Path) -> None:
    clock = FakeClock()
    state = make_state(tmp_path, clock)
    state.remember_body("hash")
    assert state.body_seen("hash", 600)
    assert not state.body_seen("hash", 0)
    clock.now += timedelta(seconds=601)
    assert not state.body_seen("hash", 600)
    state.close()


def test_prune_removes_old_rows(tmp_path: Path) -> None:
    clock = FakeClock()
    state = make_state(tmp_path, clock)
    state.record_send("<old@x>")
    clock.now += timedelta(days=8)
    state.record_send("<new@x>")
    assert state.count_since(clock.now - timedelta(days=30)) == 1
    state.close()
