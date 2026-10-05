from __future__ import annotations

import asyncio
import json
from pathlib import Path

from protonmail_mcp.policy import Capabilities, Policy
from protonmail_mcp.server import build_server

SCHEMA_DIR = Path(__file__).parent / "schemas"


def tool_schemas() -> dict[str, dict[str, object]]:
    policy = Policy(
        mode="draft",
        capabilities=Capabilities(draft=True),
        confirmation_ttl_seconds=300,
        source="test",
    )
    tools = asyncio.run(build_server(policy).list_tools())
    return {tool.name: tool.input_schema for tool in tools}


def test_tool_schemas_match_snapshots() -> None:
    missing = []
    for name, schema in tool_schemas().items():
        path = SCHEMA_DIR / f"{name}.json"
        current = json.dumps(schema, indent=2, sort_keys=True) + "\n"
        if not path.exists():
            missing.append(name)
            continue
        assert path.read_text() == current, (
            f"schema changed for {name}; update the snapshot if the change is intentional"
        )
    assert not missing, f"missing schema snapshots: {missing}"
