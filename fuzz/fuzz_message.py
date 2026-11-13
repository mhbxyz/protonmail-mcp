"""Fuzz the parsers that receive raw, attacker-controlled RFC 822 messages."""

from __future__ import annotations

import sys

import atheris

with atheris.instrument_imports():
    from protonmail_mcp.parsing import (
        attachment_parts,
        full_from_message,
        header_message_id,
        thread_parent_ids,
    )
    from protonmail_mcp.send import SendError, emitted_header_names, transmission_from_raw


def test_one_input(data: bytes) -> None:
    email = full_from_message(
        data, uid=1, folder="INBOX", flags=[], size=len(data), max_chars=2000
    )
    assert isinstance(email.body_text, str)
    attachment_parts(data)
    for message_id in thread_parent_ids(data):
        assert message_id.startswith("<")
    header_message_id(data)
    try:
        transmission = transmission_from_raw(data)
    except SendError:
        return
    # No Bcc may reach the recipients, and no header may be injected by refolding.
    assert b"bcc" not in emitted_header_names(transmission.payload)


def main() -> None:
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
