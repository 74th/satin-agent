# Spec Delta

## ADDED Requirements

### Requirement: MCPサーバのAgentへの登録
配布スクリプトは、アセットディレクトリの`mcp.json`に記したHTTPのMCPサーバを、(A)のAgentに登録しなければならない (SHALL)。登録した各サーバは、エージェントがツールとして使える状態にしなければならない (SHALL)。`mcp.json`がないか、サーバが空の場合は、AgentにMCPサーバもMCPのツールも付けてはならない (SHALL NOT)。stdioのサーバと、名前かURLのないサーバは、APIを呼ぶ前にエラーで止めなければならない (SHALL)。

#### Scenario: MCPサーバを登録する
- **WHEN** `mcp.json`に`echo`というHTTPのMCPサーバを書いて配布する
- **THEN** Agentの新しいバージョンに`echo`のMCPサーバと、そのツールを使う設定が含まれる

#### Scenario: MCPサーバがない
- **WHEN** `mcp.json`の`mcp_servers`が空のアセットを配布する
- **THEN** AgentにはMCPサーバもMCPのツールも含まれない
