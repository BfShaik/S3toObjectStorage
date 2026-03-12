# =============================================================================
# migration_script.py
# =============================================================================
#
# WHAT THIS FILE DOES:
#   One-time script that migrates all objects from AWS S3 to OCI Object
#   Storage. Runs once during the cutover window and is discarded after.
#   Reads routing rules from config/classifications.json — the same file
#   Terraform and the upload router use. No routing logic is hardcoded here.
#
#   PHASE 1 — Extract S3 inventory
#     Scans every object in the S3 source bucket and writes its key,
#     original LastModified date, S3 tags, size, and storage class
#     to a local CSV. Review this CSV before running Phase 2.
#
#   PHASE 2 — Copy objects S3 → OCI
#     For each object: downloads from S3, translates S3 tags to
#     opc-meta-* metadata, preserves the original S3 creation date
#     (CRITICAL — without this every object gets a fresh retention
#     window from today, violating compliance), and uploads to OCI
#     under a year-cohort prefix. Script is fully resumable.
#
#   PHASE 3 — Apply reduced lifecycle rules for cohort prefixes
#     A 4-year-old object must not get a fresh 7-year delete window.
#     Calculates remaining retention per year-cohort and applies
#     reduced DELETE rules via OCI SDK (GET → merge → PUT atomically).
#     Objects already past their deadline are flagged OVERDUE.
#
# OWNED BY:   Migration team
# RUNS:       Once during cutover window — delete after validation
# RESUMABLE:  Yes — re-running skips objects already present in OCI
# DEPENDS ON: boto3 (pip install boto3)
#             oci   (pip install oci)
#             config/classifications.json  (shared with Terraform + router)
# =============================================================================

import csv
import json
import logging
import os
from datetime import datetime, timezone

import boto3
import oci
import oci.exceptions
import oci.object_storage

# =============================================================================
# CONFIGURATION — edit these values before running
# =============================================================================

S3_SOURCE_BUCKET = "your-source-s3-bucket"   # AWS S3 bucket to migrate from
S3_REGION        = "us-east-1"               # AWS region of source bucket
OCI_CONFIG_PATH  = "~/.oci/config"           # OCI SDK config file path
INVENTORY_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "s3_inventory.csv")
LOG_FILE         = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migration.log")

SKIP_EXISTING    = True    # Skip objects already in OCI — safe for re-runs
DRY_RUN          = True    # ALWAYS start True — set False only after reviewing migration.log

# Path to shared config — same file used by Terraform and the upload router
_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "config", "classifications.json"
)

# =============================================================================
# SETUP
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


def _load_config() -> dict:
    """
    Load classifications.json — the single source of truth.
    Returns the full classifications dict.
    Raises immediately if the file is missing or malformed.
    """
    config_path = os.path.normpath(_CONFIG_PATH)
    if not os.path.exists(config_path):
        raise FileNotFoundError(
            f"classifications.json not found at: {config_path}"
        )
    with open(config_path) as f:
        data = json.load(f)
    return {
        k: v for k, v in data["classifications"].items()
        if not k.startswith("_")
    }


def _build_routing_map(classifications: dict) -> dict:
    """
    Build routing map from classifications config.
    Returns: { "pii-customers": ("bucket-pii-prod", "customers/"), ... }
    Same logic as oci_upload_router.py — consistent by design.
    """
    return {
        key: (val["bucket"], val["prefix"])
        for key, val in classifications.items()
    }


def _build_retention_map(classifications: dict) -> dict:
    """
    Build retention map from classifications config.
    Returns: { "pii-customers": { archive_days: 90, delete_days: 2555 }, ... }
    Used in Phase 3 to calculate remaining retention per cohort.
    """
    return {
        key: {
            "archive_days": val.get("archive_days"),
            "delete_days" : val["delete_days"],
        }
        for key, val in classifications.items()
    }


def _get_oci_client():
    config    = oci.config.from_file(OCI_CONFIG_PATH)
    client    = oci.object_storage.ObjectStorageClient(config)
    namespace = client.get_namespace().data
    return client, namespace


def _get_s3_client():
    return boto3.client("s3", region_name=S3_REGION)


def _object_exists_in_oci(client, namespace, bucket, key) -> bool:
    """Return True if object already exists in OCI. Used for resumable runs."""
    try:
        client.head_object(namespace, bucket, key)
        return True
    except oci.exceptions.ServiceError as e:
        if e.status == 404:
            return False
        raise  # propagate auth errors, 500s — don't silently swallow them


# =============================================================================
# PHASE 1 — Extract S3 inventory
# =============================================================================

