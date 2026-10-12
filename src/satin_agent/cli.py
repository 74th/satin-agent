"""`satin`コマンド。"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from .config import ConfigError, load_config


def _add_config(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", help="設定ファイル（YAML）。相対パスはこのファイル基準")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="satin", description="satin-agent")
    sub = parser.add_subparsers(dest="command", required=True)

    pub = sub.add_parser("publish", help="アセットをManaged Agents / GCSへ配布する")
    _add_config(pub)
    pub.add_argument("--assets", default="assets", help="アセットディレクトリ")
    pub.add_argument("--target", default="cma,gcs", help="配布先（cma,gcs のカンマ区切り）")

    sb = sub.add_parser("sandbox", help="Sandboxの運用コマンド")
    sbsub = sb.add_subparsers(dest="sandbox_command", required=True)
    gc = sbsub.add_parser("gc", help="保持期限を過ぎたSandboxとPVCを削除する")
    _add_config(gc)
    gc.add_argument("--all", action="store_true", help="期限に関係なくすべて削除する")

    cma = sub.add_parser("cma-orchestrator", help="(A) Managed Agentsのオーケストレータ")
    _add_config(cma)

    cc = sub.add_parser("cc", help="(B) Claude Codeのオーケストレータ")
    ccsub = cc.add_subparsers(dest="cc_command", required=True)
    spawn = ccsub.add_parser("spawn", help="spawn-runnerフック")
    _add_config(spawn)
    rec = ccsub.add_parser("reconcile", help="Podの終了の検出と停止（サイドカー）")
    _add_config(rec)

    slack = sub.add_parser("slack", help="(A) Slack Bot")
    _add_config(slack)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(getattr(args, "config", None))
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    from . import commands

    return commands.dispatch(args, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
