# Docker / headless deployment

Bridge stays on the host; only the MCP server runs in a container. This guide targets
Linux hosts, where `network_mode: host` lets the container reach Bridge on
`127.0.0.1:1143` / `127.0.0.1:1025`.

```text
┌──────────────────────────── host ────────────────────────────┐
│  Proton Bridge ── 127.0.0.1:1143/1025                         │
│        ▲                                                      │
│        │ host network namespace                               │
│  ┌─────┴──────────────── container ────────────────────┐      │
│  │  protonmail-mcp (non-root, no Proton credentials    │      │
│  │  baked in; mailbox password passed via environment) │      │
│  │  /data volume: audit, state, index, sandbox         │      │
│  └──────────────────────────────────────────────────────┘     │
└───────────────────────────────────────────────────────────────┘
```

## Prerequisites

- Linux host with Docker and the Compose plugin
- Proton Bridge installed, running, and signed in on the host
- A paid Proton plan (required by Bridge)

## Quick start (HTTP mode)

```bash
cp .env.example .env    # then edit it and chmod 600 it
docker compose up -d --build
```

`.env` must contain:

```bash
PROTONMAIL_BRIDGE_USERNAME=you@proton.me
PROTONMAIL_BRIDGE_PASSWORD=the-bridge-mailbox-password
PROTONMAIL_MCP_HTTP_TOKEN=$(openssl rand -hex 32)
PROTONMAIL_MCP_MODE=read
```

The service binds `127.0.0.1:8765` inside the host network namespace, so it is reachable
from the host only. Clients connect to `http://127.0.0.1:8765/mcp` with
`Authorization: Bearer <token>`.

Verify the pipeline:

```bash
# without a token -> 401
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8765/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'

docker compose logs -f
```

## stdio alternative

For an agent that launches local commands, run the container in stdio mode instead:

```bash
docker run -i --rm --network=host \
  -e PROTONMAIL_BRIDGE_USERNAME -e PROTONMAIL_BRIDGE_PASSWORD \
  -e PROTONMAIL_MCP_MODE=read \
  -v protonmail-data:/data \
  protonmail-mcp:local
```

MCP client configuration equivalent:

```json
{
  "mcp": {
    "protonmail": {
      "type": "local",
      "command": [
        "docker", "run", "-i", "--rm", "--network=host",
        "-e", "PROTONMAIL_BRIDGE_USERNAME", "-e", "PROTONMAIL_BRIDGE_PASSWORD",
        "-v", "protonmail-data:/data",
        "protonmail-mcp:local"
      ],
      "enabled": true
    }
  }
}
```

(`-e NAME` without a value forwards the variable from the client environment; keep the
mailbox password in your client's secret storage, not in the JSON.)

## Trust boundaries and security notes

- **Bridge stays on the host.** The container talks to Bridge over the host loopback;
  ports 1143/1025 are never published. Do not expose them.
- **Host networking shares the host network namespace.** Bind the HTTP server to
  `127.0.0.1` (as the compose example does); binding `0.0.0.0` would expose it to the
  network. For remote access, keep the loopback bind and front it with a TLS-terminating
  reverse proxy or an SSH tunnel.
- **Secrets.** The image contains no credentials; the mailbox password arrives through
  the environment (compose reads `.env`, which must be `chmod 600` and never committed).
  Anyone with Docker access can read container environment values (`docker inspect`), so
  treat Docker access as equivalent to mailbox access. Compose secrets are the stricter
  alternative if your setup supports them.
- **State volume.** `/data` holds the audit log, send-state, the optional FTS index, and
  the attachment sandbox in plaintext. Rely on host disk encryption for at-rest
  protection, back it up deliberately, and note that it survives container removal.
- **Capabilities.** `PROTONMAIL_MCP_MODE` defaults to `read`; enable `draft`, `organize`,
  `send`, or `delete` only consciously. Confirmation tokens, allowlists, and quotas apply
  inside the container exactly as they do locally.
- **Image hygiene.** The container runs as a non-root user; the only network listener is
  the HTTP server you configure. The image builds the current checkout; when deploying
  for production, pin a git tag (or install the released package from PyPI) and rebuild
  to update.

## macOS / Windows hosts

Docker Desktop runs containers in a VM whose loopback is not the host's, so Bridge's
`127.0.0.1` listeners are unreachable from the container. Use the stdio or HTTP mode
running natively on the host instead, or change Bridge's listening configuration at your
own risk.
