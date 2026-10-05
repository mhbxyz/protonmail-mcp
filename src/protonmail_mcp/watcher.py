from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from imapclient.exceptions import IMAPClientError

logger = logging.getLogger("protonmail_mcp.watcher")

DEFAULT_IDLE_TIMEOUT = 5.0
DEFAULT_BACKOFF_INITIAL = 1.0
DEFAULT_BACKOFF_MAX = 60.0

_IGNORED_ERRORS = (OSError, IMAPClientError)


class IdleConnection(Protocol):
    def select_folder(self, folder: str, readonly: bool = False) -> Any: ...

    def idle(self) -> Any: ...

    def idle_check(self, timeout: float | None = None) -> list[Any]: ...

    def idle_done(self) -> Any: ...

    def logout(self) -> Any: ...


class InboxWatcher:
    def __init__(
        self,
        connect: Callable[[], IdleConnection],
        on_event: Callable[[], None],
        *,
        folder: str = "INBOX",
        min_interval_seconds: float = 30.0,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
        backoff_initial: float = DEFAULT_BACKOFF_INITIAL,
        backoff_max: float = DEFAULT_BACKOFF_MAX,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._connect = connect
        self._on_event = on_event
        self._folder = folder
        self._min_interval = float(min_interval_seconds)
        self._idle_timeout = float(idle_timeout)
        self._backoff_initial = float(backoff_initial)
        self._backoff_max = float(backoff_max)
        self._clock = clock
        self._sleep = sleep
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_event: float | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="protonmail-mcp-idle", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self._thread = None

    def _run(self) -> None:
        backoff = self._backoff_initial
        while not self._stop.is_set():
            connection: IdleConnection | None = None
            try:
                connection = self._connect()
                connection.select_folder(self._folder, readonly=True)
                logger.info("watching %s with IDLE", self._folder)
                backoff = self._backoff_initial
                connection.idle()
                try:
                    while not self._stop.is_set():
                        events = connection.idle_check(timeout=self._idle_timeout)
                        for event in events:
                            if isinstance(event, tuple) and event and event[0] == b"EXISTS":
                                self._notify()
                finally:
                    with contextlib.suppress(*_IGNORED_ERRORS):
                        connection.idle_done()
            except _IGNORED_ERRORS as exc:
                if self._stop.is_set():
                    break
                logger.info("IDLE connection lost (%s); retrying in %.0fs", exc, backoff)
                self._sleep(backoff)
                backoff = min(backoff * 2, self._backoff_max)
            finally:
                if connection is not None:
                    with contextlib.suppress(*_IGNORED_ERRORS):
                        connection.logout()

    def _notify(self) -> None:
        now = self._clock()
        if (
            self._min_interval
            and self._last_event is not None
            and now - self._last_event < self._min_interval
        ):
            return
        self._last_event = now
        logger.info("new mail detected in %s", self._folder)
        with contextlib.suppress(Exception):
            self._on_event()
