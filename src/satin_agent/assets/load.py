"""アセットディレクトリの読み込みと検証。

assets/
  skills/<name>/SKILL.md (+付属ファイル)
  CLAUDE.md
  startup.sh
  mcp.json   {"mcp_servers": [{"type": "url", "name": "...", "url": "https://..."}]}
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class AssetError(Exception):
    pass


@dataclass(frozen=True)
class Skill:
    name: str
    files: dict[str, bytes]  # スキルディレクトリからの相対パス → 内容


@dataclass(frozen=True)
class Assets:
    root: Path
    skills: dict[str, Skill] = field(default_factory=dict)
    claude_md: str | None = None
    startup_sh: bytes | None = None
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)

    def files(self) -> dict[str, bytes]:
        """GCSに置くファイル（相対パス → 内容）。manifest.jsonは含まない。"""
        out: dict[str, bytes] = {}
        for s in self.skills.values():
            for rel, data in s.files.items():
                out[f"skills/{s.name}/{rel}"] = data
        if self.claude_md is not None:
            out["CLAUDE.md"] = self.claude_md.encode()
        if self.startup_sh is not None:
            out["startup.sh"] = self.startup_sh
        mcp = self.root / "mcp.json"
        if mcp.is_file():
            out["mcp.json"] = mcp.read_bytes()
        return out


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_assets(root: str | Path) -> Assets:
    r = Path(root)
    if not r.is_dir():
        raise AssetError(f"アセットディレクトリが見つかりません: {r}")

    skills: dict[str, Skill] = {}
    skills_dir = r / "skills"
    if skills_dir.is_dir():
        for d in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
            if not (d / "SKILL.md").is_file():
                raise AssetError(f"SKILL.md がありません: skills/{d.name}")
            files = {
                str(f.relative_to(d)): f.read_bytes() for f in sorted(d.rglob("*")) if f.is_file()
            }
            skills[d.name] = Skill(d.name, files)

    claude_md = (r / "CLAUDE.md").read_text() if (r / "CLAUDE.md").is_file() else None
    startup = (r / "startup.sh").read_bytes() if (r / "startup.sh").is_file() else None
    return Assets(r, skills, claude_md, startup, _load_mcp(r / "mcp.json"))


def _load_mcp(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise AssetError(f"mcp.json がJSONとして読めません: {e}") from e
    servers = data.get("mcp_servers", [])
    if not isinstance(servers, list):
        raise AssetError("mcp.json の mcp_servers は配列にしてください")
    out = []
    for s in servers:
        name = s.get("name", "?") if isinstance(s, dict) else "?"
        if not isinstance(s, dict) or "command" in s or s.get("type") == "stdio":
            raise AssetError(f"mcp.json: stdioのMCPサーバは使えません（HTTPのみ）: {name}")
        if s.get("type") != "url" or not s.get("url") or not s.get("name"):
            raise AssetError(f"mcp.json: type=url と name / url が必要です: {name}")
        out.append({"type": "url", "name": s["name"], "url": s["url"]})
    return out
