# Remote HTTP transport

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

For a headless container deployment (Bridge on the host, server in a container), see
[docker.md](docker.md).
