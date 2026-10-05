# Security Policy

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting: **Security tab → Report a vulnerability**.
Do not open a public issue for security problems. We aim to acknowledge reports within
72 hours.

## Supported versions

Only the latest release is supported. The project is pre-1.0; security fixes are released
as patch versions.

## Threat model

**Assets**

- Mailbox contents: messages, attachments, contact data in headers
- The Proton Bridge mailbox password
- The ability to send mail as the user's identity
- Local files on the machine running the server

**Adversaries**

1. **Malicious email content.** Emails are attacker-controlled input. The realistic attack
   is prompt injection against the agent reading the mailbox through this server.
2. **A compromised or over-eager agent.** A manipulated model can call tools with harmful
   arguments: exfiltrate data to an address, mass-move, or delete mail.
3. **Other local processes or users.** Anything that can read the configuration or speak
   to the MCP transport.

**Out of scope:** a compromised host or Proton account, vulnerabilities in Proton Bridge
itself (report those to Proton), and physical device theft.

## Current guarantees (v0.1, read-only)

- Tools only read. Mailboxes are opened with IMAP `SELECT ... READONLY`; no flag, including
  `\Seen`, is ever modified.
- The server connects to `127.0.0.1` only and speaks stdio to the MCP client.
- No telemetry, no requests to third parties.

## Automated enforcement

Every push and pull request must pass:

| Check | Tool | Gate |
|---|---|---|
| Lint including security rules (bandit) | ruff (`S`) | blocking |
| Static types, strict | mypy | blocking |
| Adversarial, fuzz, capability-matrix tests | pytest + Hypothesis | blocking |
| Coverage ≥ 88% on security-critical modules | pytest-cov | blocking |
| Secret scanning (full history) | gitleaks | blocking |
| Workflow hardening (SHA pinning, permissions) | zizmor | blocking |
| Dependency vulnerabilities | pip-audit | blocking |
| Static analysis (SAST) | semgrep `p/security-audit`, `p/python` | blocking |
| Code scanning | CodeQL (python + actions) | blocking |
| Dependency review on pull requests | dependency-review-action | blocking |
| Repository posture | OpenSSF Scorecard | weekly |
| Dependency freshness | Dependabot (7-day cooldown) | PR-based |

Structural tests enforce conventions that are otherwise easy to erode:

- Every `commit_*` tool must declare a required `token` parameter and a destructive
  annotation; every `prepare_*` must have a matching `commit_*`.
- `commit_*`/`prepare_*` tools exist only when the corresponding capability is enabled.
- Read-mode tools may never call a mutating client method.
- The whole test suite blocks non-loopback network access: telemetry cannot be added
  silently.
- Tool input schemas are snapshotted; any change must be deliberate.

## Security architecture for write/send/delete

### Capability modes

Capabilities are gated by a cumulative ladder: `read` → `draft` → `organize` → `send` →
`delete`. The active mode comes from `PROTONMAIL_MCP_MODE` (default `read`) and can be
refined by individual flags in `policy.toml`. Tools outside the enabled capabilities are
**not registered at all**. Configuration failures fail closed: absent or invalid policy
means read-only.

### Confirmation framework

Every mutating tool uses a two-phase flow:

1. `prepare_*` returns a preview (exact payload) and a single-use token with a short TTL,
   bound to the SHA-256 of that payload.
2. `commit_*` re-verifies the token and the payload hash before executing.

When the MCP client supports elicitation, the human confirmation is collected through it;
otherwise a dedicated confirm tool provides the same guarantees. Replayed, expired, or
tampered requests are rejected.

### Server-side policy

The LLM cannot grant itself privileges: allowlists, quotas, and caps live in the server.

- Recipient allowlists/denylists; sending defaults to self-only
- Per-hour and per-day send quotas backed by a persistent budget; optional send windows
- Bulk-operation caps and mandatory dry-run above a threshold
- Folder and label allowlists
- Sandboxed file access: attachments and exports can only touch a fixed directory, with
  path-traversal protection and filename sanitization

### Send safeguards

- Loop and bulk-mail guards: `Auto-Submitted`, `Precedence: bulk`/`list`, `List-*`
  headers, no-reply addresses, thread-depth caps, recently-sent duplicate bodies
- Reply-all caps and full recipient preview
- Idempotency keys so a retry can never double-send

### Delete safeguards

Deletion is the highest capability. It is Trash-only, requires a per-message confirmation
token, has a strict per-call cap, never runs in bulk, and no "empty trash" tool exists.

### Audit

Every tool call is written to an append-only JSONL log: timestamp, tool, argument hash,
outcome, and confirmation metadata. The log uses restrictive permissions and never
contains secrets.

## Data handling

- **Secrets.** The Bridge mailbox password lives in the MCP client configuration or the
  environment. Prefer client-side indirection (`{env:...}`, `{file:...}`) over inline
  values, and never commit secrets.
- **Local index (planned).** A plaintext SQLite index is documented as such and stays
  opt-in.
- **Attachments and exports (planned).** Sandbox directory only; never auto-opened.

## Invariants for any new tool

1. It belongs to exactly one capability; a disabled capability means the tool is absent.
2. It is read-only, or it is prepare/commit with payload-bound tokens.
3. It accepts no arbitrary filesystem paths.
4. It makes no network access beyond the local Bridge.
5. It is audited, credited with accurate MCP annotations, and covered by adversarial tests.

## Anti-features

Raw IMAP/SMTP passthrough, filter/auto-forward management, folder create/delete/rename,
web fetching from message content, and any tool that exposes secrets are deliberately out
of scope. See [ROADMAP.md](ROADMAP.md) for the full plan.
