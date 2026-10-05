from __future__ import annotations

import os
import socket

import pytest

os.environ.setdefault("PROTONMAIL_MCP_AUDIT_LOG", "")

from protonmail_mcp import server as server_module  # noqa: E402


@pytest.fixture(autouse=True)
def reset_server_state() -> None:
    server_module.CONFIRMATIONS.reset()
    server_module.IDEMPOTENCY.clear()
    server_module.JOURNAL.clear()
    server_module.set_smtp_sender(None)


@pytest.fixture(autouse=True)
def block_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    real_connect = socket.socket.connect

    def guarded(self: socket.socket, address: object) -> object:
        if self.family == socket.AF_UNIX:
            return real_connect(self, address)
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise AssertionError(f"external network blocked in tests: {address!r}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)
