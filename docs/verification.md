# 検証記録

satin-agent（(A) Claude Managed Agents、(B) Claude Code のself-hosted Environment）を、実際のGKE Agent Sandboxで動かして確かめた結果。

- 環境: GKEクラスタ `gke_nnyn-dev_us-central1-a_silk-agent-gke`（Agent Sandbox controller v1.0.6、gVisor、FQDNNetworkPolicy）、namespaceは`satin-system` / `satin-sandboxes`
- 実施日: 2026-10-11〜12
- モデル: (A) `claude-haiku-5-5`（Skillsは`claude-sonnet-5-5`でも確認）、(B) claude.aiのClaude Code
- 判定: 固有の値は文字列一致、意味はJev（`tests/live/jev.py`、`noul > 0.3`）。判定値と使用トークン数は結果のJSON（`results/`）に残している。
- 再実行できるもの: 自動シナリオは`verification/run_cma.py`・`verification/run_bot.py`・`verification/bot_confirm_restart.py`。手で操作したものの手順は`verification/manual_scenarios.md`。

## 自動シナリオ

`results/`のJSONから`verification/make_report.py`が生成した表（根拠のイベントID・ログの行はJSONに記録）。

<!-- auto:begin -->

### (A) Managed Agents + Sandbox（Haiku）

実施日: 2026-10-12、結果: `cma-final-20261012-132528.json`

| シナリオ | 結果 | モデル | 観察した振る舞い |
| --- | --- | --- | --- |
| `tool_in_sandbox` | 合格 | claude-haiku-5-5 | イメージ識別子 'satin-runtime-20261012k' が結果に含まれた |
| `skills` | 合格 | claude-haiku-5-5 | Skillのファイルを読んだ=True、識別子を答えた=True（Jev noul=0.97） |
| `mcp` | 未実施 | claude-haiku-5-5 | HTTPのMCPサーバが設定されていない（--mcp-url と、そのMCPをAgentへ配布する必要がある） |
| `secrets_hidden` | 合格 | claude-haiku-5-5 | APIキー・environment key・work secretの値は出力に含まれない |
| `egress_blocked` | 合格 | claude-haiku-5-5 | example.comへの通信は失敗した |
| `resume_workspace` | 合格 | claude-haiku-5-5 | idle後にSandboxは停止=True、再開後にnote.txtを読めた=True |
| `retention_new_workspace` | 合格 | claude-haiku-5-5 | 古いワークスペースは引き継がれず（世代=2）、note.txtはない |
| `parallel_sessions` | 合格 | claude-haiku-5-5 | 2つのSandboxが同時にRunning=True、応答までの秒数={'a': 62.2, 'b': 62.5} |
| `bad_image` | 合格 | claude-haiku-5-5 | 起動の失敗後、Sandboxは停止=True（35.7秒） |
| `kill_pod_during_tool` | 合格 | claude-haiku-5-5 | 自動では回復せず、オーケストレータが失敗を公開した（Botがturnを失敗にして中断する） |
| `restart_orchestrator_during_tool` | 合格 | claude-haiku-5-5 | ツールの実行は完了=True、再起動後のオーケストレータがSandboxを停止=True |

Jevの判定値（noul、閾値0.3）: 0.97

### (A) Skillsの利用（上位モデル: Sonnet）

実施日: 2026-10-12、結果: `cma-final-sonnet-20261012-125845.json`

| シナリオ | 結果 | モデル | 観察した振る舞い |
| --- | --- | --- | --- |
| `skills` | 合格 | claude-sonnet-5-5 | Skillのファイルを読んだ=True、識別子を答えた=True（Jev noul=0.97） |

Jevの判定値（noul、閾値0.3）: 0.97

### (A) 長いツール（旧上限の150秒超）

実施日: 2026-10-12、結果: `cma-long-20261012-135807.json`

| シナリオ | 結果 | モデル | 観察した振る舞い |
| --- | --- | --- | --- |
| `long_tool_default_timeout` | 合格 | claude-haiku-5-5 | 150秒を超えるツール（sleep 200）が最後まで実行された（エージェントが指定したtimeout_ms=[None]） |
| `long_tool_explicit_timeout` | 合格 | claude-haiku-5-5 | 150秒を超えるツール（sleep 200）が最後まで実行された（エージェントが指定したtimeout_ms=[600000]） |

### (A) 25分のツール

