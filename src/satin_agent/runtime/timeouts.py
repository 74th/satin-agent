"""bashツールのタイムアウトの上書き。

Managed Agentsの`EnvironmentWorker`には、2つの上限がある（SDKの定数）:
- `agent_toolset.BASH_DEFAULT_TIMEOUT`（120秒）: bashの`timeout_ms`が未指定のときの値
- `_beta_session_runner.TOOL_TIMEOUT`（150秒）: **1回のツール呼び出し全体の上限**。`timeout_ms`を
  長く指定しても、これを超えると「tool 'bash' timed out」で打ち切られる

30分かかるクエリなどを実行できるよう、Sandboxの起動時に設定値で上書きする。
外側の上限は、bashの上限より必ず長くする（SDKの規約。同じ値だと、後始末が走らない）。
"""

from __future__ import annotations

from collections.abc import Mapping

DEFAULT = 3600.0
MAX = 7200.0
OUTER_MARGIN = 30.0


def read_timeouts(env: Mapping[str, str]) -> tuple[float, float]:
    default = float(env.get("SATIN_BASH_DEFAULT_TIMEOUT", DEFAULT))
    maximum = max(float(env.get("SATIN_BASH_MAX_TIMEOUT", MAX)), default)
    return default, maximum


def apply_cma_timeouts(env: Mapping[str, str]) -> tuple[float, float]:
    """(A)のワーカーのプロセスで、SDKの定数を上書きする。(default, max)を返す。"""
    from anthropic.lib.tools import _beta_session_runner, agent_toolset

    default, maximum = read_timeouts(env)
    agent_toolset.BASH_DEFAULT_TIMEOUT = default
    _beta_session_runner.TOOL_TIMEOUT = maximum + OUTER_MARGIN
    return default, maximum


def claude_code_env(env: Mapping[str, str]) -> dict[str, str]:
    """(B)のClaude Codeのbashツール用の環境変数（利用者が既に設定していれば、それを優先）。"""
    default, maximum = read_timeouts(env)
    out: dict[str, str] = {}
    if "BASH_DEFAULT_TIMEOUT_MS" not in env:
        out["BASH_DEFAULT_TIMEOUT_MS"] = str(int(default * 1000))
    if "BASH_MAX_TIMEOUT_MS" not in env:
        out["BASH_MAX_TIMEOUT_MS"] = str(int(maximum * 1000))
    return out
