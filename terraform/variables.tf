# =============================================================================
# variables.tf
# =============================================================================
#
# WHAT THIS FILE DOES:
#   Declares all input variables required by the Terraform module.
#   Values for compartment_id, namespace, and region are supplied
#   via terraform.tfvars or environment variables at deploy time.
#   The bucket_policies variable receives its value automatically
#   from lifecycle_config.auto.tfvars (Terraform loads *.auto.tfvars
#   files automatically — no explicit reference needed).
#
# OWNED BY:   Cloud ops / DevOps team
# CHANGED BY: Never — variable declarations are stable
# =============================================================================

variable "compartment_id" {
  description = "OCI compartment OCID where all buckets will be created"
  type        = string
}

variable "namespace" {
  description = "OCI Object Storage namespace for this tenancy"
  type        = string
}

variable "region" {
  description = "OCI region identifier (e.g. us-ashburn-1)"
  type        = string
}

# -----------------------------------------------------------------------------
# bucket_policies — the full lifecycle configuration
# Populated automatically from lifecycle_config.auto.tfvars
# Structure: bucket_name → prefixes → { archive_days, delete_days }
# archive_days = null means no archive rule — object goes straight to delete
# -----------------------------------------------------------------------------
variable "bucket_policies" {
  description = "Map of bucket names to their prefix-level lifecycle rules"
  type = map(object({
    prefixes = map(object({
      archive_days = optional(number)   # null = no archive rule
      delete_days  = number             # always required
    }))
  }))
}