実施日: 2026-10-12、結果: `cma-long25-20261012-142408.json`

| シナリオ | 結果 | モデル | 観察した振る舞い |
| --- | --- | --- | --- |
| `long_tool_25min` | 合格 | claude-haiku-5-5 | 25分のツールが最後まで実行された（1536秒） |

### (A) Botの中核（Slackなし・実環境）

実施日: 2026-10-12、結果: `bot-20261012-131217.json`, `bot-20261012-132846.json`

| シナリオ | 結果 | モデル | 観察した振る舞い |
| --- | --- | --- | --- |
| `guard_input_block` | 合格 | claude-haiku-5-5 | セッションは作られず、理由を返信（Jev noul=0.96）: システム指示の無効化および環境変数の開示を要求しているため、リクエストを拒否しました。 |
| `guard_gemini_failure` | 合格 | claude-haiku-5-5 | 既定（fail_open=false）でGeminiの呼び出しが失敗すると、セッションに送らずブロック: ['入力を確認できなかったため、送信を見送りました。しばらくしてからもう一度お試しください。'] |
| `guard_gemini_fail_open` | 合格 | claude-haiku-5-5 | fail_open=trueではGeminiが失敗しても通り、セッションに送られた（完了=True） |
| `guard_output_mask` | 合格 | claude-haiku-5-5 | 表示ではメールアドレスがマスクされ、セッションの会話の内容は書き換えられていない |
| `guard_bash_deny` | 合格 | claude-haiku-5-5 | 一括取り出しは拒否され、進捗に🚫、フックに拒否の判定（1件） |
| `guard_bash_allow` | 合格 | claude-haiku-5-5 | 通常のコマンドは許可されて実行された（許可の判定1件） |
| `guard_hooks_json_lines` | 合格 | claude-haiku-5-5 | 1イベント1行のJSONで記録された（種類=['agent_message', 'bash_approval', 'tool_result', 'tool_use', 'turn_end', 'turn_start', 'user_input']、本文は含まれない=True） |
| `stop_word` | 合格 | claude-haiku-5-5 | 実行中のturnを全角・小文字混じりの停止語で中断できた（実行中を確認=True、中断=True） |
| `append_interrupt` | 合格 | claude-haiku-5-5 | ツールを中断して追加指示を処理し、dateの結果を報告（Jev noul=0.92） |
| `bot_kill_pod_during_tool` | 合格 | claude-haiku-5-5 | Pod削除後、turnは「失敗」で終わり、その後の依頼も通った（実行中を確認=True、削除したPod=1件） |

Jevの判定値（noul、閾値0.3）: 0.96, 0.92

### (A) bashの確認待ちの間のBot再起動

実施日: 2026-10-12、結果: `bot-confirm-restart-20261012-132601.json`

| シナリオ | 結果 | モデル | 観察した振る舞い |
| --- | --- | --- | --- |
| `bot_restart_during_bash_confirmation` | 合格 | claude-haiku-5-5 | 確認待ち=True、再起動後のBotが確認に応答=True、turnは完了=True |

Jevの使用トークン数（この表の結果の合計）: 入力 1787、出力 100

<!-- auto:end -->

## 手で操作したシナリオ

### (A) Slackから（10.5 / 10.5a）

操作は2026-10-12（JST）。判定は、Slackの表示、Botのログ、セッションのイベント、オーケストレータのログを突き合わせて行った。

