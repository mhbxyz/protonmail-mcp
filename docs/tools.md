# Tools and resources

Tools are registered according to the enabled capabilities: a tool whose capability is
off does not exist for the agent. Results are structured (Pydantic models). Every message
carries its `Message-ID`; use it for follow-up calls, because IMAP UIDs are not stable
across Bridge resynchronisations.

## Read (default)

| Tool | Description |
|---|---|
| `list_folders` | List every folder and label, with IMAP flags and whether it is selectable |
| `get_status` | Total and unread counts per folder, without fetching messages |
| `list_emails` | Most recent messages in a folder, newest first, with a pagination cursor: `limit`, `unread_only`, `since_days`, `sender`, `subject`, `before` |
| `search_emails` | Structured search: `query` (full-text) plus `sender`, `recipient`, `subject`, `since_days`, `before_days`, `unread_only`, `has_attachment` (bounded BODYSTRUCTURE scan, reports `scanned`/`truncated`) |
| `read_email` | Read one message by `Message-ID`: decoded body (quotes/signature stripped, `quoted_removed` flag), optional HTML→Markdown, link inventory, untrusted-content marker |
| `get_thread` | Reconstruct a conversation across All Mail using References/In-Reply-To |
| `daily_digest` | Unread count plus compact recent-message summaries for a folder |
| `list_attachments` | Attachment names, types and sizes without saving anything |
| `save_attachment` | Save one attachment into the local sandbox (traversal-safe, size-capped, never overwrites) |
| `export_email` | Export a message as `.eml` into the local sandbox |
| `sync_index` | Index recent messages into the local SQLite FTS5 store (opt-in via `[index]`) |
| `search_index` | Fast, offline full-text search over the local index |

Mailboxes are opened with IMAP `SELECT ... READONLY`: reading never changes anything, not
even the `\Seen` flag.

## Draft

| Tool | Description |
|---|---|
| `list_drafts` | List drafts |
| `create_draft` | Create a new draft in the Drafts folder |
| `preview_draft` | Render the exact MIME of a draft without saving it |
| `prepare_reply_draft` / `commit_reply_draft` | Reply draft with quoting and threading headers; `reply_all` adds the original To/Cc |
| `prepare_forward_draft` / `commit_forward_draft` | Forward draft; `include_attachments=true` re-attaches the original attachments |
| `prepare_update_draft` / `commit_update_draft` | Replace a draft's content |
| `prepare_delete_draft` / `commit_delete_draft` | Delete a draft |

Draft attachments always come from the local sandbox (`files.directory`). Re-attached
forward attachments come from the original message, are filename-sanitized, and are
capped per file and in total. Repeated identical draft creations within the idempotency
window (default 300 s) return the existing draft instead of creating a duplicate.

## Organize

| Tool | Description |
|---|---|
| `prepare_set_flags` / `commit_set_flags` | Seen and flagged state |
| `prepare_move` / `commit_move` | Move messages; the destination accepts a folder name or the aliases `archive` and `trash` |
| `prepare_label_change` / `commit_label_change` | Add or remove labels (folders under `Labels/`) |
| `prepare_undo_move` / `commit_undo_move` | Revert the most recent move |

Bulk operations are capped (`organize.max_bulk`, default 50), Drafts are protected by
default, and moving to Starred must go through flag/unflag. The undo journal is kept in
memory and does not survive a server restart.

## Send

| Tool | Description |
|---|---|
| `prepare_send_draft` / `commit_send_draft` | Submit an existing draft through Bridge SMTP |

Before anything is transmitted, the server checks the recipient allowlist, hourly and
daily quotas, message size, and loop guards (`Auto-Submitted`, `Precedence`, `List-*`,
no-reply recipients, thread depth, duplicate bodies). The draft is removed only after the
SMTP server accepts the message. Bridge stores the copy in Sent itself. See
[send-design.md](send-design.md).

## Delete

| Tool | Description |
|---|---|
| `prepare_delete_message` / `commit_delete_message` | Permanently erase one message from Trash |

The commit must repeat the exact phrase `permanently delete <message-id>`. There is no
bulk deletion and no empty-trash tool.

## Local index

With `[index] enabled = true`, `sync_index` builds a local plaintext SQLite FTS5 store
(headers plus truncated body text; attachments are never indexed and folders can be
excluded) and `search_index` queries it for fast offline search. The index never leaves
the machine.

## MCP resources

The server also exposes read-only resources:

| URI | Content |
|---|---|
| `mail://folders` | Folder list |
| `mail://status` | Counts per folder |
| `mail://message/{message-id}` | One message |
| `mail://thread/{message-id}` | One conversation |
| `mail://inbox` | Counts and recent message identifiers (with notifications) |

Percent-encode Message-IDs in URIs, for example `%3Cid%40example.com%3E`. Resources
contain the same data as the read tools and never trigger writes.

## Notifications

With `[notifications] enabled = true`, the server watches `notifications.folder` with
IMAP IDLE and publishes resource-updated events for `mail://inbox`. Clients subscribe via
`resources/subscribe`, or `subscriptions/listen` on newer protocol versions. Events carry
no content. The watcher reconnects with exponential backoff and logs to stderr.

Timeliness follows Bridge's own sync with Proton: local mailbox changes are noticed within
about a second, while server-side arrivals can lag by tens of seconds or more.
