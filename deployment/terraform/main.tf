terraform {
  required_version = ">= 1.5.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.30.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

resource "google_project_service" "required_apis" {
  for_each = toset([
    "aiplatform.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudtrace.googleapis.com",
    "dlp.googleapis.com",
    "logging.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
  ])

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_artifact_registry_repository" "dermguard_repo" {
  location      = var.region
  repository_id = "${var.service_name}-repo"
  description   = "Container repository for the DermGuard ADK agent"
  format        = "DOCKER"

  depends_on = [google_project_service.required_apis]
}

resource "google_service_account" "dermguard_sa" {
  account_id   = "${var.service_name}-sa"
  display_name = "DermGuard ADK Agent Runtime Service Account"
}

resource "google_project_iam_member" "dermguard_roles" {
  for_each = toset([
    "roles/aiplatform.user",
    "roles/cloudtrace.agent",
    "roles/dlp.user",
    "roles/logging.logWriter",
  ])

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.dermguard_sa.email}"
}

resource "google_secret_manager_secret" "google_api_key" {
  secret_id = "${var.service_name}-google-api-key"

  replication {
    auto {}
  }

  depends_on = [google_project_service.required_apis]
}

resource "google_secret_manager_secret_iam_member" "secret_accessor" {
  secret_id = google_secret_manager_secret.google_api_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.dermguard_sa.email}"
}

resource "google_cloud_run_v2_service" "dermguard" {
  name     = var.service_name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.dermguard_sa.email

    scaling {
      min_instance_count = 0
      max_instance_count = 5
    }

    containers {
      image = var.container_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "2"
          memory = "2Gi"
        }
      }

      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }

      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = var.region
      }

      env {
        name  = "GOOGLE_GENAI_USE_VERTEXAI"
        value = var.use_vertex_ai
      }

      env {
        name  = "GOOGLE_API_KEY_SECRET_ID"
        value = google_secret_manager_secret.google_api_key.secret_id
      }

      env {
        name  = "OTEL_TRACES_EXPORTER"
        value = "google_cloud_trace"
      }

      env {
        name  = "ENABLE_CLOUD_DLP"
        value = "true"
      }
    }
  }

  depends_on = [
    google_project_iam_member.dermguard_roles,
    google_secret_manager_secret_iam_member.secret_accessor,
  ]
}
