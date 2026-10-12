"""(B) `spawn-runner`フックの実体。Sandboxの作成・再開を指示して、準備完了を待たずに返す。

終了コード（claude self-hosted-runner orchestratorの契約）: 0=成功、1=再試行、2=再試行しない。
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path

from ..ports import ConflictError
from ..sandbox.manager import RunRequest, SandboxEnvMismatch, SandboxManager

OK, RETRY, FATAL = 0, 1, 2
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


def classify(exc: BaseException) -> int:
    """例外を終了コードに分類する。一時的な障害は再試行、設定の誤りは再試行しない。"""
    if isinstance(exc, SandboxEnvMismatch):
        return FATAL
    if isinstance(exc, ConflictError):
        return RETRY
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        if status in RETRYABLE_STATUS:
            return RETRY
        if 400 <= status < 500:  # 認可・不正なマニフェスト・存在しないnamespace等
            return FATAL
        return RETRY
    # 通信エラー・タイムアウトなどは一時的とみなす
    return RETRY


async def spawn(env: Mapping[str, str], mgr: SandboxManager) -> int:
    session_id = env.get("CLAUDE_RUNNER_SESSION_ID", "")
    wo_path = env.get("CLAUDE_RUNNER_WORK_ORDER_FILE", "")
    if not session_id or not wo_path:
        print(
            "spawn-runner: CLAUDE_RUNNER_SESSION_ID / WORK_ORDER_FILE がありません", file=sys.stderr
        )
        return FATAL
    try:
        work_order = Path(wo_path).read_text()
    except OSError as e:
        print(f"spawn-runner: work orderを読めません: {e}", file=sys.stderr)
        return FATAL
    try:
        res = await mgr.ensure_started(
            RunRequest(
                session_id=session_id,
                mode="cc",
                secret_data={"work_order": work_order},
                user_email=env.get("CLAUDE_RUNNER_ACCOUNT_EMAIL", ""),
            )
        )
    except Exception as e:  # noqa: BLE001 - 分類して終了コードにする
        code = classify(e)
        # stderrの末尾がnackのエラーとして転送される。秘密は含めない。
        print(f"spawn-runner: {type(e).__name__}: {e}", file=sys.stderr)
        return code
    print(f"spawn-runner: {res.action} {res.name}", file=sys.stderr)
    return OK
