# Alternatives

There are several community MCP servers for Proton Mail. protonmail-mcp aims to stay
small, correct with Bridge's quirks (STARTTLS on 1143, modified UTF-7 labels,
reverse-chronological UIDs, RFC 2047 decoding), and heavily tested. Rough landscape:

| Project | Language | Scope |
|---|---|---|
| [googlarz/proton-mail-bridge-client](https://github.com/googlarz/proton-mail-bridge-client) | TypeScript | Large tool set, read-only and send-to-self modes, SQLite cache |
| [codefuturist/email-mcp](https://github.com/codefuturist/email-mcp) | TypeScript | Generic IMAP + SMTP, works with Bridge |
| [anyrxo/protonmail-pro-mcp](https://github.com/anyrxo/protonmail-pro-mcp) | JavaScript | Large tool set with Bridge integration |
| [chandshy/mailpouch](https://github.com/chandshy/mailpouch) | TypeScript | Large permission-gated tool set |
| [amotivv/protonmail-mcp](https://github.com/amotivv/protonmail-mcp) | JavaScript | SMTP sending only |
| [miketigerblue/proton-bridge-mcp](https://github.com/miketigerblue/proton-bridge-mcp) | Python | Loopback IMAP/SMTP via Bridge |