def phase1_extract_inventory() -> int:
    """
    Scan S3 source bucket and write all object metadata to INVENTORY_FILE.
    This CSV is the source of truth for Phase 2 — review it before proceeding.

    Columns: key, original_date, size_bytes, storage_class, tags (JSON string)
    """
    logger.info("=" * 65)
    logger.info("PHASE 1: Extracting S3 inventory from: %s", S3_SOURCE_BUCKET)
    logger.info("=" * 65)

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

            # Read all S3 tags — each becomes one opc-meta-* key on OCI
            try:
                tag_resp = s3.get_object_tagging(Bucket=S3_SOURCE_BUCKET, Key=key)
                tags     = {t["Key"]: t["Value"] for t in tag_resp.get("TagSet", [])}
            except Exception as e:
                logger.warning("Could not read tags for %s: %s", key, e)
                tags = {}

            inventory.append({
                "key"          : key,
                "original_date": original_date,  # CRITICAL — do not lose this
                "size_bytes"   : size_bytes,
                "storage_class": storage_class,
                "tags"         : json.dumps(tags),
            })
            count += 1
            if count % 1000 == 0:
                logger.info("  Progress: %d objects inventoried...", count)

    if not inventory:
        logger.warning("No objects found in bucket: %s", S3_SOURCE_BUCKET)
        return 0

    with open(INVENTORY_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=inventory[0].keys())
        writer.writeheader()
        writer.writerows(inventory)

    logger.info("Phase 1 complete: %d objects → %s", count, INVENTORY_FILE)
    logger.info("ACTION: Review %s before running Phase 2", INVENTORY_FILE)
    return count


# =============================================================================
# PHASE 2 — Copy objects S3 → OCI
# =============================================================================

def phase2_copy_objects(routing_map: dict) -> tuple[int, int]:
    """
    Copy all objects from the S3 inventory to OCI.

    Year-cohort prefix strategy:
        Objects land at:  migration/{base_prefix}{year}/{original_key}
        e.g.              migration/customers/2022/reports/file.json
        This lets Phase 3 apply REDUCED delete rules per cohort so that
        old objects do not get a fresh full-length retention window.

    Metadata strategy:
        All S3 tags → opc-meta-* keys (1:1 translation)
        opc-meta-original-creation-date always = S3 LastModified date
        This is the most critical field — it preserves true object age.
    """
    logger.info("=" * 65)
    logger.info("PHASE 2: Copying objects S3 → OCI")
    logger.info("DRY_RUN=%s | SKIP_EXISTING=%s", DRY_RUN, SKIP_EXISTING)
    logger.info("=" * 65)

    client, namespace = _get_oci_client()
    s3                = _get_s3_client()
    today             = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    copied = skipped = errors = 0

    with open(INVENTORY_FILE, newline="") as f:
        rows = list(csv.DictReader(f))

    total = len(rows)
    logger.info("Processing %d objects from inventory...", total)

    for i, row in enumerate(rows, 1):
        key            = row["key"]
        original_date  = row["original_date"]        # S3 LastModified — preserve
        tags           = json.loads(row["tags"])
        classification = tags.get("classification", "")

        # Validate classification exists in config
        if classification not in routing_map:
            logger.warning(
                "[%d/%d] Unknown classification '%s' for %s — skipping",
                i, total, classification, key
            )
            errors += 1
            continue

        bucket, base_prefix = routing_map[classification]

        # Year-cohort key — encodes original age into the object path
        cohort_year   = original_date[:4]
        migration_key = f"migration/{base_prefix}{cohort_year}/{key}"

        # Skip if already in OCI (safe re-run support)
        if SKIP_EXISTING and _object_exists_in_oci(client, namespace, bucket, migration_key):
            skipped += 1
            continue

        if DRY_RUN:
            logger.info("[DRY RUN] [%d/%d] %s → %s/%s", i, total, key, bucket, migration_key)
            copied += 1
            continue

        # Download from S3
        try:
            body = s3.get_object(Bucket=S3_SOURCE_BUCKET, Key=key)["Body"].read()
        except Exception as e:
            logger.error("[%d/%d] S3 download failed: %s | %s", i, total, key, e)
            errors += 1
            continue

        # Build OCI metadata — translate all S3 tags + add migration fields
        metadata = {k: v for k, v in tags.items()}
        metadata["original-creation-date"] = original_date   # CRITICAL
        metadata["migrated-from"]          = "s3"
        metadata["migration-date"]         = today
        metadata["source-bucket"]          = S3_SOURCE_BUCKET
        metadata["source-storage-class"]   = row["storage_class"]

        # Upload to OCI — object + metadata in one atomic call
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
                "[%d/%d] OCI upload failed: %s | http=%s | %s",
                i, total, key, e.status, e.message
            )
            errors += 1

    logger.info(
        "Phase 2 complete | copied=%d | skipped=%d | errors=%d",
        copied, skipped, errors
    )
    return copied, errors


# =============================================================================
# PHASE 3 — Apply reduced lifecycle rules for migration cohorts
# =============================================================================

