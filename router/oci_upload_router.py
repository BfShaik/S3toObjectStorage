# =============================================================================
# oci_upload_router.py
# =============================================================================
#
# WHAT THIS FILE DOES:
#   This is the only custom code that runs continuously in production.
#   It reads routing rules from config/classifications.json at startup —
#   the exact same file Terraform uses to create buckets and lifecycle
#   policies. There is no separate routing map hardcoded here.
#
#   When the ops team adds a new classification to classifications.json
#   and runs terraform apply, this router picks up the new route
#   automatically on next application start — no code change needed.
#
#   Responsibilities:
#     1. Load routing map from classifications.json at startup
#     2. Validate the classification on every upload call
#     3. Route the object to the correct OCI bucket + prefix
#     4. Attach opc-meta-* metadata (for audit — not for lifecycle)
#     5. Call OCI put_object() once atomically
#
#   After upload, OCI native lifecycle policies handle all archiving
#   and deletion automatically. This code has no further involvement.
#
# OWNED BY:   Application / dev team
# RUNS:       Every time an object is uploaded
# CONFIG:     ../config/classifications.json  (shared with Terraform)
# DEPENDS ON: oci  (pip install oci)
# =============================================================================

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import oci
import oci.exceptions
import oci.object_storage

logger = logging.getLogger(__name__)

# Path to the shared config file — same file Terraform reads
# Resolves to: <project-root>/config/classifications.json
_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "config", "classifications.json"
)


def _load_routing_map() -> dict[str, tuple[str, str]]:
    """
    Read classifications.json and build the routing map.

    Returns:
        { "pii-customers": ("bucket-pii-prod", "customers/"), ... }

    Called once at class initialisation. If the JSON file is missing
    or malformed, this raises immediately — fast fail on startup.
    """
    config_path = os.path.normpath(_CONFIG_PATH)

    if not os.path.exists(config_path):
        raise FileNotFoundError(
            f"classifications.json not found at: {config_path}\n"
            f"Expected location: config/classifications.json in project root."
        )

    with open(config_path) as f:
        config = json.load(f)

    if "classifications" not in config:
        raise KeyError("classifications.json must contain a top-level 'classifications' key.")

    routing_map = {
        key: (val["bucket"], val["prefix"])
        for key, val in config["classifications"].items()
        if not key.startswith("_")   # skip _comment and _note fields
    }

    logger.info(
        "Routing map loaded from %s | %d classifications available",
        config_path, len(routing_map)
    )
    return routing_map


