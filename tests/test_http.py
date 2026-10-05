from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from protonmail_mcp.http_transport import BearerAuthMiddleware


def ok_app() -> Starlette:
    async def ok(request: httpx.Request) -> JSONResponse:  # type: ignore[type-arg]
        return JSONResponse({"ok": True})

    return Starlette(routes=[Route("/", ok)])


async def call_with_headers(headers: dict[str, str]) -> httpx.Response:
    transport = httpx.ASGITransport(app=BearerAuthMiddleware(ok_app(), "secret"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/", headers=headers)


def test_middleware_requires_bearer_token() -> None:
    assert asyncio.run(call_with_headers({})).status_code == 401
    assert asyncio.run(call_with_headers({"Authorization": "Bearer wrong"})).status_code == 401
    response = asyncio.run(call_with_headers({"Authorization": "Bearer secret"}))
    assert response.status_code == 200


def test_middleware_rejects_empty_token() -> None:
    with pytest.raises(ValueError):
        BearerAuthMiddleware(ok_app(), "")


def test_cli_refuses_http_without_token() -> None:
    env = dict(os.environ)
    env.pop("PROTONMAIL_MCP_HTTP_TOKEN", None)
    env["PROTONMAIL_MCP_AUDIT_LOG"] = ""
    result = subprocess.run(
        [sys.executable, "-m", "protonmail_mcp", "--http"],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert result.returncode == 2
    assert "token" in result.stderr.lower()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_http_end_to_end_with_bearer_auth() -> None:
    port = free_port()
    env = dict(os.environ)
    env["PROTONMAIL_MCP_HTTP_TOKEN"] = "test-token-123"
    env["PROTONMAIL_MCP_AUDIT_LOG"] = ""
    process = subprocess.Popen(
        [sys.executable, "-m", "protonmail_mcp", "--http", "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    try:
        base = f"http://127.0.0.1:{port}/mcp"
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                httpx.get(f"http://127.0.0.1:{port}/", timeout=1)
                break
            except httpx.HTTPError:
                time.sleep(0.3)

        base_headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        }
        assert httpx.post(base, json=initialize, headers=base_headers, timeout=5).status_code == 401
        wrong = httpx.post(
            base,
            json=initialize,
            headers={**base_headers, "Authorization": "Bearer nope"},
            timeout=5,
        )
        assert wrong.status_code == 401

        authorized = httpx.post(
            base,
            json=initialize,
            headers={**base_headers, "Authorization": "Bearer test-token-123"},
            timeout=10,
        )
        assert authorized.status_code == 200
        session_id = authorized.headers.get("mcp-session-id")
        assert session_id

        session_headers = {
            **base_headers,
            "Authorization": "Bearer test-token-123",
            "Mcp-Session-Id": session_id,
        }
        note = httpx.post(
            base,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=session_headers,
            timeout=5,
        )
        assert note.status_code in (200, 202)
        listing = httpx.post(
            base,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            headers=session_headers,
            timeout=10,
        )
        assert listing.status_code == 200
        names = [tool["name"] for tool in listing.json()["result"]["tools"]]
        assert "list_folders" in names
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
