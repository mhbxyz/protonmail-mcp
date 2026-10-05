from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

from .bridge import MailboxError
from .config import ConfigError
from .policy import PolicyError, effective_mode

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="protonmail-mcp",
        description="MCP server for Proton Mail via Proton Bridge (stdio by default).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Test the Proton Bridge connection, print a short diagnostic, and exit.",
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="Serve MCP over streamable HTTP instead of stdio (bearer token required).",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("PROTONMAIL_MCP_HTTP_HOST", "127.0.0.1"),
        help="HTTP bind host; defaults to loopback.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PROTONMAIL_MCP_HTTP_PORT", "8765")),
        help="HTTP port (default 8765).",
    )
    parser.add_argument(
        "--token",
        default="",
        help="Bearer token for HTTP mode; prefer PROTONMAIL_MCP_HTTP_TOKEN.",
    )
    parser.add_argument(
        "--profile",
        default="",
        help="Account profile defined in policy.toml (sets PROTONMAIL_MCP_PROFILE).",
    )
    args = parser.parse_args(argv)
    if args.profile:
        os.environ["PROTONMAIL_MCP_PROFILE"] = args.profile
    if args.check:
        raise SystemExit(run_check())
    if args.http:
        raise SystemExit(run_http(args.host, args.port, args.token))
    raise SystemExit(run_server())


def run_server() -> int:
    try:
        from .server import server
    except PolicyError as exc:
        print(f"ERROR: invalid policy: {exc}", file=sys.stderr)
        return 2
    server.run(transport="stdio")
    return 0


def run_http(host: str, port: int, token: str) -> int:
    token = token or os.environ.get("PROTONMAIL_MCP_HTTP_TOKEN", "")
    if not token:
        print(
            "ERROR: HTTP mode requires a bearer token: set PROTONMAIL_MCP_HTTP_TOKEN "
            "or pass --token.",
            file=sys.stderr,
        )
        return 2
    if host not in LOOPBACK_HOSTS:
        print(
            f"WARNING: binding {host} exposes the server beyond loopback; terminate "
            "TLS in front of it.",
            file=sys.stderr,
        )
    try:
        from .server import POLICY, build_server
    except PolicyError as exc:
        print(f"ERROR: invalid policy: {exc}", file=sys.stderr)
        return 2

    import uvicorn

    from .http_transport import BearerAuthMiddleware

    http_server = build_server(POLICY)
    app = http_server.streamable_http_app(host=host, json_response=True)
    uvicorn.run(BearerAuthMiddleware(app, token), host=host, port=port, log_level="warning")
    return 0


def run_check() -> int:
    try:
        from .server import POLICY, get_client
    except PolicyError as exc:
        print(f"ERROR: invalid policy: {exc}", file=sys.stderr)
        return 2

    client = None
    try:
        client = get_client()
        folders = client.list_folders()
        recent = client.list_emails(limit=3).messages
    except (ConfigError, MailboxError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.close()

    print(f"Bridge connection OK: {client.config.username} -> {client.config.endpoint}")
    print(f"Profile: {POLICY.profile_name}")
    print(
        f"Policy: mode={effective_mode(POLICY.capabilities)} "
        f"(configured: {POLICY.mode}), capabilities: {POLICY.capabilities.describe()} "
        f"(source: {POLICY.source})"
    )
    print(f"Folders ({len(folders)}):")
    for folder in folders:
        marker = "" if folder.selectable else " [not selectable]"
        print(f"  - {folder.name}{marker}")
    print(f"Latest INBOX messages ({len(recent)}):")
    for email in recent:
        state = "unread" if email.unread else "read"
        print(f"  - [{state}] {email.received or email.date} | {email.sender} | {email.subject}")
    return 0


if __name__ == "__main__":
    main()
