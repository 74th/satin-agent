"""セッションID → メールアドレスの対応表（ConfigMap `satin-session-users`、HMAC付き）。

Botが書き、オーケストレータが読む。オーケストレータにAnthropicのAPIキーを持たせないため。
対応表にない（または改ざんされた）セッションは空のメールにする。
"""

from __future__ import annotations

import hashlib
import hmac
import json

from ..ports import KubeApi
from .configmap import read_all, update_key

CONFIGMAP = "satin-session-users"


def _mac(key: bytes, session_id: str, email: str) -> str:
    return hmac.new(key, f"{session_id}\n{email}".encode(), hashlib.sha256).hexdigest()


class UserDirectory:
    def __init__(self, kube: KubeApi, namespace: str, hmac_key: bytes) -> None:
        self._kube, self._ns, self._key = kube, namespace, hmac_key

    async def register(self, session_id: str, email: str) -> None:
        rec = {"email": email, "mac": _mac(self._key, session_id, email)}
        await update_key(self._kube, CONFIGMAP, self._ns, session_id, json.dumps(rec))

    async def lookup(self, session_id: str) -> str:
        raw = (await read_all(self._kube, CONFIGMAP)).get(session_id)
        if not raw:
            return ""
        try:
            rec = json.loads(raw)
            email, mac = rec["email"], rec["mac"]
        except (ValueError, KeyError, TypeError):
            return ""
        if not hmac.compare_digest(mac, _mac(self._key, session_id, email)):
            return ""
        return email
