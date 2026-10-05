from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_POLICY_PATH = "~/.config/protonmail-mcp/policy.toml"
DEFAULT_CONFIRMATION_TTL_SECONDS = 300
DEFAULT_IDEMPOTENCY_WINDOW_SECONDS = 300
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
class FilesPolicy:
    directory: str = "~/.local/share/protonmail-mcp/files"
    max_bytes: int = 25 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SendPolicy:
    allow_self: bool = True
    allowed_recipients: tuple[str, ...] = ()
    allowed_domains: tuple[str, ...] = ()
    max_recipients: int = 10
    max_per_hour: int = 20
    max_per_day: int = 100
    duplicate_window_seconds: int = 600
    max_thread_depth: int = 10
    max_message_bytes: int = 25 * 1024 * 1024
    state_path: str = "~/.local/state/protonmail-mcp/state.db"


@dataclass(frozen=True, slots=True)
class IndexPolicy:
    enabled: bool = False
    path: str = "~/.local/state/protonmail-mcp/index.db"
    excluded_folders: tuple[str, ...] = ()
    max_body_chars: int = 10000


@dataclass(frozen=True, slots=True)
class NotificationsPolicy:
    enabled: bool = False
    folder: str = "INBOX"
    min_interval_seconds: int = 30


@dataclass(frozen=True, slots=True)
class ProfilePolicy:
    name: str
    username: str
    password_env: str
    host: str = "127.0.0.1"
    imap_port: int = 1143
    smtp_port: int = 1025
    mode: str | None = None


@dataclass(frozen=True, slots=True)
class OrganizePolicy:
    max_bulk: int = 50
    protect_drafts: bool = True
    allowed_targets: tuple[str, ...] = ()
    label_allowlist: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Policy:
    mode: str
    capabilities: Capabilities
    confirmation_ttl_seconds: int
    source: str
    idempotency_window_seconds: int = DEFAULT_IDEMPOTENCY_WINDOW_SECONDS
    organize: OrganizePolicy = OrganizePolicy()
    files: FilesPolicy = FilesPolicy()
    send: SendPolicy = SendPolicy()
    index: IndexPolicy = IndexPolicy()
    notifications: NotificationsPolicy = NotificationsPolicy()
    profile_name: str = "default"
    profiles: dict[str, ProfilePolicy] = field(default_factory=dict)


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


