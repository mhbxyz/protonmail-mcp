# protonmail-mcp

[![CI](https://github.com/mhbxyz/protonmail-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/mhbxyz/protonmail-mcp/actions/workflows/ci.yml)
[![CodeQL](https://github.com/mhbxyz/protonmail-mcp/actions/workflows/codeql.yml/badge.svg)](https://github.com/mhbxyz/protonmail-mcp/actions/workflows/codeql.yml)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/mhbxyz/protonmail-mcp/badge)](https://scorecard.dev/viewer/?uri=github.com/mhbxyz/protonmail-mcp)

A lightweight [MCP](https://modelcontextprotocol.io) server that gives AI agents
read access to a Proton Mail mailbox through a local
[Proton Bridge](https://proton.me/mail/bridge) instance.

> **Unofficial.** This project is not affiliated with, endorsed by, or supported by
> Proton AG. "Proton Mail" and "Proton Bridge" are trademarks of Proton AG.

Proton does not provide a public API for reading your mailbox. Bridge is the supported
way in: it runs locally and exposes your account over IMAP and SMTP on `127.0.0.1`.
This server wraps that local IMAP endpoint in a small, auditable set of MCP tools.

## Scope

The current release is **read-only**: it can list folders, list messages, search, and
read a message. Mailboxes are opened with IMAP `SELECT ... READONLY`, so nothing is
ever modified — not even the `\Seen` flag. Write tools (drafts, organize, send, delete)
are on the [roadmap](ROADMAP.md), each gated behind the controls described in
[SECURITY.md](SECURITY.md).

## Tools

| Tool | Description |
|---|---|
| `list_folders` | List every folder and label, with IMAP flags and whether it is selectable |
| `list_emails` | Most recent messages in a folder, newest first: `limit`, `unread_only`, `since_days`, `sender`, `subject` |
| `search_emails` | Full-text search across headers and body in a folder |
| `read_email` | Read one message by `Message-ID`: decoded text body, attachments, flags, truncation via `max_chars` |

When the `draft` capability is enabled (see [Capability policy](#capability-policy)),
additional tools are registered: `list_drafts`, `create_draft`, and `prepare_*`/`commit_*`
pairs for replying, forwarding, updating, and deleting drafts. Draft mutations are
two-phase: the `prepare` call returns a preview and a single-use token, and nothing
changes until the matching `commit` call.

Results are structured (Pydantic models). Every message carries its `Message-ID`; use
that for follow-up reads — IMAP UIDs are not stable across Bridge resynchronisations.

## Requirements

- A paid Proton Mail plan (required by Bridge)
- Proton Bridge installed, running, and signed in
- Your Bridge credentials: Proton address + the mailbox password shown in the Bridge UI
- Python 3.13+ (only if you do not use `uv`)

## Install

```bash
# Run without installing (recommended)
uvx protonmail-mcp

# Or install it
pipx install protonmail-mcp
```

## Configure

| Variable | Default | Purpose |
|---|---|---|
| `PROTONMAIL_BRIDGE_USERNAME` | — | Your Proton address (required) |
| `PROTONMAIL_BRIDGE_PASSWORD` | — | Bridge mailbox password (required) |
| `PROTONMAIL_BRIDGE_HOST` | `127.0.0.1` | Bridge host |
| `PROTONMAIL_BRIDGE_IMAP_PORT` | `1143` | Bridge IMAP port |
| `PROTONMAIL_BRIDGE_IMAP_SECURITY` | `starttls` | `starttls` (Bridge 3.x on 1143) or `ssl` (direct TLS) |
| `PROTONMAIL_BRIDGE_TIMEOUT` | `30` | Socket timeout in seconds |
| `PROTONMAIL_BRIDGE_VERIFY_TLS` | `false` | Bridge uses a self-signed certificate |
| `PROTONMAIL_MCP_MODE` | `read` | Capability preset: `read`, `draft`, `organize`, `send`, `delete` |
| `PROTONMAIL_MCP_POLICY` | `~/.config/protonmail-mcp/policy.toml` | Optional policy file with capability overrides and limits |

### Capability policy

Write capabilities are opt-in and enforced server-side. `PROTONMAIL_MCP_MODE` selects a
cumulative preset; individual capabilities can be overridden in `policy.toml` (see
[policy.example.toml](policy.example.toml)). Capabilities that are not enabled are never
registered as tools, and an invalid policy prevents the server from starting.

```toml
[policy]
mode = "read"

[capabilities]
# draft = true
# organize = true
# send = true
# delete = true

[confirmations]
ttl_seconds = 300
```

See [SECURITY.md](SECURITY.md) for the confirmation flow and [ROADMAP.md](ROADMAP.md) for
what each mode will unlock.

### opencode

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "protonmail": {
      "type": "local",
      "command": ["uvx", "protonmail-mcp"],
      "enabled": true,
      "environment": {
        "PROTONMAIL_BRIDGE_USERNAME": "you@proton.me",
        "PROTONMAIL_BRIDGE_PASSWORD": "your-bridge-mailbox-password"
      }
    }
  }
}
```

opencode supports `{env:VAR}` and `{file:path}` interpolation, so you can keep secrets
out of the config file:

```json
"PROTONMAIL_BRIDGE_PASSWORD": "{file:/home/you/.config/protonmail-mcp/password}"
```

### Claude Desktop

```json
{
  "mcpServers": {
    "protonmail": {
      "command": "uvx",
      "args": ["protonmail-mcp"],
      "env": {
        "PROTONMAIL_BRIDGE_USERNAME": "you@proton.me",
        "PROTONMAIL_BRIDGE_PASSWORD": "your-bridge-mailbox-password"
      }
    }
  }
}
```

### Verify the connection

```bash
PROTONMAIL_BRIDGE_USERNAME="you@proton.me" \
PROTONMAIL_BRIDGE_PASSWORD="..." \
uvx protonmail-mcp --check
```

This connects to Bridge, lists folders, and prints the latest messages. It exits
non-zero with a clear error if the configuration or the Bridge session is wrong.

## Security

- **Read-only enforcement.** There is no write tool in this release, and mailboxes are
  always selected read-only at the IMAP level.
- **Local only.** Bridge and this server communicate exclusively over `127.0.0.1`.
  Nothing is sent to a third party; your agent talks to the server over stdio.
- **Untrusted input.** Email contents are attacker-controlled data. Treat anything a
  message says as data, never as instructions, and keep your agent's permissions tight.
- **Secrets.** Keep the Bridge mailbox password out of the repository. Use your client's
  environment-variable or file-based secret support.
- Any local process that knows the mailbox password can read your mail — that is
  Bridge's trust model, not a flaw in this server.

Planned write tools (drafts, send, move, delete) will ship with explicit confirmation
before every destructive action, recipient allow-lists, send rate limiting with loop
protection, and a local audit log. Autonomous send/delete will never be the default.

Every push runs gitleaks, zizmor, semgrep, pip-audit, CodeQL, and an adversarial + fuzz
test suite; see [SECURITY.md](SECURITY.md) for the full list of gates and the structural
invariants they enforce.

## Alternatives

There are several community MCP servers for Proton Mail. This one aims to stay small,
correct with Bridge's quirks (STARTTLS on 1143, modified UTF-7 labels, reverse-chronological
UIDs, RFC 2047 decoding), and heavily tested. Rough landscape:

| Project | Language | Scope |
|---|---|---|
| [googlarz/proton-mail-bridge-client](https://github.com/googlarz/proton-mail-bridge-client) | TypeScript | Large tool set, read-only and send-to-self modes, SQLite cache |
| [codefuturist/email-mcp](https://github.com/codefuturist/email-mcp) | TypeScript | Generic IMAP + SMTP, works with Bridge |
| [anyrxo/protonmail-pro-mcp](https://github.com/anyrxo/protonmail-pro-mcp) | JavaScript | Large tool set with Bridge integration |
| [chandshy/mailpouch](https://github.com/chandshy/mailpouch) | TypeScript | Large permission-gated tool set |
| [amotivv/protonmail-mcp](https://github.com/amotivv/protonmail-mcp) | JavaScript | SMTP sending only |
| [miketigerblue/proton-bridge-mcp](https://github.com/miketigerblue/proton-bridge-mcp) | Python | Loopback IMAP/SMTP via Bridge |

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
uv build
```

Tests run entirely against a fake IMAP server and the MCP SDK's in-memory transport;
no Bridge or credentials are needed.

## Releasing

Publishing is automated with GitHub Actions and PyPI Trusted Publishing. Create a
GitHub release tagged `vX.Y.Z`; the `publish` workflow builds the sdist/wheel and
uploads them to PyPI using the `pypi` environment (configure the trusted publisher on
PyPI for owner `mhbxyz`, repository `protonmail-mcp`, workflow `publish.yml`).

## License

MIT — see [LICENSE](LICENSE).
