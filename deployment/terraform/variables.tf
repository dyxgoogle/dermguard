variable "project_id" {
  description = "Target Google Cloud Project ID for deploying DermGuard."
  type        = string
}

variable "region" {
  description = "Google Cloud region for Cloud Run and Artifact Registry."
  type        = string
  default     = "us-central1"
}

variable "service_name" {
  description = "Name of the Cloud Run service."
  type        = string
  default     = "dermguard-agent"
}

variable "container_image" {
  description = "Full Artifact Registry container image URI to deploy."
  type        = string
}

variable "use_vertex_ai" {
  description = "Whether the deployed agent calls Gemini via Vertex AI IAM ('TRUE') or Secret Manager API key ('FALSE')."
  type        = string
  default     = "TRUE"
}
