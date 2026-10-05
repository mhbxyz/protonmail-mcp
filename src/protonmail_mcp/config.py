from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .policy import Policy

DEFAULT_HOST = "127.0.0.1"
DEFAULT_IMAP_PORT = 1143
DEFAULT_SMTP_PORT = 1025
DEFAULT_TIMEOUT = 30.0


class ConfigError(RuntimeError):
    """Bridge connection settings are missing or invalid."""


def _int_env(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _bool_env(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _timeout(env: Mapping[str, str]) -> float:
    raw = env.get("PROTONMAIL_BRIDGE_TIMEOUT", "")
    if not raw:
        return DEFAULT_TIMEOUT
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"PROTONMAIL_BRIDGE_TIMEOUT must be a number, got {raw!r}") from exc


def _imap_security(env: Mapping[str, str]) -> str:
    security = env.get("PROTONMAIL_BRIDGE_IMAP_SECURITY", "starttls").strip().lower()
    if security not in {"starttls", "ssl"}:
        raise ConfigError(
            f"PROTONMAIL_BRIDGE_IMAP_SECURITY must be 'starttls' or 'ssl', got {security!r}"
        )
    return security


def _bridge_options(env: Mapping[str, str]) -> dict[str, Any]:
    return {
        "timeout": _timeout(env),
        "verify_tls": _bool_env(env, "PROTONMAIL_BRIDGE_VERIFY_TLS", False),
        "imap_security": _imap_security(env),
    }


@dataclass(frozen=True, slots=True)
class BridgeConfig:
    host: str
    imap_port: int
    smtp_port: int
    username: str
    password: str = field(repr=False)
    timeout: float
    verify_tls: bool
    imap_security: str = "starttls"

    @classmethod
    def from_env(cls) -> BridgeConfig:
        environment: Mapping[str, str] = os.environ
        username = environment.get("PROTONMAIL_BRIDGE_USERNAME", "").strip()
        password = environment.get("PROTONMAIL_BRIDGE_PASSWORD", "")
        if not username or not password:
            raise ConfigError(
                "PROTONMAIL_BRIDGE_USERNAME and PROTONMAIL_BRIDGE_PASSWORD must be set. "
                "Use your Proton address as username and the mailbox password shown in "
                "the Proton Bridge window as password."
            )
        host = environment.get("PROTONMAIL_BRIDGE_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST
        return cls(
            host=host,
            imap_port=_int_env(environment, "PROTONMAIL_BRIDGE_IMAP_PORT", DEFAULT_IMAP_PORT),
            smtp_port=_int_env(environment, "PROTONMAIL_BRIDGE_SMTP_PORT", DEFAULT_SMTP_PORT),
            username=username,
            password=password,
            **_bridge_options(environment),
        )

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.imap_port}"


def resolve_bridge_config(policy: Policy, env: Mapping[str, str] | None = None) -> BridgeConfig:
    environment: Mapping[str, str] = os.environ if env is None else env
    profile = policy.profiles.get(policy.profile_name)
    if profile is None:
        return BridgeConfig.from_env()
    password = environment.get(profile.password_env, "")
    if not password:
        raise ConfigError(
            f"profile {profile.name!r}: set the {profile.password_env} environment "
            "variable to the Bridge mailbox password"
        )
    return BridgeConfig(
        host=profile.host,
        imap_port=profile.imap_port,
        smtp_port=profile.smtp_port,
        username=profile.username,
        password=password,
        **_bridge_options(environment),
    )
