from __future__ import annotations

import pytest

from protonmail_mcp.config import BridgeConfig, ConfigError


def test_from_env_requires_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROTONMAIL_BRIDGE_USERNAME", raising=False)
    monkeypatch.delenv("PROTONMAIL_BRIDGE_PASSWORD", raising=False)
    with pytest.raises(ConfigError):
        BridgeConfig.from_env()


def test_from_env_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROTONMAIL_BRIDGE_USERNAME", "me@proton.me")
    monkeypatch.setenv("PROTONMAIL_BRIDGE_PASSWORD", "secret")
    monkeypatch.delenv("PROTONMAIL_BRIDGE_IMAP_PORT", raising=False)
    config = BridgeConfig.from_env()
    assert config.host == "127.0.0.1"
    assert config.imap_port == 1143
    assert config.verify_tls is False
    assert config.endpoint == "127.0.0.1:1143"


def test_from_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROTONMAIL_BRIDGE_USERNAME", "me@proton.me")
    monkeypatch.setenv("PROTONMAIL_BRIDGE_PASSWORD", "secret")
    monkeypatch.setenv("PROTONMAIL_BRIDGE_HOST", "bridge.local")
    monkeypatch.setenv("PROTONMAIL_BRIDGE_IMAP_PORT", "2143")
    monkeypatch.setenv("PROTONMAIL_BRIDGE_VERIFY_TLS", "true")
    config = BridgeConfig.from_env()
    assert config.host == "bridge.local"
    assert config.imap_port == 2143
    assert config.verify_tls is True


def test_from_env_invalid_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROTONMAIL_BRIDGE_USERNAME", "me@proton.me")
    monkeypatch.setenv("PROTONMAIL_BRIDGE_PASSWORD", "secret")
    monkeypatch.setenv("PROTONMAIL_BRIDGE_IMAP_PORT", "abc")
    with pytest.raises(ConfigError):
        BridgeConfig.from_env()
