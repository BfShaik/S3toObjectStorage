# =============================================================================
# variables.tf
# =============================================================================
#
# WHAT THIS FILE DOES:
#   Declares the three OCI environment variables needed by main.tf.
#   There is no bucket_policies variable here — that data now comes
#   directly from classifications.json via jsondecode(file()) in main.tf.
#   Supply values in terraform.tfvars (never commit that file).
#
# OWNED BY:   Cloud ops / DevOps team
# CHANGED BY: Never
# =============================================================================

variable "compartment_id" {
  description = "OCI compartment OCID where all buckets will be created"
  type        = string
}

variable "namespace" {
  description = "OCI Object Storage namespace (run: oci os ns get)"
  type        = string
}

variable "region" {
  description = "OCI region identifier e.g. us-ashburn-1"
  type        = string
}
