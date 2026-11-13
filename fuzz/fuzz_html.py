"""Fuzz HTML rendering, link extraction and quote stripping of message bodies."""

from __future__ import annotations

import sys

import atheris

with atheris.instrument_imports():
    from protonmail_mcp.parsing import (
        extract_links,
        html_to_markdown,
        html_to_text,
        strip_quoted,
    )


def test_one_input(data: bytes) -> None:
    provider = atheris.FuzzedDataProvider(data)
    text = provider.ConsumeUnicodeNoSurrogates(len(data))
    assert isinstance(html_to_text(text), str)
    assert isinstance(html_to_markdown(text), str)
    for link in extract_links(text=text, html=text):
        assert isinstance(link, str)
    stripped, _ = strip_quoted(text)
    assert isinstance(stripped, str)


def main() -> None:
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
