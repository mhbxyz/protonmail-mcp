from __future__ import annotations

import json
from datetime import UTC, datetime

from protonmail_mcp.audit import AuditLog, args_digest


def fixed_clock() -> datetime:
    return datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def test_record_stores_digest_but_not_raw_args(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path, clock=fixed_clock)
    log.record("send_email", outcome="ok", duration_ms=12, args={"body": "TOP SECRET BODY"})
    content = path.read_text()
    assert "TOP SECRET BODY" not in content
    entry = json.loads(content.strip())
    assert entry["tool"] == "send_email"
    assert entry["outcome"] == "ok"
    assert entry["duration_ms"] == 12
    assert entry["args_digest"] == args_digest({"body": "TOP SECRET BODY"})
    assert entry["ts"].startswith("2026-10-05T12:00")


def test_record_includes_error_type(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    AuditLog(path, clock=fixed_clock).record(
        "read_email", outcome="error", duration_ms=3, error="MailboxError"
    )
    entry = json.loads(path.read_text().strip())
    assert entry["error"] == "MailboxError"


def test_rotation(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path, max_bytes=10, clock=fixed_clock)
    log.record("a", outcome="ok", duration_ms=0)
    log.record("b", outcome="ok", duration_ms=0)
    assert path.exists()
    assert (tmp_path / "audit.jsonl.1").exists()


def test_disabled_log_is_noop() -> None:
    log = AuditLog(None)
    assert log.enabled is False
    log.record("x", outcome="ok", duration_ms=0)


def test_unwritable_path_never_raises() -> None:
    log = AuditLog("/proc/definitely/not/writable/audit.jsonl")
    log.record("x", outcome="ok", duration_ms=0)


def test_from_env_disabled_and_custom() -> None:
    assert AuditLog.from_env({"PROTONMAIL_MCP_AUDIT_LOG": ""}).enabled is False
    assert AuditLog.from_env({"PROTONMAIL_MCP_AUDIT_LOG": "logs/audit.jsonl"}).enabled is True
