variable "project_id" {
  description = "GSAとIAMを作るGoogle Cloudプロジェクト ID（GKEクラスタのプロジェクト）"
  type        = string
}

variable "job_project_id" {
  description = "BigQueryのジョブを実行（課金）するプロジェクト ID。未指定ならproject_id"
  type        = string
  default     = null
}

variable "assets_bucket_name" {
  description = "アセットのバケット名（既定のterraform/が作ったもの）。GSAに読み取りを与える"
  type        = string
}

variable "gsa_id" {
  description = "Sandbox用GSAのaccount_id"
  type        = string
  default     = "satin-bq-sandbox"
}

variable "sandbox_namespace" {
  description = "Sandboxを動かすnamespace"
  type        = string
  default     = "satin-sandboxes"
}

variable "sandbox_ksa" {
  description = "ユースケース専用のKSA名（overlayのKSAと一致させる）"
  type        = string
  default     = "satin-bq-sandbox"
}
