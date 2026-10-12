"""results/*.json から、docs/verification.md の自動シナリオの表を生成して差し込む。

    uv run python -m verification.make_report

手で操作した結果（Slack / claude.ai）は、docs/verification.md の「手で操作したシナリオ」に書く。
このスクリプトは `<!-- auto:begin -->` と `<!-- auto:end -->` の間だけを書き換える。
"""

from __future__ import annotations

import glob
import json
import os
from pathlib import Path

DOC = Path("docs/verification.md")
BEGIN, END = "<!-- auto:begin -->", "<!-- auto:end -->"

# 表に入れる結果ファイル（ラベル → 見出し）。最新のファイルを使う。
SECTIONS = [
    ("cma-final", "(A) Managed Agents + Sandbox（Haiku）"),
    ("cma-final-sonnet", "(A) Skillsの利用（上位モデル: Sonnet）"),
    ("cma-long", "(A) 長いツール（旧上限の150秒超）"),
    ("cma-long25", "(A) 25分のツール"),
    ("bot", "(A) Botの中核（Slackなし・実環境）"),
    ("bot-confirm-restart", "(A) bashの確認待ちの間のBot再起動"),
]


def files_for(label: str) -> list[str]:
    return sorted(glob.glob(f"results/{label}-2*.json"))


def cell(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def render() -> str:
    out: list[str] = []
    jev_total = [0, 0]
    for label, title in SECTIONS:
        files = files_for(label)
        if not files:
            out.append(f"### {title}\n\n（結果ファイルなし: `{label}`）\n")
            continue
        # シナリオごとに、最新の結果を使う（一部のシナリオだけ取り直したときのため）
        merged: dict[str, tuple[dict, str, str]] = {}
        for f in files:
            d = json.load(open(f))
            for r in d["results"]:
                merged[r["name"]] = (r, d["date"], os.path.basename(f))
            for a_, b_ in d.get("jev_usage", []):
                jev_total[0] += a_
                jev_total[1] += b_
        dates = sorted({v[1] for v in merged.values()})
        srcs = sorted({v[2] for v in merged.values()})
        files_txt = ", ".join(f"`{x}`" for x in srcs)
        out.append(f"### {title}\n\n実施日: {', '.join(dates)}、結果: {files_txt}\n")
        out.append("| シナリオ | 結果 | モデル | 観察した振る舞い |")
        out.append("| --- | --- | --- | --- |")
        for name, (r, _, _) in merged.items():
            mark = {"pass": "合格", "fail": "**不合格**", "skipped": "未実施"}[r["result"]]
            obs = r["observed"] or r["skipped_reason"]
            out.append(f"| `{name}` | {mark} | {r['model']} | {cell(obs)} |")
        out.append("")
        jev = [
            v[0]["evidence"].get("jev_noul")
            for v in merged.values()
            if "jev_noul" in v[0]["evidence"]
        ]
        if jev:
            out.append("Jevの判定値（noul、閾値0.3）: " + ", ".join(f"{j:.2f}" for j in jev) + "\n")
    out.append(
        f"Jevの使用トークン数（この表の結果の合計）: 入力 {jev_total[0]}、出力 {jev_total[1]}\n"
    )
    return "\n".join(out)


def main() -> int:
    text = DOC.read_text()
    a, b = text.index(BEGIN) + len(BEGIN), text.index(END)
    DOC.write_text(text[:a] + "\n\n" + render() + "\n" + text[b:])
    print(f"updated {DOC}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
