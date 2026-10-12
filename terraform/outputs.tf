output "assets_gcs_url" {
  description = "設定ファイルの assets.gcs_url に設定する値"
  value       = "gs://${google_storage_bucket.assets.name}/satin/default/"
}

output "model_armor_template" {
  description = "設定ファイルの guards.output_inspection.template に設定する値"
  value       = google_model_armor_template.simple.id
}

output "image_repository" {
  description = "イメージのpush先"
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

output "bot_principal" {
  value = local.bot_principal
}

output "sandbox_principal" {
  value = local.sandbox_principal
}