| # | シナリオ | 結果 | 観察した振る舞い |
| --- | --- | --- | --- |
| 1 | 進捗表示（`ls`と`cat /etc/satin-image-id`） | 合格 | 進捗が「完了」でツールが✅、返答は別メッセージ。識別子`satin-runtime-20261011i`を答えた（エージェントが2つを1コマンドにまとめたため、ツール表示は1件） |
| 2 | 停止語（`sleep 120`の実行中に「停止」） | 合格 | セッションに`user.interrupt`。サーバが「Tool execution was interrupted」を記録し、進捗は「中断」（ツールは⏹）。以後の指示は通常どおり処理された |
| 3 | 実行中の追加指示（`sleep 30`の実行中） | 合格 | ツールを中断し、追加指示が送られ、`sleep 30; date`が実行された（結果 `Sun Oct 11 23:34:46 UTC 2026`）。進捗に「追加の指示: 1件」。※方式はdesign.md D10（ツール実行中は`user.message`を送れないため、中断してから送る） |
| 4 | ツール実行中（`sleep 90`）のBot再起動 | 合格 | turnは止まらず完了。再起動したBotが進捗を「完了」にし、返答を投稿した（`recovered 2 turn(s)`） |
| 5 | bashの確認待ちの間のBot再起動 | 合格 | 自動で再現（`verification/bot_confirm_restart.py`）。Botを止めて確認待ちのセッションを作り、再起動後のBotが`user.tool_confirmation`で応答し、turnが完了 |
| 6 | 応答範囲の制限（許可リストにないユーザ） | 合格 | 返信なし（「受け付けました」も出ない）。監査フックに`user_input / excluded`、セッションの作成は0件 |
| 7 | `AGENT_USER_EMAIL` | 合格 | スレッドを始めたユーザのメールが入っていた（Model Armorのマスクで`[EMAIL_ADDRESS]`と表示され、無効にして値を確認）。※別ユーザが書き込んでも変わらないことは未実施（アカウントが1つ） |
| 8 | Sandbox起動中の表示 | 合格 | 受付「⏳ 受け付けました。処理を開始しています…」→「作業環境を起動中」→実行中 |
| 9 | 起動失敗の表示（存在しないイメージ） | 合格 | 約1分で進捗が「失敗」になり、作業環境を起動できなかったことと次にすることが投稿された。続けて送った依頼も受け付けられた（初回は不具合あり。下記） |
| a1 | 入力ガード（上書き要求） | 合格 | 「処理しませんでした」に更新され、理由が返信された。フックに`block`が記録（Jev 0.89） |
| a2 | 出力のマスク | 合格 | 返答のメールアドレスが`[EMAIL_ADDRESS]`で表示された（自動検証ではセッション側が書き換えられていないことも確認） |
| a3 | 一括取り出し`tar czf all.tgz .` | 合格 | 実行は拒否され、進捗に🚫。エージェントが理由を受け取り、回避を試みなかった |
| a4 | 通常のコマンド（`cat a.txt`、`grep -rn`） | 合格 | どちらも許可されて実行された（結果は「ファイルなし」「一致なし」で、拒否ではない） |
| a5 | Gemini障害時のブロック | 合格 | 自動（`guard_gemini_failure`）。既定では送らずにブロックし、`fail_open=true`では通る |
| a6 | 監査フック | 合格 | 1イベント1行のJSON。入力のブロック1件、bashの承認（許可6・拒否1）、返答のマスク1件を記録。本文は既定で出ない |

### (B) claude.aiから（10.6）

| # | シナリオ | 結果 | 観察した振る舞い |
| --- | --- | --- | --- |
| 1 | Sandbox内でのツール実行 | 合格 | `cat /etc/satin-image-id`が`satin-runtime-20261012e`。セッションごとにSandboxが1つ作られた |
| 2 | Skills・`CLAUDE.md`（リポジトリなし） | 合格 | `container-probe`が列挙された。`CLAUDE.md`は各セッションの設定ディレクトリに複製され、内容（日本語で簡潔に、絵文字なし）どおりに答えた |
| 3 | 同（リポジトリあり） | 未実施 | 利用者のユースケースでリポジトリを指定しないため |
| 4 | `startup.sh` | 合格 | `/workspace/.satin`に`assets`と`startup-done`がある |
| 5 | `AGENT_USER_EMAIL` | 合格 | claude.aiのアカウントのメールになった（`CLAUDE_RUNNER_ACCOUNT_EMAIL`から） |
| 6 | idle後の再開 | 合格 | idle解放でrunnerが終了しSandboxが停止（10分後）。次の書き込みで同じSandbox・同じPVCが再開され、`ls /`の結果を書いた`note.txt`を答えた |
| 7 | ユーザ入力待ちの間にPodを削除 | 合格 | 削除後にSandboxは停止。次の書き込みで再開し、`note.txt`の1行目（`bin`）を答えた |
| 8 | オーケストレータの再起動 | 合格 | Podを削除して停止状態にし、オーケストレータを再起動した後の書き込みで、再開して`date`が返った |
| 9 | 2セッションの並行 | 合格 | 2つのSandboxが8秒差で作られ、約220秒間並行して動き、どちらも終了コード0 |
| 10 | 秘密が見えない | 合格 | 見えた名前は`CLAUDE_CODE_SESSION_ACCESS_TOKEN`・`CLAUDE_CODE_MESSAGING_TOKEN`・`CLAUDE_SESSION_INGRESS_TOKEN_FILE`など。environment secretは見えない。※セッション専用のトークンが見えるのは既知の制限 |
| 11 | 長いツール（既定の上書き） | 合格 | `python3 -c "import time; time.sleep(200); ..."`を`timeout_ms`未指定で実行し、旧既定（2分）を超えて正常終了（`long-done`）。`BASH_DEFAULT_TIMEOUT_MS`が効いている |
| 12 | 長いツール（上限の上書き） | 合格 | 12分のコマンドを`timeout_ms`=15分で実行し、旧上限（10分）を超えて正常終了。その間にidle解放でも中断されず、Sandboxは約14分動いて終了コード0で停止。`BASH_MAX_TIMEOUT_MS`が効いている |

