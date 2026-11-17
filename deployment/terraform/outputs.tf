output "cloud_run_service_url" {
  description = "HTTPS endpoint URL of the deployed DermGuard Cloud Run service."
  value       = google_cloud_run_v2_service.dermguard.uri
}

output "service_account_email" {
  description = "Least-privilege service account identity attached to DermGuard."
  value       = google_service_account.dermguard_sa.email
}

output "artifact_registry_repository" {
  description = "Artifact Registry Docker repository ID."
  value       = google_artifact_registry_repository.dermguard_repo.name
}
