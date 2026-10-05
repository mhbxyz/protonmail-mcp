from __future__ import annotations

from pathlib import Path

import pytest

from protonmail_mcp.files import SandboxError, safe_filename, write_in_sandbox


def test_safe_filename_strips_paths_and_controls() -> None:
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("..\\..\\evil.txt") == "evil.txt"
    assert safe_filename("bad\nname.txt") == "badname.txt"
    assert safe_filename("...") == "file"
    assert safe_filename("") == "file"
    assert len(safe_filename("x" * 500)) <= 120


def test_write_in_sandbox(tmp_path: Path) -> None:
    stored = write_in_sandbox(tmp_path, "note.txt", b"hello", max_bytes=10)
    assert stored.filename == "note.txt"
    assert stored.size_bytes == 5
    assert Path(stored.path).read_bytes() == b"hello"
    assert Path(stored.path).is_relative_to(tmp_path.resolve())


def test_write_refuses_overwrite_and_oversize(tmp_path: Path) -> None:
    write_in_sandbox(tmp_path, "note.txt", b"hello", max_bytes=10)
    with pytest.raises(SandboxError, match="overwrite"):
        write_in_sandbox(tmp_path, "note.txt", b"other", max_bytes=10)
    with pytest.raises(SandboxError, match="too large"):
        write_in_sandbox(tmp_path, "big.bin", b"x" * 11, max_bytes=10)


def test_write_contains_traversal(tmp_path: Path) -> None:
    stored = write_in_sandbox(tmp_path, "../escape.txt", b"data", max_bytes=10)
    assert Path(stored.path).is_relative_to(tmp_path.resolve())
    assert stored.filename == "escape.txt"
