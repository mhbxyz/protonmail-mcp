from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_POLICY_PATH = "~/.config/protonmail-mcp/policy.toml"
DEFAULT_CONFIRMATION_TTL_SECONDS = 300
MODES = ("read", "draft", "organize", "send", "delete")
CAPABILITY_NAMES = ("draft", "organize", "send", "delete")

_MODE_PRESETS: dict[str, dict[str, bool]] = {
    "read": {},
    "draft": {"draft": True},
    "organize": {"draft": True, "organize": True},
    "send": {"draft": True, "organize": True, "send": True},
    "delete": {"draft": True, "organize": True, "send": True, "delete": True},
}


class PolicyError(RuntimeError):
    """The policy configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class Capabilities:
    draft: bool = False
    organize: bool = False
    send: bool = False
    delete: bool = False

    def enabled(self, name: str) -> bool:
        if name not in CAPABILITY_NAMES:
            raise ValueError(f"unknown capability {name!r}")
        return bool(getattr(self, name))

    def enabled_names(self) -> list[str]:
        return [name for name in CAPABILITY_NAMES if getattr(self, name)]

    def describe(self) -> str:
        names = self.enabled_names()
        return ", ".join(names) if names else "none (read-only)"


@dataclass(frozen=True, slots=True)
class Policy:
    mode: str
    capabilities: Capabilities
    confirmation_ttl_seconds: int
    source: str


def effective_mode(capabilities: Capabilities) -> str:
    for mode in reversed(MODES):
        if Capabilities(**_MODE_PRESETS[mode]) == capabilities:
            return mode
    return "custom"


def _read_policy_file(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise PolicyError(f"{path}: invalid TOML: {exc}") from exc
    except OSError as exc:
        raise PolicyError(f"{path}: cannot read policy file: {exc}") from exc


def _table(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise PolicyError(f"policy.toml: [{name}] must be a table")
    return value


def load_policy(env: dict[str, str] | None = None) -> Policy:
    environment = os.environ if env is None else env
    raw_path = environment.get("PROTONMAIL_MCP_POLICY", DEFAULT_POLICY_PATH)
    path = Path(os.path.expanduser(raw_path))

    data: dict[str, Any] = {}
    exists = path.exists()
    if exists:
        data = _read_policy_file(path)

    policy_table = _table(data, "policy")
    file_mode = policy_table.get("mode")
    if file_mode is not None and not isinstance(file_mode, str):
        raise PolicyError("policy.toml: policy.mode must be a string")

    mode = environment.get("PROTONMAIL_MCP_MODE", file_mode or "read")
    if mode not in _MODE_PRESETS:
        raise PolicyError(f"unknown mode {mode!r}; expected one of: {', '.join(MODES)}")

    flags = dict(_MODE_PRESETS[mode])
    overrides = _table(data, "capabilities")
    for name, value in overrides.items():
        if name not in CAPABILITY_NAMES:
            raise PolicyError(
                f"policy.toml: unknown capability {name!r}; expected one of: {', '.join(CAPABILITY_NAMES)}"
            )
        if not isinstance(value, bool):
            raise PolicyError(f"policy.toml: capability {name!r} must be a boolean")
        flags[name] = value

    confirmations = _table(data, "confirmations")
    ttl = confirmations.get("ttl_seconds", DEFAULT_CONFIRMATION_TTL_SECONDS)
    if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0:
        raise PolicyError("policy.toml: confirmations.ttl_seconds must be a positive integer")

    return Policy(
        mode=mode,
        capabilities=Capabilities(**flags),
        confirmation_ttl_seconds=ttl,
        source=str(path) if exists else "defaults",
    )
