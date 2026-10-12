"""全てのHTTP要求の鍵を照合するASGIミドルウェア。

鍵は `X_API_KEY`（`X-API-Key`）ヘッダー、または `Authorization: Bearer` で受け取る。
Managed AgentsのVault（static_bearer）はBearerで送ると想定されるが、文書にないので両方を受け付ける。
比較は定数時間で行い、鍵の値は応答にもログにも出さない。
"""

from __future__ import annotations

import hmac

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

_API_KEY_HEADERS = (b"x_api_key", b"x-api-key")


class ApiKeyMiddleware:
    def __init__(self, app: ASGIApp, *, key: str) -> None:
        if not key:
            raise ValueError("API key must not be empty")
        self.app = app
        self._key = key.encode()

    def _presented(self, scope: Scope) -> list[bytes]:
        out: list[bytes] = []
        for name, value in scope.get("headers", []):
            if name in _API_KEY_HEADERS:
                out.append(value.strip())
            elif name == b"authorization":
                scheme, _, token = value.partition(b" ")
                if scheme.lower() == b"bearer":
                    out.append(token.strip())
        return out

    def _ok(self, scope: Scope) -> bool:
        # 全ての候補を比較し、途中で打ち切らない
        results = [hmac.compare_digest(p, self._key) for p in self._presented(scope)]
        return any(results)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket") and not self._ok(scope):
            if scope["type"] == "http":
                resp = JSONResponse({"error": "unauthorized"}, status_code=401)
                await resp(scope, receive, send)
            else:
                await send({"type": "websocket.close", "code": 1008})
            return
        await self.app(scope, receive, send)
