#!/usr/bin/env python3
"""Managed AgentsのVaultと、Echo MCP用のstatic_bearer資格情報を作る。

  uv run python usecases/bigquery-analysis/scripts/create-vault.py \
      [--state .satin-state-bigquery.json]

- 資格情報の対象は、アセットの mcp.json にある `echo` のURL（トンネルのURL）。
- トークンは .env / 環境変数の ECHO_MCP_API_KEY。値は出力せず、状態ファイルにも書かない。
- 状態ファイルに書くのはVaultのIDだけ。2回実行してもVaultは増えない。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

SERVER_NAME = "echo"
VAULT_NAME = "satin-bigquery-analysis"


def echo_url(assets_dir: Path) -> str:
    servers = json.loads((assets_dir / "mcp.json").read_text()).get("mcp_servers", [])
    for s in servers:
        if s.get("name") == SERVER_NAME:
            return s["url"]
    raise SystemExit(f"error: mcp.json に {SERVER_NAME} がありません")


def ensure_vault(client: Any, state_path: Path, url: str, token: str) -> str:
    state: dict[str, Any] = json.loads(state_path.read_text()) if state_path.exists() else {}
    vault_id = state.get("vault_id")
    if not vault_id:
        vault_id = client.beta.vaults.create(display_name=VAULT_NAME).id
        state["vault_id"] = vault_id
        state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    existing = [
        getattr(getattr(c, "auth", None), "mcp_server_url", None)
        for c in client.beta.vaults.credentials.list(vault_id)
    ]
    if url not in existing:
        client.beta.vaults.credentials.create(
            vault_id,
            display_name=f"{SERVER_NAME} (static bearer)",
            auth={"type": "static_bearer", "mcp_server_url": url, "token": token},
        )
    return vault_id


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[3]
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--state", default=".satin-state-bigquery.json")
    ap.add_argument("--assets", default=str(root / "usecases/bigquery-analysis/assets"))
    args = ap.parse_args(argv)

    from satin_agent.config import anthropic_client, load_dotenv

    load_dotenv()
    token = os.environ.get("ECHO_MCP_API_KEY", "")
    if not token:
        print("error: ECHO_MCP_API_KEY が設定されていません", file=sys.stderr)
        return 1
    url = echo_url(Path(args.assets))
    vault_id = ensure_vault(anthropic_client(), Path(args.state), url, token)
    print(f"vault_id={vault_id} (credential for {url})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
