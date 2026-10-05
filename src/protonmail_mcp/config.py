from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_HOST = "127.0.0.1"
DEFAULT_IMAP_PORT = 1143
DEFAULT_SMTP_PORT = 1025
DEFAULT_TIMEOUT = 30.0


class ConfigError(RuntimeError):
    """Bridge connection settings are missing or invalid."""


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class BridgeConfig:
    host: str
    imap_port: int
    smtp_port: int
    username: str
    password: str
    timeout: float
    verify_tls: bool
    imap_security: str = "starttls"

    @classmethod
    def from_env(cls) -> BridgeConfig:
        username = os.environ.get("PROTONMAIL_BRIDGE_USERNAME", "").strip()
        password = os.environ.get("PROTONMAIL_BRIDGE_PASSWORD", "")
        if not username or not password:
            raise ConfigError(
                "PROTONMAIL_BRIDGE_USERNAME and PROTONMAIL_BRIDGE_PASSWORD must be set. "
                "Use your Proton address as username and the mailbox password shown in "
                "the Proton Bridge window as password."
            )
        timeout_raw = os.environ.get("PROTONMAIL_BRIDGE_TIMEOUT", "")
        try:
            timeout = float(timeout_raw) if timeout_raw else DEFAULT_TIMEOUT
        except ValueError as exc:
            raise ConfigError(f"PROTONMAIL_BRIDGE_TIMEOUT must be a number, got {timeout_raw!r}") from exc
        security = os.environ.get("PROTONMAIL_BRIDGE_IMAP_SECURITY", "starttls").strip().lower()
        if security not in {"starttls", "ssl"}:
            raise ConfigError(
                f"PROTONMAIL_BRIDGE_IMAP_SECURITY must be 'starttls' or 'ssl', got {security!r}"
            )
        return cls(
            host=os.environ.get("PROTONMAIL_BRIDGE_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST,
            imap_port=_int_env("PROTONMAIL_BRIDGE_IMAP_PORT", DEFAULT_IMAP_PORT),
            smtp_port=_int_env("PROTONMAIL_BRIDGE_SMTP_PORT", DEFAULT_SMTP_PORT),
            username=username,
            password=password,
            timeout=timeout,
            verify_tls=_bool_env("PROTONMAIL_BRIDGE_VERIFY_TLS", False),
            imap_security=security,
        )

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.imap_port}"
