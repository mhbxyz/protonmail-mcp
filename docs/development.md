# Development

```bash
uv sync
uv run pytest
uv run ruff check .
uv run mypy src
uv build
```

Coverage-guided fuzz targets live in [`fuzz/`](../fuzz/README.md).

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

Each release job also attaches the sdist, the wheel, a Sigstore signature per file
(`*.sigstore.json`) and the SLSA build provenance (`*.intoto.jsonl`) to the GitHub
release. To verify a downloaded artifact:

```bash
# Provenance: built by this repository's publish workflow
gh attestation verify protonmail_mcp-X.Y.Z-py3-none-any.whl --repo mhbxyz/protonmail-mcp

# Signature
uvx sigstore verify github protonmail_mcp-X.Y.Z-py3-none-any.whl \
  --bundle protonmail_mcp-X.Y.Z-py3-none-any.whl.sigstore.json \
  --repository mhbxyz/protonmail-mcp
```
