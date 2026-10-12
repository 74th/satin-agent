"""認証付きのEcho MCPサーバ（FastMCP、Streamable HTTP、/mcp）。"""

from __future__ import annotations

import os
from collections.abc import Mapping

from fastmcp import FastMCP
from starlette.middleware import Middleware

from .auth import ApiKeyMiddleware

KEY_ENV = "ECHO_MCP_API_KEY"


class MissingKeyError(RuntimeError):
    pass


def load_key(env: Mapping[str, str] | None = None) -> str:
    key = (os.environ if env is None else env).get(KEY_ENV, "")
    if not key.strip():
        raise MissingKeyError(f"{KEY_ENV} が設定されていないため起動しません")
    return key


def create_app(key: str):
    mcp = FastMCP("echo")

    @mcp.tool
    def echo(text: str) -> str:
        """受け取った文字列をそのまま返す。"""
        return text

    return mcp.http_app(path="/mcp", middleware=[Middleware(ApiKeyMiddleware, key=key)])


def main() -> None:
    import uvicorn

    app = create_app(load_key())  # 鍵がなければ、ここで例外になり起動しない
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), access_log=False)


if __name__ == "__main__":
    main()
