from __future__ import annotations

import contextlib
import hashlib
import json
import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_AUDIT_PATH = "~/.local/state/protonmail-mcp/audit.jsonl"
DEFAULT_MAX_BYTES = 5 * 1024 * 1024


def args_digest(args: dict[str, Any]) -> str:
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AuditLog:
    def __init__(
        self,
        path: Path | str | None,
        max_bytes: int = DEFAULT_MAX_BYTES,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._path = Path(path).expanduser() if path else None
        self._max_bytes = max_bytes
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> AuditLog:
        environment = os.environ if env is None else env
        raw = environment.get("PROTONMAIL_MCP_AUDIT_LOG", DEFAULT_AUDIT_PATH)
        return cls(raw or None)

    @property
    def enabled(self) -> bool:
        return self._path is not None

    def record(
        self,
        tool: str,
        *,
        outcome: str,
        duration_ms: int,
        args: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        if self._path is None:
            return
        entry: dict[str, Any] = {
            "ts": self._clock().isoformat(),
            "tool": tool,
            "outcome": outcome,
            "duration_ms": duration_ms,
            "args_digest": args_digest(args or {}),
        }
        if error:
            entry["error"] = error
        line = json.dumps(entry, separators=(",", ":"))
        try:
            with self._lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._rotate_if_needed()
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except OSError:
            pass

    def _rotate_if_needed(self) -> None:
        if self._path is None:
            return
        try:
            if self._path.stat().st_size < self._max_bytes:
                return
        except FileNotFoundError:
            return
        rotated = self._path.with_name(self._path.name + ".1")
        with contextlib.suppress(OSError):
            rotated.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            self._path.rename(rotated)
