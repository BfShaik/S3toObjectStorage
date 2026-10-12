terraform {
  required_providers {
    oci = {
      source  = "oracle/oci"
      version = ">= 5.0.0"
    }
  }
  required_version = ">= 1.3.0"
}

provider "oci" { region = var.region }

locals {
  raw             = jsondecode(file("${path.module}/../config/classifications.json"))
  classifications = local.raw.classifications
  bucket_names    = toset([for _, value in local.classifications : value.bucket])

  archive_rules = {
    for key, value in local.classifications : "${key}-archive" => {
      bucket = value.bucket, prefix = value.prefix, action = "ARCHIVE", days = value.archive_days
    } if value.archive_days != null
  }

  # DELETE rules are deliberately opt-in: they require a configured duration
  # and an explicit enable_delete=true input.
  delete_rules = var.enable_delete ? {
    for key, value in local.classifications : "${key}-delete" => {
      bucket = value.bucket, prefix = value.prefix, action = "DELETE", days = value.delete_days
    } if value.delete_days != null
  } : {}

  # Generated and reviewed during historical migration. Terraform remains the
  # only writer of lifecycle policies, avoiding SDK/Terraform policy conflicts.
  cohort_rules = var.enable_delete ? {
    for key, value in var.migration_cohorts : "cohort-${key}-delete" => {
      bucket = value.bucket, prefix = value.prefix, action = "DELETE", days = value.remaining_delete_days
    }
  } : {}

  all_rules = merge(local.archive_rules, local.delete_rules, local.cohort_rules)
}

resource "oci_objectstorage_bucket" "buckets" {
  for_each       = local.bucket_names
  compartment_id = var.compartment_id
  namespace      = var.namespace
  name           = each.key
  storage_tier   = "Standard"
  versioning     = "Enabled"
  metadata = {
    "managed-by" = "terraform"
    "config-src" = "classifications.json"
  }
}

resource "oci_objectstorage_object_lifecycle_policy" "policies" {
  for_each   = local.bucket_names
  namespace  = var.namespace
  bucket     = each.key
  depends_on = [oci_objectstorage_bucket.buckets]

  dynamic "rules" {
    for_each = { for key, rule in local.all_rules : key => rule if rule.bucket == each.key }
    content {
      name        = rules.key
      action      = rules.value.action
      time_amount = rules.value.days
      time_unit   = "DAYS"
      is_enabled  = true
      object_name_filter { inclusion_prefixes = [rules.value.prefix] }
    }
  }
}

output "buckets_created" { value = [for bucket in oci_objectstorage_bucket.buckets : bucket.name] }
output "total_lifecycle_rules" { value = length(local.all_rules) }