## 満たせなかった・未実施のケース

| ケース | 理由 |
| --- | --- |
| (A) HTTPのMCPサーバの利用（10.2） | 検証に使えるHTTPのMCPサーバがなかった。`run_cma.py`の`mcp`は`--mcp-url`で実行できる |
| 別ユーザが書き込んでも`AGENT_USER_EMAIL`が変わらない | Slackのアカウントが1つ。メールはセッションの作成時にSandboxの環境変数に固定される設計（セッションIDごとの対応表） |
| (B) リポジトリを指定したセッション | 利用者のユースケースでは使わない |
| 実行中のツールを止めずに、追加指示をエージェントへ届けること | Managed Agentsは、ツール実行中の`user.message`を拒否する（400）。bashにバックグラウンド実行もない。そのため、追加指示はツールを`user.interrupt`で中断してから送る（design.md D10、specを更新済み） |
| `user.interrupt`をワーカー・オーケストレータから送ること | work secretの権限では`401`、環境キーでは`403`。ワーカーが落ちた後の収束は、再実行と失敗の公開で行う（下記・design.md「ワーカーが落ちたとき」） |
| ワーカーが落ちた後に、リースを引き継いで必ず続きを処理すること | サーバ側のリースが約5分残り、再実行したワーカーは手を引く。自動で回復することも、失敗が公開されることもある（どちらでも黙って放置はされない） |

## 既知の制限

- bashツールのタイムアウトは、既定1時間、上限2時間（設定で変更可）。エージェントが`timeout_ms`を明示するとその値が優先される（上限までの範囲で）。(B)のClaude Codeには環境変数で渡しており、実機で確認済み（既定を超える200秒、旧上限10分を超える12分）。

- (A)のwork secretはマウントしたファイルとして、(B)のセッショントークンは環境変数として、同じユーザのツールから読める。どちらもそのセッション専用の短命な値で、外向きの通信はFQDNNetworkPolicyで制限している。
- (B)にはガード類がない（入出力の通り道がない）。
- ツール実行中にPodが消えたとき、猶予（45秒）内に終わらないツールは、自動で回復する場合と、失敗が公開される場合がある（Botは失敗をスレッドに通知し、次の依頼を受けられる）。Botのないセッション（API直接など）は、失敗の公開までで、ユーザが新しいメッセージを送る必要がある。落ちたワーカーのリースが、サーバ側で約5分残る。
- 同じEnvironmentにつなぐオーケストレータは1つにする。別の場所にあるとspawn hintを取り合う（このマシンには別のオーケストレータが動いていたため、本番では確認する）。

## 検証で見つけて直した不具合

実環境で動かして初めて分かったもの。いずれもユニットテストか検証シナリオに回帰テストを足した。

