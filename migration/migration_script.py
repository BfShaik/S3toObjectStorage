# =============================================================================
# migration_script.py
# =============================================================================
#
# WHAT THIS FILE DOES:
#   This script runs exactly once during the migration cutover window
#   and is discarded afterwards. It copies all existing objects from
#   AWS S3 to OCI Object Storage in three sequential phases:
#
#   PHASE 1 — Extract S3 inventory
#     Scans every object in the source S3 bucket and writes its key,
#     original LastModified date, S3 tags, size, and storage class
#     to a local CSV inventory file. This is the source of truth for
#     the entire migration. Review the CSV before running Phase 2.
#
#   PHASE 2 — Copy objects S3 → OCI
#     Reads the inventory CSV. For each object: downloads from S3,
#     translates all S3 tags to OCI opc-meta-* metadata, preserves
#     the original S3 creation date in opc-meta-original-creation-date
#     (CRITICAL — without this OCI would give every object a fresh
#     retention window from today), and uploads to OCI under a
#     year-cohort prefix so lifecycle rules apply correct remaining
#     retention. Script is resumable — re-running skips existing objects.
#
#   PHASE 3 — Apply reduced lifecycle rules for migration cohorts
#     A 4-year-old object must NOT get a fresh 7-year delete window.
#     This phase calculates the remaining retention days for each
#     year-cohort prefix and applies a reduced DELETE lifecycle rule
#     via the OCI SDK (GET existing policy → merge → PUT). Objects
#     already past their deadline are flagged OVERDUE and must be
#     deleted immediately.
#
# OWNED BY:   Migration team
# RUNS:       Once during cutover window — then discarded
# RESUMABLE:  Yes — set SKIP_EXISTING = True (default) to safely re-run
# DEPENDS ON: boto3 (pip install boto3)
#             oci   (pip install oci)
#             router/router_config.py (auto-resolved via sys.path)
# =============================================================================

import csv
import json
import logging
import os
import sys
from datetime import datetime, timezone

# router_config.py lives in ../router/ — add it to the path so this script
# can be run from the project root: python migration/migration_script.py
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "router"))

import boto3
import oci
import oci.object_storage
import oci.exceptions

from router_config import ROUTING_MAP

# =============================================================================
# CONFIGURATION — edit these values before running
# =============================================================================

S3_SOURCE_BUCKET = "your-source-s3-bucket"     # AWS S3 bucket to migrate from
S3_REGION        = "us-east-1"                  # AWS region of the source bucket
OCI_CONFIG_PATH  = "~/.oci/config"              # OCI SDK config file path
INVENTORY_FILE   = "s3_inventory.csv"           # CSV written by Phase 1
LOG_FILE         = "migration.log"              # Migration run log

SKIP_EXISTING    = True   # Skip objects already in OCI — safe for re-runs
DRY_RUN          = True   # ALWAYS start with True — set False only after reviewing migration.log

# Full retention windows per classification (days)
# These MUST match lifecycle_config.auto.tfvars exactly
RETENTION_RULES = {
    "pii-customers"       : {"archive_days": 90,   "delete_days": 2555},
    "pii-employees"       : {"archive_days": 90,   "delete_days": 3650},
    "pii-financial"       : {"archive_days": 30,   "delete_days": 3650},
    "pii-health"          : {"archive_days": 30,   "delete_days": 5475},
    "compliance-sox"      : {"archive_days": 30,   "delete_days": 2555},
    "compliance-gdpr"     : {"archive_days": 30,   "delete_days": 3650},
    "compliance-contracts": {"archive_days": 30,   "delete_days": 3650},
    "compliance-audit"    : {"archive_days": 7,    "delete_days": 3650},
    "temp-raw"            : {"archive_days": None, "delete_days": 1   },
    "temp-processing"     : {"archive_days": None, "delete_days": 3   },
    "temp-staging"        : {"archive_days": None, "delete_days": 7   },
    "log-application"     : {"archive_days": 30,   "delete_days": 90  },
    "log-access"          : {"archive_days": 7,    "delete_days": 30  },
    "log-security"        : {"archive_days": 7,    "delete_days": 365 },
}

# =============================================================================
# SETUP — logging
# =============================================================================

