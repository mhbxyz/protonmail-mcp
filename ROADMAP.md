# Roadmap

protonmail-mcp grows in strictly gated layers: nothing mutating ships without the
security infrastructure that constrains it. Every item below is tracked as a GitHub
issue in the milestone named for its phase; `risk:*` labels indicate the review rigor.

## Design principles

1. Capabilities are gated by explicit modes, never implied.
2. Security policy (allowlists, quotas, caps) is enforced by the server, not by prompts.
3. Mutating actions use a prepare/commit flow with payload-bound confirmation tokens.
4. Email content is untrusted data: labeled as such, never interpreted.
5. Every tool call is auditable, and local state stays local.

## Modes

Cumulative ladder, selected with `PROTONMAIL_MCP_MODE` (default `read`):

| Mode | Adds | Notes |
|---|---|---|
| `read` | Read tools | Default; nothing is modified |
| `draft` | Draft creation/editing | Writes confined to the Drafts folder |
| `organize` | Flags, moves, labels, trash | Reversible mailbox operations |
| `send` | SMTP submission | Recipient allowlist and quotas required |
| `delete` | Permanent deletion | Highest privilege: irreversible, Trash-only, per-message confirmation |

Individual capabilities can be overridden in `policy.toml` (for example, enabling
`send` without `delete`). Failing closed is the default: absent configuration means
read-only.

## Phases

### v0.2 — Read enhancements

| Feature | Issue | Risk |
|---|---|---|
| `get_status` counts per folder | [#1](https://github.com/mhbxyz/protonmail-mcp/issues/1) | low |
| Pagination for `list_emails` | [#2](https://github.com/mhbxyz/protonmail-mcp/issues/2) | low |
| Structured search filters | [#3](https://github.com/mhbxyz/protonmail-mcp/issues/3) | low |
| `get_thread` conversation reconstruction | [#4](https://github.com/mhbxyz/protonmail-mcp/issues/4) | low |
| Attachment listing and sandboxed save | [#5](https://github.com/mhbxyz/protonmail-mcp/issues/5) | medium |
| `export_email` to sandboxed .eml | [#6](https://github.com/mhbxyz/protonmail-mcp/issues/6) | medium |
| `daily_digest` compact summary | [#7](https://github.com/mhbxyz/protonmail-mcp/issues/7) | low |
| Enriched body rendering + untrusted markers | [#8](https://github.com/mhbxyz/protonmail-mcp/issues/8) | low |
| Tool annotations + schema snapshots | [#9](https://github.com/mhbxyz/protonmail-mcp/issues/9) | low |

### v0.3 — Drafts

| Feature | Issue | Risk |
|---|---|---|
| `policy.toml` + capability modes | [#10](https://github.com/mhbxyz/protonmail-mcp/issues/10) | high |
| Two-phase confirmation framework | [#11](https://github.com/mhbxyz/protonmail-mcp/issues/11) | high |
| Append-only audit log | [#12](https://github.com/mhbxyz/protonmail-mcp/issues/12) | medium |
| Bridge draft/APPEND spike | [#13](https://github.com/mhbxyz/protonmail-mcp/issues/13) | low |
| Drafts CRUD | [#14](https://github.com/mhbxyz/protonmail-mcp/issues/14) | medium |
| `draft_reply` / `draft_forward` | [#15](https://github.com/mhbxyz/protonmail-mcp/issues/15) | medium |
| `preview_draft` | [#16](https://github.com/mhbxyz/protonmail-mcp/issues/16) | low |
| Draft idempotency | [#17](https://github.com/mhbxyz/protonmail-mcp/issues/17) | medium |
| Adversarial test suite | [#18](https://github.com/mhbxyz/protonmail-mcp/issues/18) | medium |

### v0.4 — Organize

| Feature | Issue | Risk |
|---|---|---|
| Read/flag state changes | [#19](https://github.com/mhbxyz/protonmail-mcp/issues/19) | medium |
| Move / archive / trash | [#20](https://github.com/mhbxyz/protonmail-mcp/issues/20) | medium |
| Proton label add/remove | [#21](https://github.com/mhbxyz/protonmail-mcp/issues/21) | medium |
| `undo_move` journal | [#22](https://github.com/mhbxyz/protonmail-mcp/issues/22) | medium |

### v0.5 — Send

| Feature | Issue | Risk |
|---|---|---|
| Two-phase sending | [#23](https://github.com/mhbxyz/protonmail-mcp/issues/23) | high |
| Recipient allowlist | [#24](https://github.com/mhbxyz/protonmail-mcp/issues/24) | high |
| Send quotas + persistent budget | [#25](https://github.com/mhbxyz/protonmail-mcp/issues/25) | high |
| Loop and bulk-mail guards | [#26](https://github.com/mhbxyz/protonmail-mcp/issues/26) | high |
| Reply-all caps and warnings | [#27](https://github.com/mhbxyz/protonmail-mcp/issues/27) | medium |
| Sandboxed send attachments | [#28](https://github.com/mhbxyz/protonmail-mcp/issues/28) | medium |
| Send idempotency keys | [#29](https://github.com/mhbxyz/protonmail-mcp/issues/29) | high |
| Sent-folder behavior spike | [#30](https://github.com/mhbxyz/protonmail-mcp/issues/30) | low |

### v0.6 — Delete

| Feature | Issue | Risk |
|---|---|---|
| Permanent delete mode | [#31](https://github.com/mhbxyz/protonmail-mcp/issues/31) | high |

### v0.7 — Scale

| Feature | Issue | Risk |
|---|---|---|
| SQLite FTS5 local index | [#32](https://github.com/mhbxyz/protonmail-mcp/issues/32) | medium |
| MCP resources + IDLE notifications | [#33](https://github.com/mhbxyz/protonmail-mcp/issues/33) | medium |
| Multi-account profiles | [#34](https://github.com/mhbxyz/protonmail-mcp/issues/34) | medium |
| Optional HTTP transport | [#35](https://github.com/mhbxyz/protonmail-mcp/issues/35) | high |
| Docker/headless guide | [#36](https://github.com/mhbxyz/protonmail-mcp/issues/36) | low |

## Anti-features (deliberately never shipping)

- Raw IMAP/SMTP command passthrough
- Filter or auto-forwarding management
- Folder creation, deletion, or renaming
- Web fetching from message content
- Arbitrary filesystem paths for attachments or exports
- Secrets exposed through MCP tools

## Process

Each phase follows the same path: specify the issue, spike any unclear Bridge behavior,
implement with tests, add adversarial tests, then release. `risk:high` items require an
explicit security review against `SECURITY.md` before merge.
