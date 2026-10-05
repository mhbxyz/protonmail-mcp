from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .policy import Policy

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_UNSAFE = re.compile(r"[^A-Za-z0-9._()\[\] -]+")


class SandboxError(RuntimeError):
    """A file cannot be written inside the sandbox."""


@dataclass(frozen=True, slots=True)
class StoredFile:
    filename: str
    path: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class SandboxFile:
    filename: str
    data: bytes
    size_bytes: int


def read_from_sandbox(directory: Path, name: str, *, max_bytes: int) -> SandboxFile:
    root = directory.resolve()
    target = (root / safe_filename(name)).resolve()
    if not target.is_relative_to(root):
        raise SandboxError("refusing to read outside the sandbox")
    if not target.is_file():
        raise SandboxError(f"file not found in the sandbox: {target.name!r}")
    data = target.read_bytes()
    if len(data) > max_bytes:
        raise SandboxError(f"file is too large ({len(data)} bytes; limit {max_bytes})")
    return SandboxFile(filename=target.name, data=data, size_bytes=len(data))


def sandbox_directory(policy: Policy) -> Path:
    return Path(policy.files.directory).expanduser()


def safe_filename(name: str, fallback: str = "file") -> str:
    base = Path(name.replace("\\", "/")).name
    base = _CONTROL.sub("", base).strip().strip(".")
    base = _UNSAFE.sub("_", base)
    if not base:
        base = fallback
    return base[:120]


def write_in_sandbox(
    directory: Path,
    filename: str,
    data: bytes,
    *,
    max_bytes: int,
) -> StoredFile:
    if len(data) > max_bytes:
        raise SandboxError(f"file is too large ({len(data)} bytes; limit {max_bytes})")
    root = directory.resolve()
    target = (root / safe_filename(filename)).resolve()
    if not target.is_relative_to(root):
        raise SandboxError("refusing to write outside the sandbox")
    if target.exists():
        raise SandboxError(f"refusing to overwrite the existing file {target.name!r}")
    root.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return StoredFile(filename=target.name, path=str(target), size_bytes=len(data))
