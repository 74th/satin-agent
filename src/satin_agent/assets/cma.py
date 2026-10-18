"""Managed Agentsへの配布（Skills API / Agent）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..ports import AnthropicPort
from ..runtime.bgtasks import TOOL_DEFS
from .load import Assets

SKILLS_NOTICE = (
    "## Skills\n"
    "利用できるSkillsは `/workspace/skills/<name>/SKILL.md` にあります。"
    "作業の前に `/workspace/skills/` の一覧を確認し、"
    "関係するSkillの SKILL.md を読んでから進めてください。"
)

BACKGROUND_NOTICE = (
    "## コマンドの実行とバックグラウンドのタスク\n"
    "`cd`・`export`などシェルの状態を変える操作以外のコマンドは、`bash`ではなく`run`で実行してください。"
    "`run`は、待ち時間（既定30秒、最大60秒）以内に終われば結果を返し、終わらなければ止めずに"
    "バックグラウンドへ回してタスクIDを返します。"
    "タスクは`task_status`で確認、`task_wait`で待機（1回60秒まで）、`task_stop`で停止できます。"
    "結果をすぐに必要としないときは、`task_wait`を繰り返さずに、いったんturnを終えてください。"
    "バックグラウンドのタスクが終わると、あなたに知らせが届きます"
    "（ツール結果の末尾の`[satin]`の行、または`[自動通知]`のメッセージ）。"
    "知らせを受けたら`task_status`で結果を確認し、依頼に沿ってユーザへ報告してください。"
)


def build_system(assets: Assets) -> str:
    parts = []
    if assets.claude_md:
        parts.append(assets.claude_md.rstrip())
    parts.append(SKILLS_NOTICE)
    parts.append(BACKGROUND_NOTICE)
    return "\n\n".join(parts) + "\n"


class State:
    """`.satin-state.json`。IDだけを保存し、秘密は書かない。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = json.loads(path.read_text()) if path.exists() else {}

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n")


def agent_config(
    *,
    name: str,
    model: str,
    system: str,
    skill_ids: list[str],
    mcp_servers: list[dict[str, Any]],
    bash_always_ask: bool = False,
) -> dict[str, Any]:
    toolset: dict[str, Any] = {
        "type": "agent_toolset_20260401",
        "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
        "configs": [
            {"name": "web_search", "enabled": False},
            {"name": "web_fetch", "enabled": False},
        ],
    }
    if bash_always_ask:
        toolset["configs"].append({"name": "bash", "permission_policy": {"type": "always_ask"}})
    tools: list[dict[str, Any]] = [toolset]
    # ワーカーがSandbox内で実行するカスタムツール（権限ポリシーは効かない）。
    # 名前・説明・入力の形は、ワーカーと同じ定義（runtime/bgtasks.TOOL_DEFS）から作る。
    tools.extend({"type": "custom", **d} for d in TOOL_DEFS)
    for server in mcp_servers:
        # always_allow: Botはbash以外のツール確認に応答しない
        tools.append(
            {
                "type": "mcp_toolset",
                "mcp_server_name": server["name"],
                "default_config": {"permission_policy": {"type": "always_allow"}},
            }
        )
    cfg: dict[str, Any] = {
        "name": name,
        "model": model,
        "system": system,
        "tools": tools,
        # versionは指定しない(latest)。明示するとワーカーのダウンロードが失敗する不具合がある。
        "skills": [{"type": "custom", "skill_id": s} for s in skill_ids],
    }
    if mcp_servers:
        cfg["mcp_servers"] = mcp_servers
    return cfg


def publish_cma(
    assets: Assets,
    api: AnthropicPort,
    state: State,
    *,
    name: str = "satin-agent",
    model: str = "claude-haiku-5-5",
    bash_always_ask: bool = False,
) -> dict[str, Any]:
    skills_state: dict[str, str] = state.data.setdefault("skills", {})
    for skill in assets.skills.values():
        files = {f"{skill.name}/{rel}": data for rel, data in skill.files.items()}
        if skill.name in skills_state:
            api.add_skill_version(skills_state[skill.name], files)
        else:
            created = api.create_skill(skill.name, files)
            skills_state[skill.name] = created["id"]
            state.save()
    # アセットから消えたSkillはAgentから外す（Skill自体は残す）
    for gone in set(skills_state) - set(assets.skills):
        del skills_state[gone]

    cfg = agent_config(
        name=name,
        model=model,
        system=build_system(assets),
        skill_ids=[skills_state[n] for n in sorted(assets.skills)],
        mcp_servers=assets.mcp_servers,
        bash_always_ask=bash_always_ask,
    )
    if agent_id := state.data.get("agent_id"):
        current = api.get_agent(agent_id)
        agent = api.update_agent(agent_id, current["version"], cfg)
    else:
        agent = api.create_agent(cfg)
    state.data["agent_id"] = agent["id"]
    state.data["agent_version"] = agent["version"]
    state.save()
    return agent