def phase3_apply_cohort_lifecycle_rules(
    routing_map   : dict,
    retention_map : dict,
    client,
    oci_namespace : str,
) -> tuple[int, list]:
    """
    Calculate remaining retention days per year-cohort and apply a reduced
    DELETE lifecycle rule to each cohort prefix in OCI.

    Why this matters:
        All migrated objects have Last-Modified = today (migration date).
        OCI lifecycle counts retention from Last-Modified — so without
        cohort rules, a 4-year-old PII file would get a fresh 7-year window.
        That is a compliance violation.

    Example:
        Classification : pii-customers  (full retention = 2555 days)
        Cohort year    : 2022  (approx 4 years ago)
        Days elapsed   : ~1460
        Remaining      : 2555 - 1460 = 1095 days
        Rule applied   : DELETE migration/customers/2022/ after 1095 days
    """
    logger.info("=" * 65)
    logger.info("PHASE 3: Applying reduced lifecycle rules for cohort prefixes")
    logger.info("=" * 65)

    today       = datetime.now(timezone.utc).date()
    overdue     = []
    rules_added = 0

    # Collect all unique classification + year combinations from inventory
    cohorts: dict[str, set] = {}
    with open(INVENTORY_FILE, newline="") as f:
        for row in csv.DictReader(f):
            tags           = json.loads(row["tags"])
            classification = tags.get("classification", "")
            year           = row["original_date"][:4]
            if classification in routing_map:
                cohorts.setdefault(classification, set()).add(year)

    for classification, years in cohorts.items():
        bucket, base_prefix = routing_map[classification]
        retention           = retention_map.get(classification)
        if not retention:
            logger.warning("No retention config for '%s' — skipping", classification)
            continue

        for year in sorted(years):
            # Days elapsed since start of cohort year (conservative baseline)
            cohort_start  = datetime(int(year), 1, 1).date()
            days_elapsed  = (today - cohort_start).days
            remaining_del = retention["delete_days"] - days_elapsed
            cohort_prefix = f"migration/{base_prefix}{year}/"

            if remaining_del <= 0:
                # Past retention deadline — flag for immediate deletion
                logger.warning(
                    "OVERDUE | bucket=%s | prefix=%s | exceeded by %d days",
                    bucket, cohort_prefix, abs(remaining_del)
                )
                overdue.append({
                    "bucket"      : bucket,
                    "prefix"      : cohort_prefix,
                    "overdue_days": abs(remaining_del),
                    "classification": classification,
                    "year"        : year,
                })
                continue

            logger.info(
                "Cohort rule | bucket=%s | prefix=%s | delete_after=%d days",
                bucket, cohort_prefix, remaining_del
            )

            if not DRY_RUN:
                rule_name = f"cohort-{classification}-{year}-delete"

                # GET existing policy so we can merge — never replace the whole policy.
                # Replacing without merging wipes all Terraform-managed lifecycle rules.
                try:
                    existing = client.get_object_lifecycle_policy(oci_namespace, bucket)
                    existing_rules = existing.data.items or []
                except oci.exceptions.ServiceError as e:
                    if e.status == 404:
                        existing_rules = []
                    else:
                        logger.error("Failed to GET lifecycle policy for %s: %s", bucket, e.message)
                        continue

                # Remove stale rule with same name (idempotent re-run support)
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

                # PUT merged policy atomically — Terraform rules are preserved
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
                    logger.error("Failed to apply cohort rule %s: %s", rule_name, e.message)

    if overdue:
        logger.warning("=" * 65)
        logger.warning("ACTION REQUIRED — %d OVERDUE cohorts:", len(overdue))
        for item in overdue:
            logger.warning(
                "  DELETE IMMEDIATELY: bucket=%s | prefix=%s | overdue by %d days",
                item["bucket"], item["prefix"], item["overdue_days"]
            )
        logger.warning("These objects exceeded retention. Delete manually.")
        logger.warning("=" * 65)

    logger.info(
        "Phase 3 complete | rules_added=%d | overdue=%d",
        rules_added, len(overdue)
    )
    return rules_added, overdue


# =============================================================================
# ENTRYPOINT
# =============================================================================

def main():
    logger.info("=" * 65)
    logger.info("OCI MIGRATION — S3 → OCI Object Storage")
    logger.info("Source : %s | DRY_RUN=%s | SKIP_EXISTING=%s",
                S3_SOURCE_BUCKET, DRY_RUN, SKIP_EXISTING)
    logger.info("Config : %s", os.path.normpath(_CONFIG_PATH))
    logger.info("=" * 65)

    # Load single config — shared with Terraform and upload router
    classifications = _load_config()
    routing_map     = _build_routing_map(classifications)
    retention_map   = _build_retention_map(classifications)

    logger.info("Loaded %d classifications from config", len(classifications))

    # Phase 1
    total = phase1_extract_inventory()
    if total == 0:
        logger.error("No objects found in source bucket. Exiting.")
        return

    # Phase 2
    copied, errors = phase2_copy_objects(routing_map)
    if errors > 0:
        logger.warning(
            "%d errors in Phase 2 — review %s before Phase 3", errors, LOG_FILE
        )

    # Phase 3
    client, namespace = _get_oci_client()
    phase3_apply_cohort_lifecycle_rules(routing_map, retention_map, client, namespace)

    logger.info("=" * 65)
    logger.info("Migration complete.")
    logger.info("Monitor OCI lifecycle rules for the first 30 days.")
    logger.info("Delete this script after cutover validation is confirmed.")
    logger.info("=" * 65)


if __name__ == "__main__":
    main()
