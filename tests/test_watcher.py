from __future__ import annotations

import time
from collections.abc import Callable

from imapclient.exceptions import IMAPClientError

from protonmail_mcp.watcher import InboxWatcher


def wait_for(predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class FakeConnection:
    def __init__(self, events=None, fail_after=None) -> None:
        self.events = list(events or [])
        self.fail_after = fail_after
        self.checks = 0
        self.selected: tuple[str, bool] | None = None
        self.idle_started = False
        self.idle_done_called = False
        self.logged_out = False

    def select_folder(self, folder: str, readonly: bool = False) -> dict[str, object]:
        self.selected = (folder, readonly)
        return {}

    def idle(self) -> None:
        self.idle_started = True

    def idle_check(self, timeout: float | None = None) -> list[object]:
        self.checks += 1
        if self.fail_after is not None and self.checks >= self.fail_after:
            raise IMAPClientError("connection lost")
        time.sleep(min(timeout or 0, 0.005))
        if self.events:
            return self.events.pop(0)
        return []

    def idle_done(self) -> None:
        self.idle_done_called = True

    def logout(self) -> None:
        self.logged_out = True


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_detects_new_mail_and_stops() -> None:
    connection = FakeConnection([[(b"EXISTS", 1)]])
    events: list[int] = []
    watcher = InboxWatcher(lambda: connection, lambda: events.append(1), idle_timeout=0.01)
    watcher.start()
    try:
        assert wait_for(lambda: bool(events))
    finally:
        watcher.stop()
    assert connection.selected == ("INBOX", True)
    assert connection.idle_started
    assert connection.idle_done_called
    assert connection.logged_out
    assert not watcher.running


def test_coalesces_events_within_min_interval() -> None:
    connection = FakeConnection([[(b"EXISTS", 1)], [(b"EXISTS", 2)]])
    events: list[int] = []
    watcher = InboxWatcher(
        lambda: connection,
        lambda: events.append(1),
        min_interval_seconds=10,
        idle_timeout=0.01,
        clock=FakeClock(),
    )
    watcher.start()
    try:
        assert wait_for(lambda: connection.checks >= 3)
    finally:
        watcher.stop()
    assert len(events) == 1


def test_notifies_again_after_interval() -> None:
    connection = FakeConnection([[(b"EXISTS", 1)], [(b"EXISTS", 2)]])
    events: list[int] = []
    clock = FakeClock()
    watcher = InboxWatcher(
        lambda: connection,
        lambda: events.append(1),
        min_interval_seconds=10,
        idle_timeout=0.01,
        clock=clock,
    )
    watcher.start()
    try:
        assert wait_for(lambda: len(events) == 1)
        clock.now = 11.0
        assert wait_for(lambda: len(events) == 2)
    finally:
        watcher.stop()


def test_reconnects_with_backoff_after_failure() -> None:
    failing = FakeConnection(fail_after=1)
    good = FakeConnection([[(b"EXISTS", 1)]])
    connections = [failing, good]
    events: list[int] = []
    sleeps: list[float] = []
    watcher = InboxWatcher(
        lambda: connections.pop(0),
        lambda: events.append(1),
        idle_timeout=0.01,
        backoff_initial=0.5,
        sleep=sleeps.append,
    )
    watcher.start()
    try:
        assert wait_for(lambda: bool(events))
    finally:
        watcher.stop()
    assert failing.logged_out
    assert good.logged_out
    assert sleeps == [0.5]


def test_callback_errors_do_not_kill_the_watcher() -> None:
    connection = FakeConnection([[(b"EXISTS", 1)]])

    def failing_callback() -> None:
        raise RuntimeError("boom")

    watcher = InboxWatcher(lambda: connection, failing_callback, idle_timeout=0.01)
    watcher.start()
    try:
        assert wait_for(lambda: connection.checks >= 3)
        assert watcher.running
    finally:
        watcher.stop()
