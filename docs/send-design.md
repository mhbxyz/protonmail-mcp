# Send design

Status: agreed before implementation (v0.5, issues #23-#30).

## Goals

- The agent can send mail only through a prepared, human-confirmed action.
- Fail closed: recipients, quotas, and loop guards are enforced by the server.
- The artifact that is transmitted is exactly the artifact that was previewed.

## Non-goals (v1)

- Compose-and-send in one step without a draft.
- Sending from identities other than the account address.
- Scheduling, campaigns, or bulk sending.

## Spike findings (Bridge 03.27, #30)

- SMTP on `127.0.0.1:1025` advertises STARTTLS, AUTH PLAIN/LOGIN, SIZE, 8BITMIME,
  PIPELINING.
- STARTTLS plus AUTH with the mailbox password works.
- A message sent to self is delivered to INBOX and **automatically stored in Sent by
  Bridge/Proton** — no IMAP APPEND is needed.
- Our `Message-ID` is preserved on both the received and Sent copies, and the `Bcc`
  header is stripped from the transmitted copy.
- Therefore: submit via SMTP and do not duplicate into Sent.

## Tool surface

`prepare_send_draft` / `commit_send_draft`, gated by the `send` capability and named
after the artifact they act on.

1. **prepare** fetches the draft (Drafts folder, by Message-ID), parses it, runs every
   check, and returns a preview plus a single-use token bound to `{folder, message_id}`
   and the draft content hash.
2. **commit** verifies the token, re-fetches and re-hashes the draft (if the content
   changed since prepare, the commit is refused), re-runs every check, then submits.

Sending an existing draft instead of composing during send means the preview is
byte-identical to the transmitted artifact (the draft tools already handle quoting,
attachments, and idempotency), and there is a single mutation path through the
confirmation framework.

## Send pipeline (commit)

1. Verify token and draft hash.
2. Server-side checks:
   - recipient allowlist (below);
   - quotas (below);
   - loop and bulk guards: `Auto-Submitted`, `Precedence: bulk|list|junk`, `List-*`,
     `X-Autoreply`/`X-Autorespond` headers, `noreply`/`no-reply`/`donotreply` local
     parts, References chain longer than `max_thread_depth`, and a body hash already
     sent within `duplicate_window_seconds`;
   - size: message within `max_message_bytes`, recipients within `max_recipients`.
3. SMTP submission: STARTTLS on `127.0.0.1:1025`, envelope From = account address,
   envelope To = To + Cc + Bcc, DATA with the Bcc header stripped.
4. Only after a `250` response: delete the draft (UID EXPUNGE), record the send in the
   budget and sent-key stores, and write the audit entry.
5. Failure semantics: a failure before DATA means nothing was sent. A connection drop
   after DATA but before `250` is *ambiguous*: record `unknown`, never auto-retry; the
   idempotency key keeps manual retries safe.

## Recipient policy

`[send]` policy, fail closed:

- `allow_self` (default `true`): the account address and its plus-addressed forms are
  always allowed.
- `allowed_recipients`: exact addresses.
- `allowed_domains`: exact domains.
- Every To/Cc/Bcc address must match one of the above; otherwise the send is refused
  with the offending address.
- `max_recipients` (default 10). Reply-all is just a To/Cc list and obeys the same
  rules; the preview flags external addresses.

## Quotas and state

`[send] max_per_hour` (default 20) and `max_per_day` (default 100), enforced from a
small SQLite database (`~/.local/state/protonmail-mcp/state.db`):

| table | purpose |
|---|---|
| `sends(ts, message_id)` | sliding-window quota counting |
| `sent_keys(key, ts)` | idempotency for retries and token reuse |
| `body_hashes(hash, ts)` | duplicate-body loop guard |

The database is local and plaintext, and only stores timestamps, Message-IDs, and
hashes — never bodies or recipients.

## Attachments

Drafts carry their own MIME parts; v1 sends what the draft contains under
`max_message_bytes`. The future sandbox attachment tool (#28) only governs how files
enter a draft.

## Confirmation and annotations

- Preview: To/Cc/Bcc with external addresses flagged, subject, body preview, attachment
  names, remaining quota, and every guard result.
- Token: single-use, TTL from `[confirmations]`, bound to the draft hash. MCP
  elicitation is used when the client supports it (opencode currently does not, #37).
- `commit_send_draft` is annotated destructive: sending cannot be undone.

## Open questions

- Sent-copy flags: the spike showed only `\Recent`; confirm Proton marks Sent as read.
- Proton-side sending limits are unknown; local quotas stay well below them.
- `Reply-To`/redirect mismatch warnings (#27) are preview-only in v1.
