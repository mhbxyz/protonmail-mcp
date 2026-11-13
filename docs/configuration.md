# Configuration

protonmail-mcp is configured with environment variables (connection, mode, transport) and
an optional `policy.toml` (capabilities, limits, local stores, profiles).

## Environment variables

### Bridge connection

| Variable | Default | Purpose |
|---|---|---|
| `PROTONMAIL_BRIDGE_USERNAME` | — | Your Proton address (required) |
| `PROTONMAIL_BRIDGE_PASSWORD` | — | Bridge mailbox password, shown in the Bridge UI (required) |
| `PROTONMAIL_BRIDGE_HOST` | `127.0.0.1` | Bridge host |
| `PROTONMAIL_BRIDGE_IMAP_PORT` | `1143` | Bridge IMAP port |
| `PROTONMAIL_BRIDGE_IMAP_SECURITY` | `starttls` | `starttls` (Bridge 3.x on 1143) or `ssl` (direct TLS) |
| `PROTONMAIL_BRIDGE_SMTP_PORT` | `1025` | Bridge SMTP port (used by the `send` capability) |
| `PROTONMAIL_BRIDGE_TIMEOUT` | `30` | Socket timeout in seconds |
| `PROTONMAIL_BRIDGE_VERIFY_TLS` | `false` | Bridge uses a self-signed certificate |

### Server

| Variable | Default | Purpose |
|---|---|---|
| `PROTONMAIL_MCP_MODE` | `read` | Capability preset: `read`, `draft`, `organize`, `send`, `delete` |
| `PROTONMAIL_MCP_POLICY` | `~/.config/protonmail-mcp/policy.toml` | Policy file with capability overrides and limits |
| `PROTONMAIL_MCP_PROFILE` | — | Account profile to use (see [profiles](profiles.md)) |
| `PROTONMAIL_MCP_AUDIT_LOG` | `~/.local/state/protonmail-mcp/audit.jsonl` | Audit log path; an empty value disables it |
| `PROTONMAIL_MCP_HTTP_TOKEN` | — | Bearer token for HTTP mode (see [HTTP transport](http-transport.md)) |
| `PROTONMAIL_MCP_HTTP_HOST` | `127.0.0.1` | HTTP bind address |
| `PROTONMAIL_MCP_HTTP_PORT` | `8765` | HTTP port |

## Command line

| Flag | Purpose |
|---|---|
| `--check` | Connect to Bridge, list folders and print the latest messages, then exit |
| `--profile NAME` | Use an account profile from `policy.toml` |
| `--http` | Serve MCP over streamable HTTP instead of stdio |
| `--host`, `--port`, `--token` | HTTP bind address, port and bearer token |

## MCP clients

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

Other MCP clients work the same way: run `uvx protonmail-mcp` over stdio with the two
Bridge variables in its environment.

## Verify the connection

```bash
PROTONMAIL_BRIDGE_USERNAME="you@proton.me" \
PROTONMAIL_BRIDGE_PASSWORD="..." \
uvx protonmail-mcp --check
```

This connects to Bridge, lists folders, and prints the latest messages. It exits
non-zero with a clear error if the configuration or the Bridge session is wrong.

## Updating

`uvx` caches installs. To force it to pick up the newest release:

```bash
uvx --refresh protonmail-mcp
```

## Capability policy

Write capabilities are opt-in and enforced server-side. `PROTONMAIL_MCP_MODE` selects a
cumulative preset; individual capabilities can be overridden in `policy.toml`.
Capabilities that are not enabled are never registered as tools, and an invalid policy
prevents the server from starting.

The annotated reference is [`policy.example.toml`](../policy.example.toml). Copy it to
`~/.config/protonmail-mcp/policy.toml` (or point `PROTONMAIL_MCP_POLICY` at it).

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

[notifications]
enabled = false
folder = "INBOX"
min_interval_seconds = 30
```

| Section | Controls |
|---|---|
| `[policy]` | Mode preset and default profile |
| `[capabilities]` | Per-capability overrides (for example `send` without `delete`) |
| `[confirmations]` | Lifetime of prepare/commit tokens |
| `[idempotency]` | Window during which an identical draft creation returns the existing draft (`0` disables it) |
| `[organize]` | Bulk cap, Drafts protection, allowed move targets and labels |
| `[files]` | Sandbox directory for attachments and exports, and its size cap |
| `[send]` | Recipient allowlist, quotas, size, loop guards, persistent state |
| `[index]` | Opt-in local SQLite FTS5 index (see [tools](tools.md#local-index)) |
| `[notifications]` | Opt-in IMAP IDLE watcher (see [tools](tools.md#notifications)) |
| `[profiles.*]` | Multiple accounts (see [profiles](profiles.md)) |
