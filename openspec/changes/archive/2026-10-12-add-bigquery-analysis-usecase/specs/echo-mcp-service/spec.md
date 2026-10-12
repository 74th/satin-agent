# Spec Delta

## Purpose

クラスタ内のMCPサーバとsatin-agentの(A) Managed Agentsとの接続を、Echoツールの呼び出しで検証できるようにする。接続はMCP tunnelsを経由し、認証の成功と拒否の両方を確かめる。

## ADDED Requirements

### Requirement: Echo MCPを提供する
ユースケースは、MCPサーバをKubernetes上に配備しなければならない (SHALL)。このサーバはHTTPで接続でき、受け取った文字列をそのまま返すEchoツールを持つ。サーバはクラスタの外へ直接公開してはならない (SHALL NOT)。(A)のエージェントは、ユースケースの`mcp.json`に記したMCPサーバとして、このツールを使えなければならない (SHALL)。

#### Scenario: エージェントからEchoを呼ぶ
- **WHEN** 利用者がSlackで、指定した文字列のEchoを依頼する。トンネルとEcho MCPは稼働している
- **THEN** エージェントはMCPのEchoツールを呼び、同じ文字列を返す

### Requirement: MCP tunnelsでManaged Agentsとつなぐ
ユースケースは、MCP tunnelsのスタック（Proxyとトンネルのコネクタ）を、Echo MCPとは別のマニフェストとして配備する手順を提供しなければならない (SHALL)。スタックは外向きの接続だけでトンネルを張らなければならない (SHALL)。クラスタに受信用のポートを開けてはならない (SHALL NOT)。Proxyの転送先は、ルートに設定したEcho MCPのServiceに限らなければならない (SHALL)。Echo MCPへの受信は、Proxyからの接続だけに限らなければならない (SHALL)。トンネルトークンとサーバ証明書の秘密鍵は、Kubernetes Secretから供給し、リポジトリに置いてはならない (SHALL NOT)。

#### Scenario: トンネル経由の接続
- **WHEN** トンネルのスタックを配備し、`mcp.json`にトンネルのURL（`https://<サブドメイン>.<トンネルのドメイン>/mcp`）を設定してAgentへ配布する
- **THEN** Managed AgentsのセッションからEcho MCPのツールを列挙して呼び出せる

#### Scenario: Proxy以外からの接続
- **WHEN** Echo MCPと同じクラスタの別のPod（Proxyではない）から、Echo MCPのServiceへ接続する
- **THEN** 接続は拒否される

### Requirement: 全てのMCP要求を認証する
Echo MCPは、設定された鍵と一致する要求だけを処理しなければならない (SHALL)。鍵は`X_API_KEY`ヘッダーで受け取る。Managed AgentsのVaultの`static_bearer`の資格情報で届く`Authorization: Bearer`でも受け取る。鍵がないか一致しない要求は、ツールを実行する前に拒否しなければならない (SHALL)。鍵はKubernetes Secretから供給し、リポジトリのファイル・応答・通常のログに出してはならない (SHALL NOT)。鍵が設定されていない場合、サーバは起動してはならない (SHALL NOT)。

#### Scenario: 正しい鍵を使う
- **WHEN** Vaultの資格情報を付けたセッションからEchoを呼ぶ。または、正しい`X_API_KEY`を付けてMCPの要求を送る
- **THEN** MCPの接続とEchoの呼び出しが成功する

#### Scenario: 鍵がない、または誤っている
- **WHEN** 鍵を付けずに、または設定と異なる鍵を付けてMCPの要求を送る
- **THEN** サーバは認証エラーを返し、Echoツールは実行されない

### Requirement: 疎通を検証する手順を提供する
ユースケースは、配備の後に次の3点を確かめる手順を提供しなければならない (SHALL)。
- クラスタ内からの直接の接続での、正しい鍵・鍵なし・誤った鍵の結果
- トンネルを経由したManaged AgentsのセッションからのEchoの呼び出し
- Slackを通したEchoの呼び出し

#### Scenario: 配備を検証する
- **WHEN** 運用者が手順書の疎通検証を実行する
- **THEN** 成功するときは入力と同じEchoの結果を、拒否されるときは認証エラーを確認できる
