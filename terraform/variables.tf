variable "project_id" {
  description = "satin-agent のリソースを作成する Google Cloud プロジェクト ID"
  type        = string
  default     = "nnyn-dev"
}

variable "region" {
  description = "Model Armor / DLP / Artifact Registry のリージョン。設定ファイルの値と一致させる。"
  type        = string
  default     = "us-central1"
}

variable "assets_bucket_name" {
  description = "Skills / CLAUDE.md / startup.sh を置く GCS バケット名（グローバルに一意）"
  type        = string
  default     = "nnyn-dev-satin-agent-assets"
}

variable "model_armor_template_id" {
  description = "出力の検査に使う Model Armor テンプレート ID（silk-agent の simple とは別）"
  type        = string
  default     = "satin-simple"
}

variable "artifact_registry_repo" {
  description = "コンテナイメージ（ランタイム・アプリ）を置く Artifact Registry のリポジトリ名"
  type        = string
  default     = "satin-agent"
}

variable "system_namespace" {
  description = "オーケストレータとBotを動かすnamespace"
  type        = string
  default     = "satin-system"
}

variable "sandbox_namespace" {
  description = "セッションごとのSandboxを動かすnamespace"
  type        = string
  default     = "satin-sandboxes"
}

variable "bot_ksa" {
  description = "Botが使うKubernetes ServiceAccount名（k8s/base/serviceaccount.yamlと一致させる）"
  type        = string
  default     = "satin-slack-bot"
}

variable "sandbox_ksa" {
  description = "Sandboxが使うKubernetes ServiceAccount名"
  type        = string
  default     = "satin-sandbox"
}
