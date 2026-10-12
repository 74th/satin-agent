output "gsa_email" {
  description = "KSAの iam.gke.io/gcp-service-account 注釈（deploy時のGSA_EMAIL）"
  value       = google_service_account.sandbox.email
}

output "wi_member" {
  description = "なりすましを許可したKSA（namespace/name）"
  value       = google_service_account_iam_member.wi_user.member
}

output "job_project_id" {
  description = "sandbox.env.BIGQUERY_JOB_PROJECT に設定する値"
  value       = local.job_project
}
