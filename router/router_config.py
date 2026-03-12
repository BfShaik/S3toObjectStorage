# =============================================================================
# router_config.py
# =============================================================================
#
# WHAT THIS FILE DOES:
#   This is the single source of truth for the upload routing rules
#   used by OCIUploadRouter. It maps every classification key that an
#   application can pass at upload time to the exact OCI bucket name
#   and prefix folder where that object should land.
#
#   This file mirrors the bucket/prefix structure in:
#     terraform/lifecycle_config.auto.tfvars
#   Both files must be kept in sync. When you add a new prefix in the
#   Terraform config, add the matching entry here at the same time.
#
#   Format:
#     "classification-key" : ("bucket-name", "prefix/")
#
#   The classification-key is what application teams pass to the
#   upload() method. It should be human-readable and follow the
#   convention: "{bucket-type}-{data-type}"
#
# OWNED BY:   Cloud ops / Data governance team
# CHANGED BY: Cloud ops admin — add one line per new classification
# SYNC WITH:  terraform/lifecycle_config.auto.tfvars
#
# HOW TO ADD A NEW CLASSIFICATION:
#   1. Add one line here following the existing pattern
#   2. Add the matching prefix line in lifecycle_config.auto.tfvars
#   3. Run: terraform apply
#   4. No application code changes needed — the router picks it up automatically
# =============================================================================

ROUTING_MAP: dict[str, tuple[str, str]] = {

    # -------------------------------------------------------------------------
    # PII DATA  →  bucket-pii-prod
    # Lifecycle: archive after 90 days (30 for financial/health)
    #            delete after 7-15 years depending on sub-type
    # -------------------------------------------------------------------------
    "pii-customers"       : ("bucket-pii-prod", "customers/"),   # 90d archive, 7yr delete
    "pii-employees"       : ("bucket-pii-prod", "employees/"),   # 90d archive, 10yr delete
    "pii-financial"       : ("bucket-pii-prod", "financial/"),   # 30d archive, 10yr delete
    "pii-health"          : ("bucket-pii-prod", "health/"),      # 30d archive, 15yr delete
    # To add: "pii-contractors" : ("bucket-pii-prod", "contractors/"),

    # -------------------------------------------------------------------------
    # COMPLIANCE DATA  →  bucket-compliance-prod
    # Lifecycle: archive after 7-30 days, delete after 7-10 years
    # -------------------------------------------------------------------------
    "compliance-sox"      : ("bucket-compliance-prod", "sox/"),           # 30d archive, 7yr delete
    "compliance-gdpr"     : ("bucket-compliance-prod", "gdpr/"),          # 30d archive, 10yr delete
    "compliance-contracts": ("bucket-compliance-prod", "contracts/"),     # 30d archive, 10yr delete
    "compliance-audit"    : ("bucket-compliance-prod", "audit-trails/"),  # 7d  archive, 10yr delete
    # To add: "compliance-hipaa" : ("bucket-compliance-prod", "hipaa/"),

    # -------------------------------------------------------------------------
    # TEMPORARY PROCESSING  →  bucket-temp-processing
    # Lifecycle: no archive, delete after 1-7 days
    # -------------------------------------------------------------------------
    "temp-raw"            : ("bucket-temp-processing", "raw-ingestion/"), # delete 1d
    "temp-processing"     : ("bucket-temp-processing", "processing/"),    # delete 3d
    "temp-staging"        : ("bucket-temp-processing", "staging/"),       # delete 7d
    # To add: "temp-quarantine" : ("bucket-temp-processing", "quarantine/"),

    # -------------------------------------------------------------------------
    # LOGS  →  bucket-logs-prod
    # Lifecycle: archive after 7-30 days, delete after 30-365 days
    # -------------------------------------------------------------------------
    "log-application"     : ("bucket-logs-prod", "application/"),  # 30d archive, 90d delete
    "log-access"          : ("bucket-logs-prod", "access/"),       # 7d  archive, 30d delete
    "log-security"        : ("bucket-logs-prod", "security/"),     # 7d  archive, 365d delete
    # To add: "log-database" : ("bucket-logs-prod", "database/"),

}

# Valid classification keys as a set — used for fast validation in the router
VALID_CLASSIFICATIONS: set[str] = set(ROUTING_MAP.keys())