logging.basicConfig(
    level    = logging.INFO,
    format   = "%(asctime)s | %(levelname)s | %(message)s",
    handlers = [
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


# =============================================================================
# HELPERS
# =============================================================================

def _get_oci_client():
    config    = oci.config.from_file(OCI_CONFIG_PATH)
    client    = oci.object_storage.ObjectStorageClient(config)
    namespace = client.get_namespace().data
    return client, namespace


def _get_s3_client():
    return boto3.client("s3", region_name=S3_REGION)


def _object_exists_in_oci(client, namespace, bucket, key):
    """Return True if the object already exists in OCI (for resumable runs)."""
    try:
        client.head_object(namespace, bucket, key)
        return True
    except oci.exceptions.ServiceError as e:
        if e.status == 404:
            return False
        raise  # propagate auth errors, 500s, etc. — don't silently swallow them


# =============================================================================
# PHASE 1 — Extract S3 inventory
# =============================================================================

def phase1_extract_inventory():
    """
    Scan the S3 source bucket and write every object's metadata to
    INVENTORY_FILE. This CSV is the source of truth for Phase 2.

    Columns written:
        key            : S3 object key
        original_date  : S3 LastModified — the true creation date (PRESERVE)
        size_bytes     : Object size in bytes
        storage_class  : S3 storage class (STANDARD, GLACIER, etc.)
        tags           : JSON string of all S3 tags → becomes opc-meta-* on OCI
    """
    logger.info("=" * 60)
    logger.info("PHASE 1: Extracting S3 inventory from bucket: %s", S3_SOURCE_BUCKET)
    logger.info("=" * 60)

    s3        = _get_s3_client()
    paginator = s3.get_paginator("list_objects_v2")
    inventory = []
    count     = 0

    for page in paginator.paginate(Bucket=S3_SOURCE_BUCKET):
        for obj in page.get("Contents", []):
            key           = obj["Key"]
            original_date = obj["LastModified"].strftime("%Y-%m-%dT%H:%M:%SZ")
            size_bytes    = obj["Size"]
            storage_class = obj.get("StorageClass", "STANDARD")

            # Read all S3 tags — each tag becomes one opc-meta-* key on OCI
            try:
                tag_resp = s3.get_object_tagging(Bucket=S3_SOURCE_BUCKET, Key=key)
                tags     = {t["Key"]: t["Value"] for t in tag_resp.get("TagSet", [])}
            except Exception as e:
                logger.warning("Could not read tags for %s: %s", key, e)
                tags = {}

            inventory.append({
                "key"          : key,
                "original_date": original_date,  # CRITICAL — preserve this
                "size_bytes"   : size_bytes,
                "storage_class": storage_class,
                "tags"         : json.dumps(tags),
            })

            count += 1
            if count % 1000 == 0:
                logger.info("  Progress: %d objects inventoried...", count)

    # Write to CSV
    if not inventory:
        logger.warning("No objects found in bucket %s", S3_SOURCE_BUCKET)
        return 0

    with open(INVENTORY_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=inventory[0].keys())
        writer.writeheader()
        writer.writerows(inventory)

    logger.info("Phase 1 complete: %d objects written to %s", count, INVENTORY_FILE)
    logger.info("ACTION REQUIRED: Review %s before running Phase 2", INVENTORY_FILE)
    return count


# =============================================================================
# PHASE 2 — Copy objects S3 → OCI
# =============================================================================

def phase2_copy_objects():
    """
    Read the inventory CSV and copy each object from S3 to OCI.

    Year-cohort prefix strategy:
        Objects land at: migration/{base_prefix}{year}/{original_key}
        e.g.           : migration/customers/2022/reports/customer-001.json

        This allows Phase 3 to apply REDUCED delete rules per cohort.
        A 4-year-old PII file needs only 3 more years (1095 days),
        not a fresh 7-year window from today.

    Metadata translation:
        Every S3 tag key-value pair becomes an opc-meta-* key on OCI.
        opc-meta-original-creation-date is always set from the S3
        LastModified date — NOT from today's date.
    """
    logger.info("=" * 60)
    logger.info("PHASE 2: Copying objects S3 → OCI")
    logger.info("=" * 60)

    client, namespace = _get_oci_client()
    s3                = _get_s3_client()
    copied            = 0
    skipped           = 0
    errors            = 0
    today             = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with open(INVENTORY_FILE, newline="") as f:
        rows = list(csv.DictReader(f))

    total = len(rows)
    logger.info("Processing %d objects from inventory...", total)

    for i, row in enumerate(rows, 1):
        key            = row["key"]
        original_date  = row["original_date"]
        tags           = json.loads(row["tags"])
        classification = tags.get("classification", "")

        # Validate classification — skip unknown ones with a warning
        if classification not in ROUTING_MAP:
            logger.warning(
                "[%d/%d] Unknown classification '%s' for object %s — skipping",
                i, total, classification, key
            )
            errors += 1
            continue

        bucket, base_prefix = ROUTING_MAP[classification]

        # Build year-cohort migration key
        # Encodes the original creation year into the path
        # so lifecycle rules can apply the correct remaining retention
        cohort_year   = original_date[:4]                              # e.g. "2022"
        migration_key = f"migration/{base_prefix}{cohort_year}/{key}"  # e.g. migration/customers/2022/file.csv

        # Skip if already exists in OCI (resumable run support)
        if SKIP_EXISTING and _object_exists_in_oci(client, namespace, bucket, migration_key):
            skipped += 1
            if skipped % 500 == 0:
                logger.info("  Skipped %d already-migrated objects...", skipped)
            continue

        if DRY_RUN:
            logger.info(
                "[DRY RUN] [%d/%d] Would copy: %s → %s/%s",
                i, total, key, bucket, migration_key
            )
            copied += 1
            continue

        # Download from S3
        try:
            s3_obj = s3.get_object(Bucket=S3_SOURCE_BUCKET, Key=key)
            body   = s3_obj["Body"].read()
        except Exception as e:
            logger.error("[%d/%d] S3 download failed: %s | error: %s", i, total, key, e)
            errors += 1
            continue

        # Build OCI metadata
        # Translate all S3 tags → opc-meta-* keys (1:1 mapping)
        metadata = {k: v for k, v in tags.items()}
        metadata["original-creation-date"] = original_date  # CRITICAL — do not use today
        metadata["migrated-from"]          = "s3"
        metadata["migration-date"]         = today
        metadata["source-bucket"]          = S3_SOURCE_BUCKET
        metadata["source-storage-class"]   = row["storage_class"]

        # Upload to OCI with all metadata in one atomic call
        try:
            client.put_object(
                namespace_name  = namespace,
                bucket_name     = bucket,
                object_name     = migration_key,
                put_object_body = body,
                opc_meta        = metadata,
            )
            copied += 1
            if copied % 500 == 0:
                logger.info("  Progress: %d/%d copied...", copied, total)

        except oci.exceptions.ServiceError as e:
            logger.error(
                "[%d/%d] OCI upload failed: %s | status: %s | %s",
                i, total, key, e.status, e.message
            )
            errors += 1

    logger.info(
        "Phase 2 complete | copied=%d | skipped=%d (already existed) | errors=%d",
        copied, skipped, errors
    )
    return copied, errors


# =============================================================================
# PHASE 3 — Apply reduced lifecycle rules for migration cohorts
# =============================================================================

def phase3_apply_cohort_lifecycle_rules(oci_namespace: str):  # noqa: C901
    """
    Calculate remaining retention days for each migration year-cohort
    and apply a reduced DELETE lifecycle rule to that cohort prefix.

    Why this is necessary:
        OCI lifecycle measures object age from Last-Modified date.
        All migrated objects have Last-Modified = today (migration date).
        Without cohort rules, a 4-year-old PII file would get
        a fresh 7-year (2555-day) delete window — a compliance violation.

    What this phase does:
        For each classification + year-cohort combination:
            days_elapsed   = today - original_year (approximate)
            remaining_days = full_delete_days - days_elapsed

            If remaining_days <= 0:  FLAG as OVERDUE → delete immediately
            If remaining_days > 0:   Apply DELETE rule with remaining_days
                                     to prefix: migration/{base}/{year}/

    Example:
        Classification : pii-customers  (full retention = 2555 days / 7 years)
        Cohort year    : 2022
        Days elapsed   : ~1520 days (approx 4.2 years)
        Remaining      : 2555 - 1520 = 1035 days
        Rule applied   : DELETE migration/customers/2022/ after 1035 days
    """
    logger.info("=" * 60)
    logger.info("PHASE 3: Applying reduced lifecycle rules for migration cohorts")
    logger.info("=" * 60)

    client, _   = _get_oci_client()
    today       = datetime.now(timezone.utc).date()
    overdue     = []
    rules_added = 0

    # Determine all unique classification + year combinations from the inventory
    cohorts: dict[str, set] = {}  # classification → set of years

    with open(INVENTORY_FILE, newline="") as f:
        for row in csv.DictReader(f):
            tags           = json.loads(row["tags"])
            classification = tags.get("classification", "")
            year           = row["original_date"][:4]

            if classification in ROUTING_MAP:
                cohorts.setdefault(classification, set()).add(year)

    # For each cohort, calculate remaining days and apply lifecycle rule
    for classification, years in cohorts.items():
        bucket, base_prefix = ROUTING_MAP[classification]
        rules               = RETENTION_RULES.get(classification)

        if not rules:
            logger.warning("No retention rules defined for %s — skipping", classification)
            continue

        for year in sorted(years):
            # Approximate days elapsed since original creation
            # Using Jan 1 of cohort year as a conservative baseline
            cohort_start  = datetime(int(year), 1, 1, tzinfo=timezone.utc).date()
            days_elapsed  = (today - cohort_start).days
            remaining_del = rules["delete_days"] - days_elapsed

            cohort_prefix = f"migration/{base_prefix}{year}/"

            if remaining_del <= 0:
                # Object is past its retention deadline — flag for immediate action
                logger.warning(
                    "OVERDUE: %s | classification=%s | year=%s | "
                    "exceeded retention by %d days",
                    cohort_prefix, classification, year, abs(remaining_del)
                )
                overdue.append({
                    "bucket"        : bucket,
                    "prefix"        : cohort_prefix,
                    "classification": classification,
                    "year"          : year,
                    "overdue_days"  : abs(remaining_del),
                })
                continue

            # Build the reduced DELETE lifecycle rule for this cohort
            rule_name = f"migrate-{classification}-{year}-delete"

            logger.info(
                "Cohort rule: %s | bucket=%s | prefix=%s | delete_after=%d days",
                rule_name, bucket, cohort_prefix, remaining_del
            )

            if not DRY_RUN:
                # GET existing policy rules so we can merge — never replace the whole policy.
                # Replacing without merging would wipe all Terraform-managed lifecycle rules.
                try:
                    existing_policy = client.get_object_lifecycle_policy(oci_namespace, bucket)
                    existing_rules  = existing_policy.data.items or []
                except oci.exceptions.ServiceError as e:
                    if e.status == 404:
                        existing_rules = []
                    else:
                        logger.error("Failed to GET lifecycle policy for %s: %s", bucket, e.message)
                        continue

                # Remove any stale rule with the same name (idempotent re-run support)
                merged_rules = [r for r in existing_rules if r.name != rule_name]

                # Append the new cohort rule
                merged_rules.append(
                    oci.object_storage.models.ObjectLifecycleRule(
                        name        = rule_name,
                        action      = "DELETE",
                        time_amount = remaining_del,
                        time_unit   = "DAYS",
                        is_enabled  = True,
                        object_name_filter = oci.object_storage.models.ObjectNameFilter(
                            inclusion_prefixes = [cohort_prefix]
                        ),
                    )
                )

                # PUT the merged policy atomically — Terraform rules are preserved
                try:
                    client.put_object_lifecycle_policy(
                        oci_namespace,
                        bucket,
                        oci.object_storage.models.PutObjectLifecyclePolicyDetails(
                            items = merged_rules
                        ),
                    )
                    rules_added += 1
                except oci.exceptions.ServiceError as e:
                    logger.error("Failed to apply rule %s: %s", rule_name, e.message)

    # Summary of OVERDUE objects
    if overdue:
        logger.warning("=" * 60)
        logger.warning("ACTION REQUIRED — %d OVERDUE cohorts found:", len(overdue))
        for item in overdue:
            logger.warning(
                "  DELETE IMMEDIATELY: bucket=%s prefix=%s (overdue by %d days)",
                item["bucket"], item["prefix"], item["overdue_days"]
            )
        logger.warning("These objects exceeded their retention deadline.")
        logger.warning("They must be deleted manually or via an emergency lifecycle rule.")
        logger.warning("=" * 60)
    else:
        logger.info("No OVERDUE cohorts found.")

    logger.info("Phase 3 complete | rules_added=%d | overdue_cohorts=%d", rules_added, len(overdue))
    return rules_added, overdue


# =============================================================================
# ENTRYPOINT — run all three phases in sequence
# =============================================================================

def main():
    logger.info("=" * 60)
    logger.info("OCI MIGRATION SCRIPT — S3 → OCI Object Storage")
    logger.info("Source bucket : %s", S3_SOURCE_BUCKET)
    logger.info("DRY RUN       : %s", DRY_RUN)
    logger.info("SKIP EXISTING : %s", SKIP_EXISTING)
    logger.info("=" * 60)

    # Phase 1 — always run first to build the inventory
    total = phase1_extract_inventory()
    if total == 0:
        logger.error("No objects found. Exiting.")
        return

    # Phase 2 — copy objects S3 → OCI
    copied, errors = phase2_copy_objects()
    if errors > 0:
        logger.warning("%d errors in Phase 2 — review migration.log before Phase 3", errors)

    # Phase 3 — apply cohort-specific lifecycle rules
    _, client_ns = _get_oci_client()
    phase3_apply_cohort_lifecycle_rules(oci_namespace=client_ns)

    logger.info("=" * 60)
    logger.info("Migration complete.")
    logger.info("Monitor OCI lifecycle rules for the first 30 days post-migration.")
    logger.info("After validation, this script can be safely deleted.")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
