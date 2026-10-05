from __future__ import annotations

import pytest

from protonmail_mcp.config import BridgeConfig, ConfigError, resolve_bridge_config
from protonmail_mcp.policy import Capabilities, Policy, ProfilePolicy


def test_from_env_requires_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROTONMAIL_BRIDGE_USERNAME", raising=False)
    monkeypatch.delenv("PROTONMAIL_BRIDGE_PASSWORD", raising=False)
    with pytest.raises(ConfigError):
        BridgeConfig.from_env()


def test_from_env_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROTONMAIL_BRIDGE_USERNAME", "me@proton.me")
    monkeypatch.setenv("PROTONMAIL_BRIDGE_PASSWORD", "secret")
    monkeypatch.delenv("PROTONMAIL_BRIDGE_IMAP_PORT", raising=False)
    monkeypatch.delenv("PROTONMAIL_BRIDGE_IMAP_SECURITY", raising=False)
    config = BridgeConfig.from_env()
    assert config.host == "127.0.0.1"
    assert config.imap_port == 1143
    assert config.verify_tls is False
    assert config.imap_security == "starttls"
    assert config.endpoint == "127.0.0.1:1143"


def test_from_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROTONMAIL_BRIDGE_USERNAME", "me@proton.me")
    monkeypatch.setenv("PROTONMAIL_BRIDGE_PASSWORD", "secret")
    monkeypatch.setenv("PROTONMAIL_BRIDGE_HOST", "bridge.local")
    monkeypatch.setenv("PROTONMAIL_BRIDGE_IMAP_PORT", "2143")
    monkeypatch.setenv("PROTONMAIL_BRIDGE_VERIFY_TLS", "true")
    monkeypatch.setenv("PROTONMAIL_BRIDGE_IMAP_SECURITY", "ssl")
    config = BridgeConfig.from_env()
    assert config.host == "bridge.local"
    assert config.imap_port == 2143
    assert config.verify_tls is True
    assert config.imap_security == "ssl"


def test_from_env_invalid_security(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROTONMAIL_BRIDGE_USERNAME", "me@proton.me")
    monkeypatch.setenv("PROTONMAIL_BRIDGE_PASSWORD", "secret")
    monkeypatch.setenv("PROTONMAIL_BRIDGE_IMAP_SECURITY", "plain")
    with pytest.raises(ConfigError):
        BridgeConfig.from_env()


def test_from_env_invalid_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROTONMAIL_BRIDGE_USERNAME", "me@proton.me")
    monkeypatch.setenv("PROTONMAIL_BRIDGE_PASSWORD", "secret")
    monkeypatch.setenv("PROTONMAIL_BRIDGE_IMAP_PORT", "abc")
    with pytest.raises(ConfigError):
        BridgeConfig.from_env()


def test_repr_does_not_leak_password() -> None:
    config = BridgeConfig(
        host="127.0.0.1",
        imap_port=1143,
        smtp_port=1025,
        username="me@proton.me",
        password="SUPER_SECRET",
        timeout=30.0,
        verify_tls=False,
    )
    assert "SUPER_SECRET" not in repr(config)
    assert "SUPER_SECRET" not in str(config)


def profile_policy() -> Policy:
    return Policy(
        mode="read",
        capabilities=Capabilities(),
        confirmation_ttl_seconds=300,
        source="test",
        profile_name="work",
        profiles={
            "work": ProfilePolicy(
                name="work",
                username="work@proton.me",
                password_env="PW_WORK",
                imap_port=2143,
                smtp_port=2025,
            )
        },
    )


def test_resolve_bridge_config_from_profile() -> None:
    config = resolve_bridge_config(profile_policy(), env={"PW_WORK": "secret"})
    assert config.username == "work@proton.me"
    assert config.imap_port == 2143
    assert config.smtp_port == 2025
    assert config.password == "secret"


def test_resolve_bridge_config_missing_profile_password() -> None:
    with pytest.raises(ConfigError, match="PW_WORK"):
        resolve_bridge_config(profile_policy(), env={})


def test_resolve_bridge_config_legacy_without_profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROTONMAIL_BRIDGE_USERNAME", "me@proton.me")
    monkeypatch.setenv("PROTONMAIL_BRIDGE_PASSWORD", "secret")
    policy = Policy(
        mode="read",
        capabilities=Capabilities(),
        confirmation_ttl_seconds=300,
        source="test",
    )
    config = resolve_bridge_config(policy)
    assert config.username == "me@proton.me"
    assert config.imap_port == 1143