class OCIUploadRouter:
    """
    Single upload interface for all application teams.

    Reads its routing rules from config/classifications.json —
    the same file Terraform uses. One config file drives both
    infrastructure and application routing. They cannot diverge.

    Example usage:
        router = OCIUploadRouter()

        # Upload a PII customer record
        router.upload(
            object_name    = "customer-9821-profile.json",
            body           = file_bytes,
            classification = "pii-customers",
        )
        # → uploaded to: bucket-pii-prod/customers/customer-9821-profile.json
        # → OCI archives after 90 days automatically
        # → OCI deletes after 7 years automatically
        # → no further code runs in the lifecycle path
    """

    def __init__(self, config_path: str = "~/.oci/config", profile: str = "DEFAULT"):
        """
        Initialise OCI client and load routing map from classifications.json.

        Args:
            config_path : Path to OCI SDK config file. Default: ~/.oci/config
            profile     : OCI config profile name.    Default: DEFAULT
        """
        oci_cfg             = oci.config.from_file(config_path, profile)
        self._client        = oci.object_storage.ObjectStorageClient(oci_cfg)
        self._namespace     = self._client.get_namespace().data
        self._routing_map   = _load_routing_map()
        self._valid_keys    = set(self._routing_map.keys())

    # -------------------------------------------------------------------------
    # upload — the single method all application teams call
    # -------------------------------------------------------------------------
    def upload(
        self,
        object_name    : str,
        body           : bytes,
        classification : str,
        content_type   : str = "application/octet-stream",
        extra_meta     : Optional[dict] = None,
        original_date  : Optional[datetime] = None,
    ) -> dict:
        """
        Upload an object to the correct OCI bucket and prefix.

        Args:
            object_name    : Filename / object key  (without prefix)
            body           : Object content as bytes
            classification : Data classification key from classifications.json
                             e.g. "pii-customers", "compliance-sox", "temp-raw"
            content_type   : MIME type. Default: application/octet-stream
            extra_meta     : Optional extra metadata key-value pairs
            original_date  : Pass the original source system creation date
                             when migrating historical objects. Leave None
                             for new objects — upload timestamp is used.

        Returns:
            { bucket, key, namespace, classification }

        Raises:
            ValueError                  : Unknown classification
            oci.exceptions.ServiceError : OCI API error
        """

        # ── Step 1: Validate ─────────────────────────────────────────────────
        if classification not in self._valid_keys:
            raise ValueError(
                f"Unknown classification '{classification}'.\n"
                f"Valid values: {sorted(self._valid_keys)}\n"
                f"To add a new one: edit config/classifications.json"
            )

        # ── Step 2: Route ─────────────────────────────────────────────────────
        bucket, prefix = self._routing_map[classification]
        full_key       = f"{prefix}{object_name}"

        # ── Step 3: Build metadata ────────────────────────────────────────────
        # Stored for audit and search. Does NOT drive lifecycle.
        # OCI native policies on the bucket handle lifecycle.
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        metadata = {
            "classification"         : classification,
            "upload-timestamp"       : now,
            "original-creation-date" : (
                original_date.strftime("%Y-%m-%dT%H:%M:%SZ")
                if original_date else now
            ),
        }
        if extra_meta:
            metadata.update(extra_meta)

        # ── Step 4: Upload ────────────────────────────────────────────────────
        try:
            self._client.put_object(
                namespace_name  = self._namespace,
                bucket_name     = bucket,
                object_name     = full_key,
                put_object_body = body,
                content_type    = content_type,
                opc_meta        = metadata,
            )
        except oci.exceptions.ServiceError as e:
            logger.error(
                "Upload failed | key=%s | bucket=%s | http=%s | %s",
                full_key, bucket, e.status, e.message
            )
            raise

        logger.info(
            "Uploaded | bucket=%s | key=%s | classification=%s",
            bucket, full_key, classification
        )
        return {
            "bucket"        : bucket,
            "key"           : full_key,
            "namespace"     : self._namespace,
            "classification": classification,
        }

    # -------------------------------------------------------------------------
    # get_metadata — equivalent of S3 get_object_tagging
    # -------------------------------------------------------------------------
    def get_metadata(self, bucket: str, object_key: str) -> dict:
        """
        Read opc-meta-* metadata from an existing object.
        Lightweight HEAD request — does not download the object body.

        Args:
            bucket     : OCI bucket name
            object_key : Full object key including prefix

        Returns:
            dict of metadata key-value pairs (opc-meta-* prefix stripped)
        """
        head = self._client.head_object(self._namespace, bucket, object_key)
        return {
            key.replace("opc-meta-", ""): val
            for key, val in head.headers.items()
            if key.lower().startswith("opc-meta-")
        }

    # -------------------------------------------------------------------------
    # update_lifecycle — change retention days for a specific object in-place
    # -------------------------------------------------------------------------
    def update_lifecycle(
        self,
        bucket           : str,
        object_key       : str,
        new_delete_days  : int,
        new_archive_days : Optional[int] = None,
    ) -> None:
        """
        Override lifecycle rules for a specific object without moving it.

        OCI lifecycle policies are bucket-level, not object-level. This
        method adds prefix-exact rules to the bucket policy that target
        only the given object key. Uses GET → merge → PUT to preserve all
        existing Terraform-managed and migration rules.

        Idempotent: calling it twice with the same key replaces the
        previous override, not duplicates it.

        Args:
            bucket           : OCI bucket name (from upload() return value)
            object_key       : Full object key including prefix
            new_delete_days  : Days after upload before deletion
            new_archive_days : Days after upload before archival (optional;
                               omit to leave the bucket-level archive rule
                               unchanged for this object)

        Raises:
            oci.exceptions.ServiceError : OCI API error

        Note:
            OCI buckets support ~1000 lifecycle rules. If you need to
            override lifecycle for many individual objects, use
            reclassify() to move them to a prefix that already has the
            right policy — it scales without limit.

        Example:
            result = router.upload("report.pdf", body, "temp-raw")
            # Legal hold — extend delete window to 10 years
            router.update_lifecycle(
                bucket          = result["bucket"],
                object_key      = result["key"],
                new_delete_days = 3650,
            )
        """
        safe_name   = object_key.replace("/", "-").replace(".", "-")
        del_rule    = f"override-{safe_name}-delete"
        arch_rule   = f"override-{safe_name}-archive"
        stale_names = {del_rule, arch_rule}

        # GET existing policy — preserve all other rules
        try:
            existing       = self._client.get_object_lifecycle_policy(self._namespace, bucket)
            existing_rules = list(existing.data.items or [])
        except oci.exceptions.ServiceError as e:
            if e.status == 404:
                existing_rules = []
            else:
                logger.error(
                    "Failed to GET lifecycle policy for %s: http=%s | %s",
                    bucket, e.status, e.message
                )
                raise

        # Remove stale override rules for this object (idempotent re-run)
        merged_rules = [r for r in existing_rules if r.name not in stale_names]

        # Add archive override if requested
        if new_archive_days is not None:
            merged_rules.append(
                oci.object_storage.models.ObjectLifecycleRule(
                    name       = arch_rule,
                    action     = "ARCHIVE",
                    time_amount = new_archive_days,
                    time_unit  = "DAYS",
                    is_enabled = True,
                    object_name_filter = oci.object_storage.models.ObjectNameFilter(
                        inclusion_prefixes=[object_key]
                    ),
                )
            )

        # Add delete override
        merged_rules.append(
            oci.object_storage.models.ObjectLifecycleRule(
                name       = del_rule,
                action     = "DELETE",
                time_amount = new_delete_days,
                time_unit  = "DAYS",
                is_enabled = True,
                object_name_filter = oci.object_storage.models.ObjectNameFilter(
                    inclusion_prefixes=[object_key]
                ),
            )
        )

        # PUT merged policy — Terraform-managed rules untouched
        self._client.put_object_lifecycle_policy(
            self._namespace,
            bucket,
            oci.object_storage.models.PutObjectLifecyclePolicyDetails(items=merged_rules),
        )

        logger.info(
            "Lifecycle overridden | bucket=%s | key=%s | delete=%dd | archive=%s",
            bucket, object_key, new_delete_days,
            f"{new_archive_days}d" if new_archive_days is not None else "unchanged",
        )

    # -------------------------------------------------------------------------
    # reclassify — move object to a different classification's bucket/prefix
    # -------------------------------------------------------------------------
    def reclassify(
        self,
        object_key         : str,
        current_bucket     : str,
        new_classification : str,
        extra_meta         : Optional[dict] = None,
    ) -> dict:
        """
        Move an object to a new classification's bucket and prefix.

        Downloads the object from its current location, re-uploads to the
        bucket/prefix defined for new_classification (preserving all
        existing metadata and the original creation date), then deletes
        the original. The original is only deleted after a successful
        upload — no data loss on partial failure.

        Use this when the object's data class has genuinely changed
        (e.g. temp-raw promoted to compliance-sox after legal review),
        or when you need per-object lifecycle changes at scale (avoids
        the ~1000 rule bucket policy limit that update_lifecycle has).

        Args:
            object_key         : Full current key including prefix
            current_bucket     : Current OCI bucket name
            new_classification : Target classification from classifications.json
            extra_meta         : Extra metadata to attach on re-upload (optional)

        Returns:
            Same shape as upload(): { bucket, key, namespace, classification }

        Raises:
            ValueError                  : Unknown new_classification
            oci.exceptions.ServiceError : OCI API error

        Example:
            result = router.upload("contract.pdf", body, "temp-raw")
            # After legal review — promote to long-term compliance bucket
            new = router.reclassify(
                object_key         = result["key"],
                current_bucket     = result["bucket"],
                new_classification = "compliance-sox",
            )
            # Object now lives in bucket-compliance-prod/sox/contract.pdf
            # with 7-year delete policy applied automatically
        """
        if new_classification not in self._valid_keys:
            raise ValueError(
                f"Unknown classification '{new_classification}'.\n"
                f"Valid values: {sorted(self._valid_keys)}"
            )

        # ── Step 1: Download object + read existing metadata ─────────────────
        try:
            response = self._client.get_object(self._namespace, current_bucket, object_key)
            body     = response.data.content
            head     = self._client.head_object(self._namespace, current_bucket, object_key)
            old_meta = {
                k.replace("opc-meta-", ""): v
                for k, v in head.headers.items()
                if k.lower().startswith("opc-meta-")
            }
        except oci.exceptions.ServiceError as e:
            logger.error(
                "Failed to read source object %s/%s: http=%s | %s",
                current_bucket, object_key, e.status, e.message
            )
            raise

        # ── Step 2: Build relative key — strip old classification prefix ──────
        old_classification = old_meta.get("classification", "")
        if old_classification in self._routing_map:
            _, old_prefix = self._routing_map[old_classification]
            # Strip old prefix to get the relative sub-path
            relative_key = (
                object_key[len(old_prefix):]
                if object_key.startswith(old_prefix)
                else object_key.split("/")[-1]
            )
        else:
            # Unknown old classification — use leaf filename only
            relative_key = object_key.split("/")[-1]

        # ── Step 3: Merge metadata — preserve creation date, update class ─────
        merged_meta = {**old_meta}
        merged_meta["classification"]       = new_classification
        merged_meta["reclassified-from"]    = old_classification or "unknown"
        merged_meta["reclassification-date"] = (
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        if extra_meta:
            merged_meta.update(extra_meta)

        # Recover original creation date for upload() so it is preserved
        original_date = None
        raw_date = old_meta.get("original-creation-date")
        if raw_date:
            try:
                original_date = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            except ValueError:
                pass

        # ── Step 4: Upload to new classification ──────────────────────────────
        result = self.upload(
            object_name    = relative_key,
            body           = body,
            classification = new_classification,
            extra_meta     = merged_meta,
            original_date  = original_date,
        )

        # ── Step 5: Delete original — only after successful upload ────────────
        try:
            self._client.delete_object(self._namespace, current_bucket, object_key)
        except oci.exceptions.ServiceError as e:
            logger.error(
                "Upload succeeded but delete of original failed %s/%s: http=%s | %s — "
                "object exists in both locations, manual cleanup needed.",
                current_bucket, object_key, e.status, e.message
            )
            raise

        new_bucket, _ = self._routing_map[new_classification]
        logger.info(
            "Reclassified | %s/%s → %s/%s | %s → %s",
            current_bucket, object_key,
            new_bucket, result["key"],
            old_classification or "?", new_classification,
        )
        return result

    # -------------------------------------------------------------------------
    # valid_classifications — useful for validation in calling code
    # -------------------------------------------------------------------------
    @property
    def valid_classifications(self) -> list[str]:
        """Return sorted list of valid classification keys."""
        return sorted(self._valid_keys)
