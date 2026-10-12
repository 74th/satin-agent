# Spec Delta

## ADDED Requirements

### Requirement: セッションへのVaultの付与
Botは、設定にVaultのIDがある場合、スレッドのセッションを作るときにそのVaultを付けなければならない (SHALL)。これにより、Agentに登録したMCPサーバへの資格情報をManaged Agentsが使えるようにする。設定がない場合は、Vaultを付けずにセッションを作らなければならない (SHALL)。Botは、Vaultの資格情報の値を持ってはならず (SHALL NOT)、ログに出してはならない (SHALL NOT)。

#### Scenario: Vaultを設定している
- **WHEN** 設定にVaultのIDを1つ書いてBotを起動し、新しいスレッドを始める
- **THEN** 作られたセッションには、そのVaultが付いている

#### Scenario: Vaultを設定していない
- **WHEN** VaultのIDを設定せずにBotを起動し、新しいスレッドを始める
- **THEN** セッションはVaultなしで作られ、従来どおり動く
