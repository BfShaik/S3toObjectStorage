# =============================================================================
# main.tf
# =============================================================================
#
# WHAT THIS FILE DOES:
#   This is the core Terraform infrastructure code. It reads the
#   bucket_policies variable (populated from lifecycle_config.auto.tfvars)
#   and uses for_each loops to automatically generate every OCI bucket
#   and every lifecycle policy rule from that config.
#
#   The code itself NEVER changes. All business rule changes go through
#   lifecycle_config.auto.tfvars only. Adding a new prefix or a new bucket
#   requires zero changes here — only a config file update and terraform apply.
#
#   What this file creates:
#     - One OCI Object Storage bucket per key in bucket_policies
#     - One lifecycle policy per bucket containing all prefix rules
#     - Archive rules (Standard → Archive tier) where archive_days is set
#     - Delete rules (permanent deletion) for every prefix
#
# OWNED BY:   Cloud ops / DevOps team
# CHANGED BY: Never after initial deployment
# DEPLOY:     terraform init → terraform plan → terraform apply
# =============================================================================

terraform {
  required_providers {
    oci = {
      source  = "oracle/oci"
      version = ">= 5.0.0"
    }
  }
  required_version = ">= 1.3.0"
}

provider "oci" {
  region = var.region
  # Authentication via OCI config file (~/.oci/config) or instance principal
  # For CI/CD pipelines: set TF_VAR_* environment variables instead
}

# =============================================================================
# STEP 1 — Create all buckets
# Loops over bucket_policies and creates one bucket per key.
# Example: "bucket-pii-prod", "bucket-compliance-prod", etc.
# =============================================================================
resource "oci_objectstorage_bucket" "buckets" {
  for_each = var.bucket_policies

  compartment_id = var.compartment_id
  namespace      = var.namespace
  name           = each.key
  storage_tier   = "Standard"

  # Versioning enabled — protects against accidental overwrites during migration
  versioning = "Enabled"

  # Object events enabled — required if you add OCI Events rules later
  object_events_enabled = true

  metadata = {
    "managed-by"  = "terraform"
    "environment" = "production"
    "solution"    = "oci-lifecycle-migration"
  }
}

# =============================================================================
# STEP 2 — Flatten nested config into a single list of rules
#
# Converts the nested map structure:
#   bucket-pii-prod → customers/ → { archive_days=90, delete_days=2555 }
#
# Into a flat list of individual rule objects:
#   { key="bucket-pii-prod__customers/__archive", bucket="bucket-pii-prod",
#     prefix="customers/", action="ARCHIVE", days=90 }
#   { key="bucket-pii-prod__customers/__delete",  bucket="bucket-pii-prod",
#     prefix="customers/", action="DELETE",  days=2555 }
#
# The double-underscore (__) separator is used because bucket names and
# prefixes can contain hyphens and slashes — __ is safe as a unique delimiter.
# =============================================================================
locals {

  # Flatten all archive rules (only where archive_days is not null)
  archive_rules = flatten([
    for bucket_name, bucket_config in var.bucket_policies : [
      for prefix, retention in bucket_config.prefixes : {
        key    = "${bucket_name}__${prefix}__archive"
        bucket = bucket_name
        prefix = prefix
        action = "ARCHIVE"
        days   = retention.archive_days
      }
      if retention.archive_days != null   # skip if no archive rule configured
    ]
  ])

  # Flatten all delete rules (every prefix always gets a delete rule)
  delete_rules = flatten([
    for bucket_name, bucket_config in var.bucket_policies : [
      for prefix, retention in bucket_config.prefixes : {
        key    = "${bucket_name}__${prefix}__delete"
        bucket = bucket_name
        prefix = prefix
        action = "DELETE"
        days   = retention.delete_days
      }
    ]
  ])

  # Merge archive and delete rules into a single map keyed by rule key
  # This is the structure Terraform for_each and dynamic blocks consume
  all_rules_map = {
    for rule in concat(local.archive_rules, local.delete_rules) :
    rule.key => rule
  }
}

# =============================================================================
# STEP 3 — Apply lifecycle policies
#
# Creates one lifecycle policy resource per bucket.
# Each policy contains all prefix rules for that bucket, generated
# automatically by the dynamic "rules" block iterating over all_rules_map.
#
# The dynamic block filters all_rules_map to only include rules
# belonging to the current bucket (rule.bucket == each.key).
# =============================================================================
resource "oci_objectstorage_object_lifecycle_policy" "policies" {
  for_each = var.bucket_policies

  namespace = var.namespace
  bucket    = each.key

  # Ensure bucket exists before applying policy
  depends_on = [oci_objectstorage_bucket.buckets]

  # dynamic block — generates one rule{} block per prefix automatically
  # No code change needed when prefixes are added or removed in config
  dynamic "rules" {
    for_each = {
      for key, rule in local.all_rules_map :
      key => rule
      if rule.bucket == each.key    # only rules belonging to this bucket
    }

    content {
      name        = rules.value.key
      action      = rules.value.action    # "ARCHIVE" or "DELETE"
      time_amount = rules.value.days      # number of days from Last-Modified
      time_unit   = "DAYS"
      is_enabled  = true

      object_name_filter {
        inclusion_prefixes = [rules.value.prefix]
      }
    }
  }
}

# =============================================================================
# OUTPUTS — useful after terraform apply
# =============================================================================
output "bucket_names" {
  description = "Names of all created OCI Object Storage buckets"
  value       = [for b in oci_objectstorage_bucket.buckets : b.name]
}

output "lifecycle_rule_count" {
  description = "Total number of lifecycle rules applied across all buckets"
  value       = length(local.all_rules_map)
}

output "rules_summary" {
  description = "Summary of all generated lifecycle rules"
  value = {
    for key, rule in local.all_rules_map :
    key => "${rule.action} after ${rule.days} days"
  }
}
