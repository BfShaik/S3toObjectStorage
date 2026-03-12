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
    # valid_classifications — useful for validation in calling code
    # -------------------------------------------------------------------------
    @property
    def valid_classifications(self) -> list[str]:
        """Return sorted list of valid classification keys."""
        return sorted(self._valid_keys)
