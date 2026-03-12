# =============================================================================
# lifecycle_config.auto.tfvars
# =============================================================================
#
# WHAT THIS FILE DOES:
#   This is the single source of truth for every bucket, prefix, and
#   retention rule in the entire OCI Object Storage lifecycle system.
#   It is the ONLY file that ever needs to change when:
#     - Adding a new data classification (e.g. "contractors/")
#     - Adjusting a retention period for compliance reasons
#     - Adding a new bucket for a new business unit
#
#   No Terraform code changes are needed. No developer is required.
#   A cloud ops admin edits this file and runs: terraform apply
#
# OWNED BY:   Cloud ops / Data governance team
# CHANGED BY: Cloud ops admin — no code review required for config changes
# FORMAT:     archive_days = null means "never archive — go straight to delete"
#
# HOW TO ADD A NEW CLASSIFICATION:
#   1. Add one line under the correct bucket's prefixes block
#   2. Add the matching entry in router/router_config.py
#   3. Run: terraform apply
#   That is the complete change required.
#
# RETENTION REFERENCE:
#   PII data (GDPR/CCPA)    : archive 90d,  delete 7yr  (2555d)
#   Employee records (HR)   : archive 90d,  delete 10yr (3650d)
#   Financial PII           : archive 30d,  delete 10yr (3650d)
#   Health data (HIPAA)     : archive 30d,  delete 15yr (5475d)
#   SOX compliance          : archive 30d,  delete 7yr  (2555d)
#   GDPR compliance records : archive 30d,  delete 10yr (3650d)
#   Legal contracts         : archive 30d,  delete 10yr (3650d)
#   Audit trails            : archive 7d,   delete 10yr (3650d)
#   Temp / raw ingestion    : no archive,   delete 1d
#   Temp / processing       : no archive,   delete 3d
#   Temp / staging          : no archive,   delete 7d
#   Application logs        : archive 30d,  delete 90d
#   Access logs             : archive 7d,   delete 30d
#   Security logs           : archive 7d,   delete 365d
# =============================================================================

bucket_policies = {

  # ---------------------------------------------------------------------------
  # PII DATA BUCKET
  # Stores all personal identifiable information.
  # Archive quickly to reduce storage cost. Retain for regulatory compliance.
  # ---------------------------------------------------------------------------
  "bucket-pii-prod" = {
    prefixes = {
      "customers/"  = { archive_days = 90, delete_days = 2555 }  # 7 years
      "employees/"  = { archive_days = 90, delete_days = 3650 }  # 10 years
      "financial/"  = { archive_days = 30, delete_days = 3650 }  # 10 years
      "health/"     = { archive_days = 30, delete_days = 5475 }  # 15 years
      # -----------------------------------------------------------------------
      # TO ADD A NEW PII CLASSIFICATION:
      # "contractors/" = { archive_days = 90, delete_days = 2555 }
      # -----------------------------------------------------------------------
    }
  }

  # ---------------------------------------------------------------------------
  # COMPLIANCE DATA BUCKET
  # Stores regulatory, legal, and audit records.
  # Move to archive tier quickly — accessed rarely but must be retained.
  # ---------------------------------------------------------------------------
  "bucket-compliance-prod" = {
    prefixes = {
      "sox/"          = { archive_days = 30, delete_days = 2555 }  # 7 years
      "gdpr/"         = { archive_days = 30, delete_days = 3650 }  # 10 years
      "contracts/"    = { archive_days = 30, delete_days = 3650 }  # 10 years
      "audit-trails/" = { archive_days = 7,  delete_days = 3650 }  # 10 years
      # -----------------------------------------------------------------------
      # TO ADD A NEW COMPLIANCE TYPE:
      # "hipaa/" = { archive_days = 30, delete_days = 3650 }
      # -----------------------------------------------------------------------
    }
  }

  # ---------------------------------------------------------------------------
  # TEMPORARY PROCESSING BUCKET
  # Stores short-lived objects from data pipelines and processing jobs.
  # No archive tier — objects are deleted directly after their TTL.
  # archive_days = null means skip archive, go straight to delete.
  # ---------------------------------------------------------------------------
  "bucket-temp-processing" = {
    prefixes = {
      "raw-ingestion/" = { archive_days = null, delete_days = 1 }  # 1 day
      "processing/"    = { archive_days = null, delete_days = 3 }  # 3 days
      "staging/"       = { archive_days = null, delete_days = 7 }  # 7 days
      # -----------------------------------------------------------------------
      # TO ADD A NEW TEMP TYPE:
      # "quarantine/" = { archive_days = null, delete_days = 2 }
      # -----------------------------------------------------------------------
    }
  }

  # ---------------------------------------------------------------------------
  # LOGS BUCKET
  # Stores application, access, and security logs.
  # Archive quickly. Delete when beyond operational and compliance window.
  # ---------------------------------------------------------------------------
  "bucket-logs-prod" = {
    prefixes = {
      "application/" = { archive_days = 30, delete_days = 90  }  # 90 days
      "access/"      = { archive_days = 7,  delete_days = 30  }  # 30 days
      "security/"    = { archive_days = 7,  delete_days = 365 }  # 1 year
      # -----------------------------------------------------------------------
      # TO ADD A NEW LOG TYPE:
      # "database/" = { archive_days = 7, delete_days = 90 }
      # -----------------------------------------------------------------------
    }
  }

}
