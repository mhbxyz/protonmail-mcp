# Multiple accounts (profiles)

One Bridge instance serves one Proton account, so multi-account setups run one MCP server
per account, selected with `--profile` (or `PROTONMAIL_MCP_PROFILE`). Profiles live in
`policy.toml`:

```toml
[policy]
default_profile = "perso"

[profiles.perso]
username = "perso@proton.me"
password_env = "PROTONMAIL_BRIDGE_PASSWORD_PERSO"

[profiles.work]
username = "work@proton.me"
password_env = "PROTONMAIL_BRIDGE_PASSWORD_WORK"
imap_port = 2143
smtp_port = 2025
mode = "organize"
```

- Each profile carries its own username, host/ports and the name of the environment
  variable holding its Bridge password; credentials never cross profiles.
- `mode` narrows the account's capabilities. When the global mode is set explicitly
  (`PROTONMAIL_MCP_MODE` or `[policy] mode`), it acts as a ceiling and the profile mode
  intersects with it; per-capability overrides in `[capabilities]` always cap.
- Register one MCP entry per profile, for example in opencode:

```json
"protonmail-perso": { "type": "local", "command": ["uvx", "protonmail-mcp", "--profile", "perso"], "enabled": true, "environment": { "PROTONMAIL_BRIDGE_PASSWORD_PERSO": "..." } },
"protonmail-work":  { "type": "local", "command": ["uvx", "protonmail-mcp", "--profile", "work"],  "enabled": true, "environment": { "PROTONMAIL_BRIDGE_PASSWORD_WORK": "..." } }
```

Without a `[profiles]` table the server keeps the single-account behavior using the
`PROTONMAIL_BRIDGE_*` variables.
