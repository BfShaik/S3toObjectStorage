# =============================================================================
# main.tf
# =============================================================================
#
# WHAT THIS FILE DOES:
#   Reads classifications.json (the single source of truth) and
#   automatically generates every OCI bucket and every lifecycle
#   policy rule from it using for_each loops.
#
#   This file NEVER changes. All business rule changes go through
#   config/classifications.json only. Adding a new prefix or bucket
#   requires zero changes here — only a JSON entry + terraform apply.
#
#   What gets created:
#     - One OCI bucket per unique "bucket" value in classifications.json
#     - One lifecycle policy per bucket
#     - ARCHIVE rules for every classification where archive_days != null
#     - DELETE rules for every classification (always)
#
# OWNED BY:   Cloud ops / DevOps team
# CHANGED BY: Never — logic is fixed
# CONFIG:     ../config/classifications.json
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
}

# =============================================================================
# READ CONFIG — parse classifications.json
# This is the only data source. No hardcoded values anywhere in this file.
# =============================================================================
locals {

  # Read and parse the single shared config file
  # Same file the Python upload router reads at runtime
  raw             = jsondecode(file("${path.module}/../config/classifications.json"))
  classifications = local.raw["classifications"]

  # ── Derive unique bucket names ──────────────────────────────────────────────
  # Extract the set of distinct bucket names from the config.
  # Terraform creates exactly one bucket resource per unique name.
  # Adding a new classification that routes to an existing bucket
  # does not create a duplicate — toset() deduplicates automatically.
  bucket_names = toset([
    for key, val in local.classifications : val.bucket
  ])

  # ── Build ARCHIVE rules ─────────────────────────────────────────────────────
  # One rule per classification where archive_days is not null.
  # Key format: "{bucket}__{prefix}__archive"
  # Double underscore used as delimiter — safe for bucket names and prefixes.
  archive_rules = {
    for key, val in local.classifications :
    "${val.bucket}__${val.prefix}__archive" => {
      bucket = val.bucket
      prefix = val.prefix
      action = "ARCHIVE"
      days   = val.archive_days
    }
    if val.archive_days != null
  }

  # ── Build DELETE rules ──────────────────────────────────────────────────────
  # Every classification always gets a DELETE rule — no exceptions.
  delete_rules = {
    for key, val in local.classifications :
    "${val.bucket}__${val.prefix}__delete" => {
      bucket = val.bucket
      prefix = val.prefix
      action = "DELETE"
      days   = val.delete_days
    }
  }

  # ── Merge into one flat map ─────────────────────────────────────────────────
  # Single map consumed by the dynamic rules block below.
  all_rules = merge(local.archive_rules, local.delete_rules)
}

# =============================================================================
# CREATE BUCKETS
# One bucket per unique bucket name derived from classifications.json.
# No bucket names are hardcoded here.
# =============================================================================
resource "oci_objectstorage_bucket" "buckets" {
  for_each = local.bucket_names

  compartment_id        = var.compartment_id
  namespace             = var.namespace
  name                  = each.key
  storage_tier          = "Standard"
  versioning            = "Enabled"
  object_events_enabled = true

  metadata = {
    "managed-by" = "terraform"
    "config-src" = "classifications.json"
  }
}

# =============================================================================
# APPLY LIFECYCLE POLICIES
# One policy per bucket. Rules generated dynamically from all_rules.
# The dynamic block filters all_rules to only the rules for each bucket.
# No rule definitions are hardcoded here.
# =============================================================================
resource "oci_objectstorage_object_lifecycle_policy" "policies" {
  for_each = local.bucket_names

  namespace  = var.namespace
  bucket     = each.key
  depends_on = [oci_objectstorage_bucket.buckets]

  dynamic "rules" {
    for_each = {
      for rule_key, rule in local.all_rules :
      rule_key => rule
      if rule.bucket == each.key
    }

    content {
      name        = rules.key
      action      = rules.value.action
      time_amount = rules.value.days
      time_unit   = "DAYS"
      is_enabled  = true

      object_name_filter {
        inclusion_prefixes = [rules.value.prefix]
      }
    }
  }
}

# =============================================================================
# OUTPUTS
# =============================================================================
output "buckets_created" {
  description = "All OCI buckets created by this configuration"
  value       = [for b in oci_objectstorage_bucket.buckets : b.name]
}

output "total_lifecycle_rules" {
  description = "Total lifecycle rules applied across all buckets"
  value       = length(local.all_rules)
}

output "rules_summary" {
  description = "Full list of generated lifecycle rules"
  value = {
    for key, rule in local.all_rules :
    key => "${rule.action} after ${rule.days} days on prefix '${rule.prefix}'"
  }
}