1. **Sandboxの作成直後にcontrollerがstatusを更新**し、`resourceVersion`が変わって競合する → 冪等なmerge patchで`Running`にする。
2. **`kubernetes_asyncio`のpatchが`400`**（JSON Patchとして送られる）→ `Content-Type: application/merge-patch+json`を指定。
3. **Sandboxから、GCS（アセット）の資格情報を取れない**: 外向き通信が全拒否で、Workload Identityのメタデータサーバ宛も遮断されていた → メタデータサーバ宛だけを許可するポリシーを追加。
4. **Botが`user.tool_result`を扱っていない**: Sandbox内のワーカーはツールの結果を`user.tool_result`で返す。ツールが「実行中」のまま残っていた。
5. **保持期限切れ後の再作成が失敗**: 古い世代のSandboxの削除で、同じ名前のSecretが回収され、新しい世代のSecretまで消えた → Secret名に世代を含める。
6. **準備完了にならないSandboxが、オーケストレータの再起動後に残り続ける**（イメージを取得できない等）→ 引き継ぎ時にも準備完了の上限時間を適用する。
7. **ツール実行中・起動準備中にPodが消えると、セッションが結果待ちのまま放置**される。原因は、SDKがSIGTERMのキャンセルでもwork itemを`force-stop`すること、落ちたワーカーのリースが約5分残ること。→ SIGTERMでキャンセルせず猶予内に待つ（超えたら後始末なしで終了）、異常終了したSandboxを同じwork itemで再実行（上限2回）、再実行が手を引いたら`failed`を公開してリースを解放、Botは失敗を通知して`user.interrupt`で次の依頼を受けられる状態に戻す（design.md「ワーカーが落ちたとき」）。
8. **Sandboxの起動失敗を、Botがturnの失敗として扱っていない**: 進捗が「実行中」のまま止まった → 失敗を検出してturnを失敗にし、結果待ちのセッションに`user.interrupt`を送って次の依頼を受けられるようにした。
9. **セッション作成後・最初の指示の送信前にBotが落ちる**と、turnが空のセッションで`running`のまま残る → 最初の指示を保存して再送し、保存がなければ失敗にして案内する。
10. **再開時の「env同一性」の照合が厳しすぎる**: アセットのURLやidleの設定を変えただけで、既存の会話が再開できない（`SandboxEnvMismatch`）→ Sandboxに書かれたモード・セッションID・ユーザだけを直接比較する。
11. **ガードが正当な依頼まで止める**: `echo $AGENT_USER_EMAIL`が「環境変数の開示」としてブロックされた → 設定`guards.input_guard.enabled`で無効にできる（判定の基準は`guards/input_guard.md`で調整できる）。
12. **反応が遅く見える**: ガードとSandboxの起動の後に初めて進捗が出た → メッセージを受けた直後に受付のメッセージを出し、そのまま進捗として更新する。
13. 進捗の時刻がUTC → `slack.timezone`（既定`Asia/Tokyo`）。
14. 中断したツールが失敗（❌）と区別できない → 中断（⏹）と表示する。
15. `claude self-hosted-runner`は`--flag=value`形式を受け付けない / `--host-config-dir`というフラグはなく環境変数`SELF_HOSTED_RUNNER_HOST_CONFIG_DIR`で渡す。
16. **1回のツール呼び出しが150秒で打ち切られる**: SDKのrunnerに、`timeout_ms`とは別の外側の上限（`TOOL_TIMEOUT`=150秒）があり、長いクエリ（30分など）が実行できない。→ Sandboxの起動時に、bashの既定（`timeout_ms`未指定のとき。既定1時間）と、1回のツール呼び出しの上限（既定2時間）を、設定（`sandbox.bash_default_timeout_seconds` / `bash_max_timeout_seconds`）で上書きする。(B)のClaude Codeには`BASH_DEFAULT_TIMEOUT_MS` / `BASH_MAX_TIMEOUT_MS`で渡す（実機で確認済み）。

## 再現手順

```sh
# 1. 準備（README「セットアップとデプロイ」）
scripts/build-images.sh <tag> && KUBE_CONTEXT=<ctx> scripts/deploy.sh <tag>

# 2. (A)の自動シナリオ（実クラスタ。--expected-image-id はruntimeイメージのIMAGE_ID）
uv run python -m verification.run_cma --context <ctx> --expected-image-id satin-runtime-<tag> --label cma-final
uv run python -m verification.run_cma --context <ctx> --expected-image-id satin-runtime-<tag> \
    --only skills --state .satin-state-verify-sonnet.json --model claude-sonnet-5-5 --label cma-final-sonnet

# 3. Botの中核（Slackなし。障害のシナリオを含む）
uv run python -m verification.run_bot                        # 結果は results/bot-*.json
uv run python verification/bot_confirm_restart.py <slack-channel-id>

# 4. 手で操作するシナリオは verification/manual_scenarios.md
# 5. この文書の自動シナリオの表を更新
uv run python -m verification.make_report
```

## BigQuery分析ユースケース（usecases/bigquery-analysis）

実施日: 2026-10-12。クラスタ: `gke_nnyn-dev_us-central1-a_silk-agent-gke`。

