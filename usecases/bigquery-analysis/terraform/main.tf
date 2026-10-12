provider "google" {
  project = var.project_id

  user_project_override = true
  billing_project       = var.project_id
}

locals {
  job_project = coalesce(var.job_project_id, var.project_id)
  wi_pool     = "${var.project_id}.svc.id.goog"
}

resource "google_project_service" "bigquery" {
  project            = local.job_project
  service            = "bigquery.googleapis.com"
  disable_on_destroy = false
}

# Sandbox用のGSA。KSAからWorkload Identityでなりすます（静的な鍵は作らない）。
resource "google_service_account" "sandbox" {
  project      = var.project_id
  account_id   = var.gsa_id
  display_name = "satin-agent BigQuery analysis sandbox"
}

# BigQueryのジョブの実行だけ。公開データの読み取りにデータセットの権限は要らない。
resource "google_project_iam_member" "job_user" {
  project = local.job_project
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.sandbox.email}"

  depends_on = [google_project_service.bigquery]
}

# GSA経由になるので、アセットの取得（GCS）の権限もGSAに与える。
resource "google_storage_bucket_iam_member" "assets_reader" {
  bucket = var.assets_bucket_name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.sandbox.email}"
}

# 対象のKSAだけがGSAとして振る舞える。
resource "google_service_account_iam_member" "wi_user" {
  service_account_id = google_service_account.sandbox.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${local.wi_pool}[${var.sandbox_namespace}/${var.sandbox_ksa}]"
}
