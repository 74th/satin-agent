provider "google" {
  project = var.project_id
  region  = var.region

  # DLP などは ADC（ユーザ認証）では quota project が必要。
  user_project_override = true
  billing_project       = var.project_id
}

data "google_project" "this" {
  project_id = var.project_id
}

locals {
  services = [
    "aiplatform.googleapis.com",       # Gemini（入力ガード）
    "modelarmor.googleapis.com",       # 出力の検査
    "storage.googleapis.com",          # アセット
    "dlp.googleapis.com",              # Model Armor の機密データ保護
    "artifactregistry.googleapis.com", # イメージ
  ]

  # GKE の Workload Identity Federation: GSA を作らず、KSA を直接 IAM の principal にする。
  wi_pool           = "${var.project_id}.svc.id.goog"
  bot_principal     = "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${local.wi_pool}/subject/ns/${var.system_namespace}/sa/${var.bot_ksa}"
  sandbox_principal = "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${local.wi_pool}/subject/ns/${var.sandbox_namespace}/sa/${var.sandbox_ksa}"
}

resource "google_project_service" "apis" {
  for_each           = toset(local.services)
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# --- アセットのバケット ---
resource "google_storage_bucket" "assets" {
  project                     = var.project_id
  name                        = var.assets_bucket_name
  location                    = upper(var.region)
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  depends_on = [google_project_service.apis]
}

# Sandbox は読むだけ。書き込み（satin publish）は開発者の資格情報で行う。
resource "google_storage_bucket_iam_member" "sandbox_reader" {
  bucket = google_storage_bucket.assets.name
  role   = "roles/storage.objectViewer"
  member = local.sandbox_principal
}

# --- イメージのリポジトリ ---
resource "google_artifact_registry_repository" "images" {
  project       = var.project_id
  location      = var.region
  repository_id = var.artifact_registry_repo
  format        = "DOCKER"
  description   = "satin-agent runtime / app images"

  depends_on = [google_project_service.apis]
}

# --- Bot の権限（入力ガードの Gemini と出力検査の Model Armor） ---
resource "google_project_iam_member" "bot" {
  for_each = toset(["roles/aiplatform.user", "roles/modelarmor.user"])
  project  = var.project_id
  role     = each.value
  member   = local.bot_principal
}

# --- 個人情報のマスク（DLP テンプレート + Model Armor テンプレート） ---
locals {
  pii_info_types = [
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "CREDIT_CARD_NUMBER",
    "JAPAN_INDIVIDUAL_NUMBER",
    "JAPAN_PASSPORT",
    "JAPAN_BANK_ACCOUNT",
  ]
}

resource "google_data_loss_prevention_inspect_template" "pii" {
  parent       = "projects/${var.project_id}/locations/${var.region}"
  display_name = "satin-agent-pii-inspect"

  inspect_config {
    dynamic "info_types" {
      for_each = local.pii_info_types
      content {
        name = info_types.value
      }
    }
    min_likelihood = "POSSIBLE"
  }

  depends_on = [google_project_service.apis]
}

# 検出した個人情報を、情報タイプ名（例: [EMAIL_ADDRESS]）に置き換える。
resource "google_data_loss_prevention_deidentify_template" "pii" {
  parent       = "projects/${var.project_id}/locations/${var.region}"
  display_name = "satin-agent-pii-deidentify"

  deidentify_config {
    info_type_transformations {
      transformations {
        primitive_transformation {
          replace_with_info_type_config = true
        }
      }
    }
  }

  depends_on = [google_project_service.apis]
}

resource "google_model_armor_template" "simple" {
  project     = var.project_id
  location    = var.region
  template_id = var.model_armor_template_id

  filter_config {
    rai_settings {
      rai_filters {
        filter_type      = "HATE_SPEECH"
        confidence_level = "MEDIUM_AND_ABOVE"
      }
      rai_filters {
        filter_type      = "HARASSMENT"
        confidence_level = "MEDIUM_AND_ABOVE"
      }
      rai_filters {
        filter_type      = "DANGEROUS"
        confidence_level = "MEDIUM_AND_ABOVE"
      }
      rai_filters {
        filter_type      = "SEXUALLY_EXPLICIT"
        confidence_level = "MEDIUM_AND_ABOVE"
      }
    }
    pi_and_jailbreak_filter_settings {
      filter_enforcement = "ENABLED"
      confidence_level   = "MEDIUM_AND_ABOVE"
    }
    malicious_uri_filter_settings {
      filter_enforcement = "ENABLED"
    }
    sdp_settings {
      # マスクして表示するため、deidentify テンプレートを使う advanced_config にする。
      advanced_config {
        inspect_template    = google_data_loss_prevention_inspect_template.pii.id
        deidentify_template = google_data_loss_prevention_deidentify_template.pii.id
      }
    }
  }

  template_metadata {
    log_sanitize_operations = false
  }

  depends_on = [google_project_service.apis]
}
