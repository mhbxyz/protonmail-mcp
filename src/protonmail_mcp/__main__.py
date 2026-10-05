from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from .bridge import MailboxError
from .config import ConfigError
from .policy import PolicyError


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="protonmail-mcp",
        description="MCP server for Proton Mail via Proton Bridge (stdio).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Test the Proton Bridge connection, print a short diagnostic, and exit.",
    )
    args = parser.parse_args(argv)
    if args.check:
        raise SystemExit(run_check())
    raise SystemExit(run_server())


def run_server() -> int:
    try:
        from .server import server
    except PolicyError as exc:
        print(f"ERROR: invalid policy: {exc}", file=sys.stderr)
        return 2
    server.run(transport="stdio")
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
        recent = client.list_emails(limit=3)
    except (ConfigError, MailboxError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.close()

    print(f"Bridge connection OK: {client.config.username} -> {client.config.endpoint}")
    print(
        f"Policy: mode={POLICY.mode}, capabilities: {POLICY.capabilities.describe()} "
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
