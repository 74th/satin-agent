import logging
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from echo_mcp.server import MissingKeyError, create_app, load_key
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

KEY = "test-secret-key-0123456789"


@pytest.fixture(scope="module")
def url():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(create_app(KEY), host="127.0.0.1", port=port, log_level="warning")
    )
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    t.join(5)


async def _echo(url, headers, text="こんにちは"):
    async with Client(StreamableHttpTransport(url, headers=headers)) as c:
        tools = await c.list_tools()
        res = await c.call_tool("echo", {"text": text})
    return [t.name for t in tools], res.data


async def test_x_api_key_header(url):
    names, data = await _echo(url, {"X_API_KEY": KEY})
    assert names == ["echo"] and data == "こんにちは"


async def test_bearer_header(url):
    _, data = await _echo(url, {"Authorization": f"Bearer {KEY}"}, "bearer")
    assert data == "bearer"


@pytest.mark.parametrize(
    "headers",
    [{}, {"X_API_KEY": "wrong"}, {"Authorization": "Bearer wrong"}, {"Authorization": KEY}],
)
async def test_missing_or_wrong_key_is_rejected_before_any_tool_runs(url, headers):
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "echo", "arguments": {"text": "x"}},
    }
    async with httpx.AsyncClient() as c:
        r = await c.post(
            url,
            json=init,
            headers={"Accept": "application/json, text/event-stream", **headers},
        )
    assert r.status_code == 401
    assert "echo" not in r.text and "wrong" not in r.text and KEY not in r.text


async def test_unauthenticated_client_cannot_connect(url):
    with pytest.raises(Exception):  # noqa: B017
        async with Client(StreamableHttpTransport(url, headers={"X_API_KEY": "wrong"})) as c:
            await c.list_tools()


@pytest.mark.parametrize("value", [None, "", "   "])
def test_empty_key_fails_to_start(value):
    env = {} if value is None else {"ECHO_MCP_API_KEY": value}
    with pytest.raises(MissingKeyError):
        load_key(env)


async def test_key_never_appears_in_responses_or_logs(url, caplog):
    caplog.set_level(logging.DEBUG)
    async with httpx.AsyncClient() as c:
        for h in ({"X_API_KEY": KEY}, {"X_API_KEY": "bad"}):
            r = await c.post(url, json={}, headers={"Accept": "application/json", **h})
            assert KEY not in r.text and KEY not in str(r.headers)
    assert KEY not in caplog.text
