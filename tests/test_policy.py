from __future__ import annotations

from pathlib import Path

import pytest

from protonmail_mcp.policy import (
    Capabilities,
    PolicyError,
    effective_mode,
    load_policy,
)


def env_for(tmp_path: Path, **overrides: str) -> dict[str, str]:
    env = {"PROTONMAIL_MCP_POLICY": str(tmp_path / "policy.toml")}
    env.update(overrides)
    return env


def write_policy(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "policy.toml"
    path.write_text(text)
    return path


def test_defaults_to_read_only(tmp_path: Path) -> None:
    policy = load_policy(env=env_for(tmp_path))
    assert policy.mode == "read"
    assert policy.capabilities == Capabilities()
    assert policy.capabilities.describe() == "none (read-only)"
    assert policy.confirmation_ttl_seconds == 300
    assert policy.source == "defaults"


def test_env_mode_presets_are_cumulative(tmp_path: Path) -> None:
    draft = load_policy(env=env_for(tmp_path, PROTONMAIL_MCP_MODE="draft"))
    assert draft.capabilities == Capabilities(draft=True)
    organize = load_policy(env=env_for(tmp_path, PROTONMAIL_MCP_MODE="organize"))
    assert organize.capabilities == Capabilities(draft=True, organize=True)
    send = load_policy(env=env_for(tmp_path, PROTONMAIL_MCP_MODE="send"))
    assert send.capabilities == Capabilities(draft=True, organize=True, send=True)
    delete = load_policy(env=env_for(tmp_path, PROTONMAIL_MCP_MODE="delete"))
    assert delete.capabilities == Capabilities(True, True, True, True)


def test_unknown_mode_rejected(tmp_path: Path) -> None:
    with pytest.raises(PolicyError, match="unknown mode"):
        load_policy(env=env_for(tmp_path, PROTONMAIL_MCP_MODE="yolo"))


def test_file_mode_and_source(tmp_path: Path) -> None:
    path = write_policy(tmp_path, '[policy]\nmode = "organize"\n')
    policy = load_policy(env=env_for(tmp_path))
    assert policy.mode == "organize"
    assert policy.capabilities == Capabilities(draft=True, organize=True)
    assert policy.source == str(path)


def test_env_mode_overrides_file(tmp_path: Path) -> None:
    write_policy(tmp_path, '[policy]\nmode = "organize"\n')
    policy = load_policy(env=env_for(tmp_path, PROTONMAIL_MCP_MODE="read"))
    assert policy.mode == "read"
    assert policy.capabilities == Capabilities()


def test_capability_overrides(tmp_path: Path) -> None:
    write_policy(tmp_path, "[capabilities]\nsend = true\n")
    policy = load_policy(env=env_for(tmp_path, PROTONMAIL_MCP_MODE="read"))
    assert policy.capabilities == Capabilities(send=True)
    assert effective_mode(policy.capabilities) == "custom"


def test_unknown_capability_rejected(tmp_path: Path) -> None:
    write_policy(tmp_path, "[capabilities]\ndrafts = true\n")
    with pytest.raises(PolicyError, match="unknown capability"):
        load_policy(env=env_for(tmp_path))


def test_non_boolean_capability_rejected(tmp_path: Path) -> None:
    write_policy(tmp_path, '[capabilities]\ndraft = "yes"\n')
    with pytest.raises(PolicyError, match="must be a boolean"):
        load_policy(env=env_for(tmp_path))


def test_invalid_toml_rejected(tmp_path: Path) -> None:
    write_policy(tmp_path, "this is not toml")
    with pytest.raises(PolicyError, match="invalid TOML"):
        load_policy(env=env_for(tmp_path))


def test_custom_confirmation_ttl(tmp_path: Path) -> None:
    write_policy(tmp_path, "[confirmations]\nttl_seconds = 60\n")
    assert load_policy(env=env_for(tmp_path)).confirmation_ttl_seconds == 60


def test_invalid_confirmation_ttl(tmp_path: Path) -> None:
    write_policy(tmp_path, "[confirmations]\nttl_seconds = 0\n")
    with pytest.raises(PolicyError, match="ttl_seconds"):
        load_policy(env=env_for(tmp_path))
    write_policy(tmp_path, '[confirmations]\nttl_seconds = "soon"\n')
    with pytest.raises(PolicyError, match="ttl_seconds"):
        load_policy(env=env_for(tmp_path))


def test_idempotency_window_default_and_custom(tmp_path: Path) -> None:
    assert load_policy(env=env_for(tmp_path)).idempotency_window_seconds == 300
    write_policy(tmp_path, "[idempotency]\nwindow_seconds = 0\n")
    assert load_policy(env=env_for(tmp_path)).idempotency_window_seconds == 0


def test_invalid_idempotency_window(tmp_path: Path) -> None:
    write_policy(tmp_path, "[idempotency]\nwindow_seconds = -1\n")
    with pytest.raises(PolicyError, match="window_seconds"):
        load_policy(env=env_for(tmp_path))


def test_organize_defaults(tmp_path: Path) -> None:
    policy = load_policy(env=env_for(tmp_path))
    assert policy.organize.max_bulk == 50
    assert policy.organize.protect_drafts is True
    assert policy.organize.allowed_targets == ()
    assert policy.organize.label_allowlist == ()


def test_organize_overrides(tmp_path: Path) -> None:
    write_policy(
        tmp_path,
        "[organize]\n"
        "max_bulk = 5\n"
        "protect_drafts = false\n"
        'allowed_targets = ["Archive"]\n'
        'label_allowlist = ["Labels/Important"]\n',
    )
    policy = load_policy(env=env_for(tmp_path))
    assert policy.organize.max_bulk == 5
    assert policy.organize.protect_drafts is False
    assert policy.organize.allowed_targets == ("Archive",)
    assert policy.organize.label_allowlist == ("Labels/Important",)


def test_invalid_organize_values(tmp_path: Path) -> None:
    write_policy(tmp_path, "[organize]\nmax_bulk = 0\n")
    with pytest.raises(PolicyError, match="max_bulk"):
        load_policy(env=env_for(tmp_path))
    write_policy(tmp_path, '[organize]\nprotect_drafts = "yes"\n')
    with pytest.raises(PolicyError, match="protect_drafts"):
        load_policy(env=env_for(tmp_path))
    write_policy(tmp_path, "[organize]\nallowed_targets = [1]\n")
    with pytest.raises(PolicyError, match="allowed_targets"):
        load_policy(env=env_for(tmp_path))


def test_files_defaults_and_overrides(tmp_path: Path) -> None:
    policy = load_policy(env=env_for(tmp_path))
    assert policy.files.directory == "~/.local/share/protonmail-mcp/files"
    assert policy.files.max_bytes == 25 * 1024 * 1024
    write_policy(tmp_path, '[files]\ndirectory = "sandbox"\nmax_bytes = 1024\n')
    policy = load_policy(env=env_for(tmp_path))
    assert policy.files.directory == "sandbox"
    assert policy.files.max_bytes == 1024


def test_invalid_files_values(tmp_path: Path) -> None:
    write_policy(tmp_path, '[files]\ndirectory = ""\n')
    with pytest.raises(PolicyError, match="directory"):
        load_policy(env=env_for(tmp_path))
    write_policy(tmp_path, "[files]\nmax_bytes = 0\n")
    with pytest.raises(PolicyError, match="max_bytes"):
        load_policy(env=env_for(tmp_path))


def test_effective_mode_for_presets_and_custom() -> None:
    assert effective_mode(Capabilities()) == "read"
    assert effective_mode(Capabilities(draft=True)) == "draft"
    assert effective_mode(Capabilities(draft=True, organize=True)) == "organize"
    assert effective_mode(Capabilities(draft=True, organize=True, send=True)) == "send"
    assert effective_mode(Capabilities(True, True, True, True)) == "delete"
    assert effective_mode(Capabilities(draft=True, delete=True)) == "custom"


def test_enabled_names_and_unknown_capability() -> None:
    capabilities = Capabilities(draft=True, send=True)
    assert capabilities.enabled_names() == ["draft", "send"]
    assert capabilities.enabled("draft") is True
    with pytest.raises(ValueError):
        capabilities.enabled("nope")
