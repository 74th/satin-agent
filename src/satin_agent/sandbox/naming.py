"""セッションIDからのリソース名・ラベル。"""

from __future__ import annotations

import hashlib

LABEL_SESSION = "satin.dev/session"
LABEL_MODE = "satin.dev/mode"
LABEL_SANDBOX = "satin.dev/sandbox"
ANN_SESSION_ID = "satin.dev/session-id"
ANN_GENERATION = "satin.dev/generation"
ANN_LAST_EXIT_AT = "satin.dev/last-exit-at"
ANN_EXIT_CODE = "satin.dev/exit-code"
ANN_ENV_HASH = "satin.dev/env-hash"
ANN_STARTED_AT = "satin.dev/started-at"


def session_hash(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()[:10]


def sandbox_name(session_id: str, generation: int) -> str:
    return f"satin-{session_hash(session_id)}-g{generation}"


def secret_name(session_id: str, generation: int = 1) -> str:
    """処理ごとのSecret。世代ごとに別の名前にする。

    古い世代のSandboxを削除すると、それを所有者とするSecretがガベージコレクションされる。
    世代をまたいで同じ名前だと、新しい世代のSecretまで消えることがある（実クラスタで発生）。
    """
    return f"satin-run-{session_hash(session_id)}-g{generation}"


def pvc_name(sandbox: str) -> str:
    return f"workspace-{sandbox}"
