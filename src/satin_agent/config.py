"""設定の読み込み。YAMLと環境変数。相対パスは設定ファイルのあるディレクトリが基準。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    pass


class MissingSecretError(ConfigError):
    pass


@dataclass(frozen=True)
class Config:
    data: dict[str, Any]
    base_dir: Path

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def path(self, dotted: str, default: str | None = None) -> Path | None:
        """設定値をパスとして返す。相対パスは設定ファイル基準。"""
        raw = self.get(dotted, default)
        if raw is None:
            return None
        p = Path(str(raw)).expanduser()
        return p if p.is_absolute() else (self.base_dir / p).resolve()


def load_config(path: str | os.PathLike[str] | None) -> Config:
    if path is None:
        return Config({}, Path.cwd())
    p = Path(path).resolve()
    if not p.is_file():
        raise ConfigError(f"設定ファイルが見つかりません: {p}")
    data = yaml.safe_load(p.read_text()) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"設定ファイルのトップはマッピングにしてください: {p}")
    return Config(data, p.parent)


def anthropic_api_key(env: Mapping[str, str] | None = None) -> str:
    """`CLAUDE_API_KEY`を優先、なければ`ANTHROPIC_API_KEY`。値はログに出さない。"""
    e = os.environ if env is None else env
    for name in ("CLAUDE_API_KEY", "ANTHROPIC_API_KEY"):
        v = e.get(name)
        if v:
            return v
    raise MissingSecretError("CLAUDE_API_KEY（または ANTHROPIC_API_KEY）が設定されていません")


def jev_api_key(env: Mapping[str, str] | None = None) -> str:
    e = os.environ if env is None else env
    v = e.get("JEV_API_TOKEN")
    if not v:
        raise MissingSecretError("JEV_API_TOKEN が設定されていません")
    return v


def anthropic_client(env: dict[str, str] | None = None):
    """APIキーを明示的に渡したクライアント（SDK既定の環境変数には頼らない）。"""
    import anthropic

    return anthropic.Anthropic(api_key=anthropic_api_key(env))


def load_dotenv(path: str | os.PathLike[str] = ".env", env: dict[str, str] | None = None) -> None:
    """`.env`を読み、未設定のキーだけ環境変数に入れる。値はログに出さない。"""
    target = os.environ if env is None else env
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        target.setdefault(k.strip(), v.strip().strip("'\""))
