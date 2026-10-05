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

Capabilities are opt-in through policy (see [Capability policy](#capability-policy)) and
enforced server-side:

- **read** (default): folders, messages, search, threads, digests, attachments and `.eml`
  export into a local sandbox. Mailboxes are opened with IMAP `SELECT ... READONLY`;
  nothing is modified, not even the `\Seen` flag.
- **draft**: create, preview, reply/forward, update and delete drafts, all two-phase.
- **organize**: seen/flagged state, moves with undo, label add/remove.
- **send**: submit an existing draft through Bridge SMTP after allowlist, quota, size
  and loop-guard checks.
- **delete**: permanently erase one message from Trash, with a typed confirmation phrase
  and no bulk operation.

Every mutation is prepare/commit with payload-bound confirmation tokens; see
[SECURITY.md](SECURITY.md) for the security model.

## Tools

| Tool | Description |
|---|---|
| `list_folders` | List every folder and label, with IMAP flags and whether it is selectable |
| `get_status` | Total and unread counts per folder, without fetching messages |
| `list_emails` | Most recent messages in a folder, newest first, with a pagination cursor: `limit`, `unread_only`, `since_days`, `sender`, `subject`, `before` |
| `search_emails` | Structured search: `query` (full-text) plus `sender`, `recipient`, `subject`, `since_days`, `before_days`, `unread_only` |
| `read_email` | Read one message by `Message-ID`: decoded body (quotes/signature stripped, `quoted_removed` flag), optional HTML→Markdown, link inventory, untrusted-content marker |
| `get_thread` | Reconstruct a conversation across All Mail using References/In-Reply-To |
| `daily_digest` | Unread count plus compact recent-message summaries for a folder |
| `list_attachments` | Attachment names, types and sizes without saving anything |
| `save_attachment` | Save one attachment into the local sandbox (traversal-safe, size-capped, never overwrites) |
| `export_email` | Export a message as `.eml` into the local sandbox |
| `sync_index` | Index recent messages into the local SQLite FTS5 store (opt-in via `[index]`) |
| `search_index` | Fast, offline full-text search over the local index |

When the `draft` capability is enabled (see [Capability policy](#capability-policy)),
additional tools are registered: `list_drafts`, `create_draft`, `preview_draft` (renders
the exact MIME without saving it), and `prepare_*`/`commit_*` pairs for replying,
forwarding, updating, and deleting drafts. Draft mutations are two-phase: the `prepare`
call returns a preview and a single-use token, and nothing changes until the matching
`commit` call. Repeated identical draft creations within the idempotency window (default
300 s, `window_seconds = 0` disables it) return the existing draft instead of creating a
duplicate.

When the `organize` capability is enabled, another set of prepare/commit tools is
registered: seen/flagged state (`prepare_set_flags`), moves (destination accepts a folder
name or the aliases `archive` and `trash`), label add/remove for folders under `Labels/`,
and `prepare_undo_move` to revert the most recent move. Bulk operations are capped
(`organize.max_bulk`, default 50), Drafts are protected by default, travel to Starred must
go through flag/unflag, and every mutation is previewed before its commit.

When the `send` capability is enabled, `prepare_send_draft` / `commit_send_draft` submit
an existing draft through Bridge SMTP. Before anything is transmitted the server checks
the recipient allowlist, hourly/daily quotas, message size, and loop guards
(`Auto-Submitted`, `Precedence`, `List-*`, no-reply recipients, thread depth, duplicate
bodies). The draft is removed only after the SMTP server accepts the message. See
[docs/send-design.md](docs/send-design.md).

When the `delete` capability is enabled, `prepare_delete_message` /
`commit_delete_message` permanently erase a single message from Trash. The commit must
repeat the exact phrase `permanently delete <message-id>`; there is no bulk deletion and
no empty-trash tool.

When `[index] enabled = true`, `sync_index` builds a local plaintext SQLite FTS5 store
(headers plus truncated body text; attachments are never indexed and folders can be
excluded) and `search_index` queries it for fast offline search.

### MCP resources

The server also exposes read-only resources: `mail://folders`, `mail://status`, and the
templates `mail://message/{message-id}` and `mail://thread/{message-id}` (percent-encode
Message-IDs, for example `%3Cid%40example.com%3E`). They contain the same data as the read
tools and never trigger writes.

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

# Force uvx to pick up the newest release if an older one is cached
uvx --refresh protonmail-mcp

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

[idempotency]
window_seconds = 300

[organize]
max_bulk = 50
protect_drafts = true
# allowed_targets = ["Archive", "Folders/Newsletters"]
# label_allowlist = ["Labels/Important"]

[files]
directory = "~/.local/share/protonmail-mcp/files"
max_bytes = 26214400

[send]
allow_self = true
max_per_hour = 20
max_per_day = 100
state_path = "~/.local/state/protonmail-mcp/state.db"

[index]
enabled = false
path = "~/.local/state/protonmail-mcp/index.db"
# excluded_folders = ["Spam"]
max_body_chars = 10000
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

## Remote HTTP transport (optional)

By default the server speaks stdio. To serve MCP over streamable HTTP instead:

```bash
export PROTONMAIL_MCP_HTTP_TOKEN="$(openssl rand -hex 32)"
protonmail-mcp --http --host 127.0.0.1 --port 8765
```

- Bearer authentication is mandatory: HTTP mode refuses to start without a token
  (`--token` or `PROTONMAIL_MCP_HTTP_TOKEN`; prefer the environment variable to keep it
  out of shell history).
- The default bind is loopback. Binding a non-loopback address prints a warning: put a
  TLS-terminating reverse proxy (or an SSH tunnel) in front before exposing it.
- Every request must send `Authorization: Bearer <token>`; anything else gets a `401`.
- Clients connect to `http://127.0.0.1:8765/mcp`.

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

Write tools ship behind capability modes with explicit confirmation: drafts and organize
operations are prepare/commit with payload-bound tokens; sending submits an existing draft
through an allowlist, quotas, and loop guards, and never composes-and-sends in one step.
Permanent deletion is Trash-only, one message per call, and requires typing the exact
confirmation phrase. Autonomous send or delete is never the default.

Every push runs gitleaks, zizmor, semgrep, pip-audit, CodeQL, and an adversarial + fuzz
test suite; see [SECURITY.md](SECURITY.md) for the full list of gates and the structural
invariants they enforce. The send milestone is designed in
[docs/send-design.md](docs/send-design.md) before any send code lands.

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
