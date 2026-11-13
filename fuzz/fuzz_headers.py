"""Fuzz the guards against header injection and sandbox path traversal."""

from __future__ import annotations

import sys

import atheris

with atheris.instrument_imports():
    from protonmail_mcp.compose import ComposeError, validate_recipients
    from protonmail_mcp.files import safe_filename


def test_one_input(data: bytes) -> None:
    provider = atheris.FuzzedDataProvider(data)
    value = provider.ConsumeUnicodeNoSurrogates(256)
    try:
        recipients = validate_recipients(value, header="To")
    except ComposeError:
        pass
    else:
        assert "\r" not in recipients and "\n" not in recipients

    name = safe_filename(value)
    assert name, "filename must never be empty"
    assert "/" not in name and "\\" not in name
    assert name not in {".", ".."}
    assert not name.startswith(".")
    assert len(name) <= 120
    assert all(ord(char) >= 0x20 and ord(char) != 0x7F for char in name)


def main() -> None:
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