def _parse_profiles(data: dict[str, Any]) -> dict[str, ProfilePolicy]:
    profiles: dict[str, ProfilePolicy] = {}
    for name, raw in _table(data, "profiles").items():
        if not isinstance(raw, dict):
            raise PolicyError(f"policy.toml: profiles.{name} must be a table")
        username = raw.get("username", "")
        if not isinstance(username, str) or not username.strip():
            raise PolicyError(
                f"policy.toml: profiles.{name}.username must be a non-empty string"
            )
        password_env = raw.get("password_env", "")
        if not isinstance(password_env, str) or not password_env.strip():
            raise PolicyError(
                f"policy.toml: profiles.{name}.password_env must be a non-empty string"
            )
        host = raw.get("host", "127.0.0.1")
        if not isinstance(host, str) or not host.strip():
            raise PolicyError(f"policy.toml: profiles.{name}.host must be a non-empty string")
        ports: dict[str, int] = {}
        for port_name, default_port in (("imap_port", 1143), ("smtp_port", 1025)):
            value = raw.get(port_name, default_port)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= 65535
            ):
                raise PolicyError(
                    f"policy.toml: profiles.{name}.{port_name} must be an integer "
                    "between 1 and 65535"
                )
            ports[port_name] = value
        profile_mode = raw.get("mode")
        if profile_mode is not None and (
            not isinstance(profile_mode, str) or profile_mode not in _MODE_PRESETS
        ):
            raise PolicyError(
                f"policy.toml: profiles.{name}.mode must be one of: {', '.join(MODES)}"
            )
        profiles[name] = ProfilePolicy(
            name=name,
            username=username.strip(),
            password_env=password_env.strip(),
            host=host.strip(),
            imap_port=ports["imap_port"],
            smtp_port=ports["smtp_port"],
            mode=profile_mode,
        )
    return profiles


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

    env_mode = environment.get("PROTONMAIL_MCP_MODE")
    mode = env_mode or file_mode or "read"
    if mode not in _MODE_PRESETS:
        raise PolicyError(f"unknown mode {mode!r}; expected one of: {', '.join(MODES)}")
    global_explicit = env_mode is not None or file_mode is not None

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

    profiles = _parse_profiles(data)
    profile_name = "default"
    if profiles:
        file_default = policy_table.get("default_profile")
        if file_default is not None and (
            not isinstance(file_default, str) or not file_default.strip()
        ):
            raise PolicyError("policy.toml: policy.default_profile must be a non-empty string")
        requested = (environment.get("PROTONMAIL_MCP_PROFILE") or file_default or "").strip()
        if not requested:
            raise PolicyError(
                "policy.toml defines profiles; set policy.default_profile or "
                f"PROTONMAIL_MCP_PROFILE to one of: {', '.join(sorted(profiles))}"
            )
        if requested not in profiles:
            raise PolicyError(
                f"unknown profile {requested!r}; available: {', '.join(sorted(profiles))}"
            )
        profile_name = requested
        active_profile = profiles[requested]
        if active_profile.mode:
            profile_flags = _MODE_PRESETS[active_profile.mode]
            if global_explicit:
                flags = {
                    capability: flags.get(capability, False)
                    and profile_flags.get(capability, False)
                    for capability in CAPABILITY_NAMES
                }
            else:
                flags = {
                    capability: profile_flags.get(capability, False)
                    and overrides.get(capability, True)
                    for capability in CAPABILITY_NAMES
                }

    confirmations = _table(data, "confirmations")
    ttl = confirmations.get("ttl_seconds", DEFAULT_CONFIRMATION_TTL_SECONDS)
    if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0:
        raise PolicyError("policy.toml: confirmations.ttl_seconds must be a positive integer")

    idempotency = _table(data, "idempotency")
    window = idempotency.get("window_seconds", DEFAULT_IDEMPOTENCY_WINDOW_SECONDS)
    if isinstance(window, bool) or not isinstance(window, int) or window < 0:
        raise PolicyError(
            "policy.toml: idempotency.window_seconds must be a non-negative integer"
        )

    organize = _table(data, "organize")
    max_bulk = organize.get("max_bulk", 50)
    if isinstance(max_bulk, bool) or not isinstance(max_bulk, int) or max_bulk <= 0:
        raise PolicyError("policy.toml: organize.max_bulk must be a positive integer")
    protect_drafts = organize.get("protect_drafts", True)
    if not isinstance(protect_drafts, bool):
        raise PolicyError("policy.toml: organize.protect_drafts must be a boolean")
    targets = organize.get("allowed_targets", [])
    labels = organize.get("label_allowlist", [])
    for name, value in (("allowed_targets", targets), ("label_allowlist", labels)):
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item for item in value
        ):
            raise PolicyError(
                f"policy.toml: organize.{name} must be a list of non-empty strings"
            )

    files_table = _table(data, "files")
    directory = files_table.get("directory", FilesPolicy().directory)
    if not isinstance(directory, str) or not directory.strip():
        raise PolicyError("policy.toml: files.directory must be a non-empty string")
    max_bytes = files_table.get("max_bytes", FilesPolicy().max_bytes)
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise PolicyError("policy.toml: files.max_bytes must be a positive integer")

    send_table = _table(data, "send")

    def send_int(name: str, default: int, *, minimum: int) -> int:
        value = send_table.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise PolicyError(
                f"policy.toml: send.{name} must be an integer >= {minimum}"
            )
        return value

    allow_self = send_table.get("allow_self", True)
    if not isinstance(allow_self, bool):
        raise PolicyError("policy.toml: send.allow_self must be a boolean")
    recipients = send_table.get("allowed_recipients", [])
    domains = send_table.get("allowed_domains", [])
    for name, value in (("allowed_recipients", recipients), ("allowed_domains", domains)):
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item for item in value
        ):
            raise PolicyError(
                f"policy.toml: send.{name} must be a list of non-empty strings"
            )
    state_path = send_table.get("state_path", SendPolicy().state_path)
    if not isinstance(state_path, str) or not state_path.strip():
        raise PolicyError("policy.toml: send.state_path must be a non-empty string")

    index_table = _table(data, "index")
    index_enabled = index_table.get("enabled", False)
    if not isinstance(index_enabled, bool):
        raise PolicyError("policy.toml: index.enabled must be a boolean")
    index_path = index_table.get("path", IndexPolicy().path)
    if not isinstance(index_path, str) or not index_path.strip():
        raise PolicyError("policy.toml: index.path must be a non-empty string")
    excluded = index_table.get("excluded_folders", [])
    if not isinstance(excluded, list) or any(
        not isinstance(item, str) or not item for item in excluded
    ):
        raise PolicyError("policy.toml: index.excluded_folders must be a list of non-empty strings")
    max_body_chars = index_table.get("max_body_chars", IndexPolicy().max_body_chars)
    if (
        isinstance(max_body_chars, bool)
        or not isinstance(max_body_chars, int)
        or max_body_chars <= 0
    ):
        raise PolicyError("policy.toml: index.max_body_chars must be a positive integer")

    notifications_table = _table(data, "notifications")
    notifications_enabled = notifications_table.get("enabled", False)
    if not isinstance(notifications_enabled, bool):
        raise PolicyError("policy.toml: notifications.enabled must be a boolean")
    notifications_folder = notifications_table.get("folder", NotificationsPolicy().folder)
    if not isinstance(notifications_folder, str) or not notifications_folder.strip():
        raise PolicyError("policy.toml: notifications.folder must be a non-empty string")
    min_interval = notifications_table.get(
        "min_interval_seconds", NotificationsPolicy().min_interval_seconds
    )
    if (
        isinstance(min_interval, bool)
        or not isinstance(min_interval, int)
        or min_interval < 0
    ):
        raise PolicyError(
            "policy.toml: notifications.min_interval_seconds must be a non-negative integer"
        )

    return Policy(
        mode=mode,
        capabilities=Capabilities(**flags),
        confirmation_ttl_seconds=ttl,
        source=str(path) if exists else "defaults",
        idempotency_window_seconds=window,
        organize=OrganizePolicy(
            max_bulk=max_bulk,
            protect_drafts=protect_drafts,
            allowed_targets=tuple(targets),
            label_allowlist=tuple(labels),
        ),
        files=FilesPolicy(directory=directory, max_bytes=max_bytes),
        send=SendPolicy(
            allow_self=allow_self,
            allowed_recipients=tuple(recipients),
            allowed_domains=tuple(domains),
            max_recipients=send_int("max_recipients", SendPolicy().max_recipients, minimum=1),
            max_per_hour=send_int("max_per_hour", SendPolicy().max_per_hour, minimum=0),
            max_per_day=send_int("max_per_day", SendPolicy().max_per_day, minimum=0),
            duplicate_window_seconds=send_int(
                "duplicate_window_seconds", SendPolicy().duplicate_window_seconds, minimum=0
            ),
            max_thread_depth=send_int(
                "max_thread_depth", SendPolicy().max_thread_depth, minimum=1
            ),
            max_message_bytes=send_int(
                "max_message_bytes", SendPolicy().max_message_bytes, minimum=1
            ),
            state_path=state_path,
        ),
        index=IndexPolicy(
            enabled=index_enabled,
            path=index_path,
            excluded_folders=tuple(excluded),
            max_body_chars=max_body_chars,
        ),
        notifications=NotificationsPolicy(
            enabled=notifications_enabled,
            folder=notifications_folder.strip(),
            min_interval_seconds=min_interval,
        ),
        profile_name=profile_name,
        profiles=profiles,
    )