| 項目 | 結果 |
|---|---|
| Terraform（GSA・IAM）の適用 | 5リソースを追加。GSAは`satin-bq-sandbox@nnyn-dev.iam.gserviceaccount.com` |
| KSAの注釈 | `satin-sandboxes/satin-bq-sandbox`に`iam.gke.io/gcp-service-account`が付き、`automountServiceAccountToken: false`。Terraformのなりすまし許可（`nnyn-dev.svc.id.goog[satin-sandboxes/satin-bq-sandbox]`）と一致 |
| gVisor Pod（KSA `satin-bq-sandbox`）で`run_query.py` | 成功（終了コード0）。列は`refresh_date`・`rank`・`term`、10行、日付の降順・順位の昇順 |
| gVisor Pod（既定のKSA `satin-sandbox`）で`run_query.py` | `permission`（`bigquery.jobs.create`なし）、終了コード4。結果は出力されない |
| Echo MCPへの直接接続（6.3） | 合格。Sandboxのラベルを付けたPodから、`X_API_KEY`・Bearerとも正しい鍵で200、鍵なし・誤りで401。Sandbox以外のPod（default namespace）からは接続できない（タイムアウト） |
| トンネル経由のManaged Agents（6.4） | 見送り（MCP tunnelsは利用申請が未承認。(A)のMCPは断念。回避策はRESTのAPIをcurlで呼ぶ） |
| Managed Agentsのセッションから分析を依頼（Slackなし、`usecases/bigquery-analysis/scripts/verify_cma.py`、`results/bigquery-cma-20261012-170606.json`） | 合格。エージェントが`run_query.py`を実行し、10行すべての検索語と日付（2026-10-11）・取得条件を回答に含めた。Pod（オーケストレータが作ったSandbox）のKSAは`satin-bq-sandbox`。背景は推測と断り、確認できないと述べた。これにより、Sandbox単位のNetworkPolicyで`bigquery.googleapis.com`への通信が通ることも確かめた |
| (B) Claude Code self-hosted runnerからEcho MCP | 合格。claude.aiでself-hosted Environmentを選んで始めたセッションで、Echoツールの呼び出しが成功し「こんにちは12345」がそのまま返った。クラスタ側の確認: Sandboxはモード`cc`、KSA `satin-bq-sandbox`で作成、環境変数`ECHO_MCP_API_KEY`はSecret（`echo-mcp-api-key`）から供給、`startup.sh`がホスト設定ディレクトリの`.claude.json`にMCPを登録（鍵は環境変数の参照のみで、ファイルには書かれていない）、Sandboxの`-pods`のNetworkPolicyが作られ、Echoのログにツールの引数の検証の記録（初回の呼び出しで引数名を誤り、再試行で成功）。runnerはホスト設定ディレクトリの`.claude.json`を読む（`.mcp.json`は不要だった） |

注: この検証のPodは、配備を入れ替えずに作った一時のPod（`runtimeClassName: gvisor`）で、オーケストレータが作るSandboxではない。そのため、`bigquery.googleapis.com`のFQDN許可（Sandbox単位のNetworkPolicy）は、ここでは経路として確かめていない（単体テストで描画だけを確認）。Podは検証後に削除した。

## アセットのzip化（bundle-assets-as-zip）

実施日: 2026-10-12。クラスタ: `gke_nnyn-dev_us-central1-a_silk-agent-gke`。

| 項目 | 結果 |
|---|---|
| 切り替え（イメージのpush → `satin publish --target gcs` → `scripts/deploy.sh` → `satin sandbox gc --all`） | 実施。`gs://nnyn-dev-satin-agent-assets/satin/default/`には`assets.zip`だけが残った（従来のファイルと`manifest.json`は削除された）。古いSandbox 3件を削除 |
| (A) Sandbox内のツール実行・Skillの利用（`results/zip-bundle-20261012-174221.json`） | 合格。新しいイメージのSandboxが`assets.zip`を取得・展開して起動し、`container-probe`のSkillで識別子`satin-runtime-zip1`を答えた |
| (B) SkillsとCLAUDE.mdの読み込み | 合格。claude.aiのself-hosted Environmentで始めたセッションで、`container-probe`のSkillにより識別子`satin-runtime-zip1`が返った。Sandbox（モード`cc`）では、`assets.zip`の展開結果が`/workspace/.satin/assets`に、SkillsとCLAUDE.mdがホスト設定ディレクトリ（`~/.claude-host`）に置かれ、CLAUDE.mdの内容（日本語・絵文字なし）も反映されていた |
