# =============================================================================
# oci_upload_router.py
# =============================================================================
#
# WHAT THIS FILE DOES:
#   This is the only custom code that runs continuously in production.
#   It provides a single upload() method that application teams call
#   instead of calling the OCI SDK directly. The router looks up the
#   correct bucket and prefix from router_config.py based on the
#   classification provided, attaches the required metadata, and
#   calls OCI put_object() in one atomic operation.
#
#   Once the object lands in the correct bucket/prefix, OCI native
#   lifecycle policies take over automatically — this code has no
#   further involvement in archiving or deletion.
#
#   Key responsibilities:
#     1. Validate the classification exists in ROUTING_MAP
#     2. Route the object to the correct bucket + prefix
#     3. Attach opc-meta-* metadata (for audit — NOT for lifecycle)
#     4. Call OCI put_object() once
#     5. Return the final bucket and key for the caller's reference
#
# OWNED BY:   Application / dev team
# RUNS:       Every time an object is uploaded to OCI
# COMPLEXITY: ~80 lines — stable after initial build
# DEPENDS ON: router_config.py  (for routing rules)
#             oci SDK           (pip install oci)
# =============================================================================

import logging
from datetime import datetime, timezone
from typing import Optional

import oci
import oci.object_storage
import oci.exceptions

from router_config import ROUTING_MAP, VALID_CLASSIFICATIONS

logger = logging.getLogger(__name__)


class OCIUploadRouter:
    """
    Drop-in replacement for direct OCI SDK put_object calls.

    Application teams call upload() with a classification string.
    The router handles bucket selection, prefix routing, metadata
    attachment, and error logging. OCI lifecycle handles the rest.

    Usage:
        router = OCIUploadRouter()
        result = router.upload(
            object_name    = "customer-12345-profile.json",
            body           = file_bytes,
            classification = "pii-customers"
        )
        # Object is now in bucket-pii-prod/customers/customer-12345-profile.json
        # OCI will archive it after 90 days and delete it after 7 years
    """

    def __init__(self, config_path: str = "~/.oci/config", profile: str = "DEFAULT"):
        """
        Initialise the OCI Object Storage client.

        Args:
            config_path : Path to OCI config file. Defaults to ~/.oci/config
            profile     : OCI config profile name. Defaults to DEFAULT
        """
        oci_config         = oci.config.from_file(config_path, profile)
        self._client       = oci.object_storage.ObjectStorageClient(oci_config)
        self._namespace    = self._client.get_namespace().data
        logger.info("OCIUploadRouter initialised | namespace=%s", self._namespace)

    # -------------------------------------------------------------------------
    # PUBLIC — upload
    # The single method all application teams use for all uploads
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
            object_name    : The object filename/key (without prefix)
            body           : Object content as bytes
            classification : Data classification key from ROUTING_MAP
                             e.g. "pii-customers", "compliance-sox", "temp-raw"
            content_type   : MIME type. Defaults to application/octet-stream
            extra_meta     : Optional additional metadata key-value pairs
            original_date  : Original creation date — used for migrated objects
                             to preserve the true creation date from the source
                             system. Leave None for new objects (uses now).

        Returns:
            dict with keys: bucket, key, namespace, classification

        Raises:
            ValueError              : Unknown classification key
            oci.exceptions.ServiceError : OCI API error during upload
        """

        # Step 1 — Validate classification
        # Fail fast before any network call if classification is unknown
        if classification not in VALID_CLASSIFICATIONS:
            raise ValueError(
                f"Unknown classification '{classification}'. "
                f"Valid values: {sorted(VALID_CLASSIFICATIONS)}"
            )

        # Step 2 — Look up routing from config
        bucket, prefix = ROUTING_MAP[classification]

        # Step 3 — Build the full object key (prefix + filename)
        full_key = f"{prefix}{object_name}"

        # Step 4 — Build metadata
        # These are stored for audit and search purposes ONLY.
        # They do NOT drive lifecycle enforcement — OCI native policies do that.
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        metadata = {
            "classification"         : classification,
            "upload-timestamp"       : now,
            # original-creation-date is critical for migrated objects.
            # For new objects it equals upload time — which is correct.
            "original-creation-date" : (
                original_date.strftime("%Y-%m-%dT%H:%M:%SZ")
                if original_date else now
            ),
        }

        # Merge any extra metadata the caller wants to attach
        if extra_meta:
            metadata.update(extra_meta)

        # Step 5 — Upload to OCI
        # Single atomic call: object + metadata land together
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
                "Upload failed | key=%s | bucket=%s | status=%s | message=%s",
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
    # PUBLIC — get_metadata
    # Equivalent of S3 get_object_tagging — reads metadata without downloading
    # -------------------------------------------------------------------------
    def get_metadata(self, bucket: str, object_key: str) -> dict:
        """
        Read opc-meta-* metadata from an existing object.
        Does NOT download the object body — lightweight HEAD request only.

        Args:
            bucket     : OCI bucket name
            object_key : Full object key including prefix

        Returns:
            dict of metadata key-value pairs (opc-meta-* prefix stripped)
        """
        head = self._client.head_object(self._namespace, bucket, object_key)
        return {
            key.replace("opc-meta-", ""): value
            for key, value in head.headers.items()
            if key.lower().startswith("opc-meta-")
        }
