# Development

```bash
uv sync
uv run pytest
uv run ruff check .
uv run mypy src
uv build
```

Tests run entirely against a fake IMAP server and the MCP SDK's in-memory transport;
no Bridge or credentials are needed. The suite blocks non-loopback network access, and
tool input schemas are snapshotted in `tests/schemas`: any change must be deliberate.

See [SECURITY.md](../SECURITY.md) for the CI gates and the invariants every new tool must
respect.

## Releasing

Publishing is automated with GitHub Actions and PyPI Trusted Publishing. Create a
GitHub release tagged `vX.Y.Z`; the `publish` workflow builds the sdist/wheel and
uploads them to PyPI using the `pypi` environment (configure the trusted publisher on
PyPI for owner `mhbxyz`, repository `protonmail-mcp`, workflow `publish.yml`).
