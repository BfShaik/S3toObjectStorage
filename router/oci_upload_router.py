"""Route uploads to OCI Object Storage buckets and prefixes from shared JSON.

This sample intentionally performs routing only. OCI lifecycle policies are
managed by Terraform; object metadata is informational and does not affect
lifecycle evaluation.
"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Optional

import oci
import oci.object_storage

logger = logging.getLogger(__name__)
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config", "classifications.json")
_METADATA_KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,126}$")


def _load_routing_map(config_path: str = _CONFIG_PATH) -> dict[str, tuple[str, str]]:
    """Read and validate the classification-to-destination map."""
    with open(os.path.normpath(config_path), encoding="utf-8") as config_file:
        data = json.load(config_file)
    classifications = data.get("classifications")
    if not isinstance(classifications, dict) or not classifications:
        raise ValueError("classifications.json must contain a non-empty classifications object")

    routes: dict[str, tuple[str, str]] = {}
    for name, value in classifications.items():
        if not isinstance(value, dict):
            raise ValueError(f"Classification {name!r} must be an object")
        bucket, prefix = value.get("bucket"), value.get("prefix")
        if not isinstance(bucket, str) or not bucket:
            raise ValueError(f"Classification {name!r} requires a bucket")
        if not isinstance(prefix, str) or not prefix or not prefix.endswith("/"):
            raise ValueError(f"Classification {name!r} requires a non-empty prefix ending in '/'")
        routes[name] = (bucket, prefix)
    return routes


def _validate_object_name(object_name: str) -> str:
    """Accept a relative Object Storage key and prevent unintended prefix use."""
    if not isinstance(object_name, str) or not object_name or "\x00" in object_name:
        raise ValueError("object_name must be a non-empty string without NUL characters")
    if object_name.startswith("/") or any(part in {"", ".", ".."} for part in object_name.split("/")):
        raise ValueError("object_name must be a relative key without empty, '.' or '..' path segments")
    return object_name


def _metadata(source_last_modified: Optional[datetime], extra_metadata: Optional[dict[str, str]]) -> dict[str, str]:
    now = datetime.now(timezone.utc)
    if source_last_modified and source_last_modified.tzinfo is None:
        raise ValueError("source_last_modified must include a timezone")
    metadata = {
        "upload-timestamp": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source-last-modified": (
            source_last_modified.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if source_last_modified else now.strftime("%Y-%m-%dT%H:%M:%SZ")
        ),
    }
    for key, value in (extra_metadata or {}).items():
        if not isinstance(key, str) or not _METADATA_KEY.fullmatch(key):
            raise ValueError(f"Invalid metadata key: {key!r}")
        if not isinstance(value, str):
            raise ValueError(f"Metadata value for {key!r} must be a string")
        metadata[key] = value
    return metadata


class OCIUploadRouter:
    """Small, testable router that delegates lifecycle enforcement to OCI."""

    def __init__(
        self,
        config_path: str = "~/.oci/config",
        profile: str = "DEFAULT",
        classifications_path: str = _CONFIG_PATH,
        client: Any = None,
        namespace: Optional[str] = None,
    ) -> None:
        self._routing_map = _load_routing_map(classifications_path)
        if client is None:
            oci_config = oci.config.from_file(os.path.expanduser(config_path), profile)
            client = oci.object_storage.ObjectStorageClient(oci_config)
        self._client = client
        self._namespace = namespace or self._client.get_namespace().data

    @property
    def valid_classifications(self) -> list[str]:
        return sorted(self._routing_map)

    def upload(
        self,
        object_name: str,
        body: bytes,
        classification: str,
        content_type: str = "application/octet-stream",
        extra_metadata: Optional[dict[str, str]] = None,
        source_last_modified: Optional[datetime] = None,
    ) -> dict[str, str]:
        """Upload below the configured prefix; source metadata never drives lifecycle."""
        if classification not in self._routing_map:
            raise ValueError("Unknown classification")
        if not isinstance(body, bytes):
            raise ValueError("body must be bytes")
        relative_key = _validate_object_name(object_name)
        bucket, prefix = self._routing_map[classification]
        full_key = f"{prefix}{relative_key}"
        metadata = _metadata(source_last_modified, extra_metadata)
        metadata["classification"] = classification

        self._client.put_object(
            namespace_name=self._namespace,
            bucket_name=bucket,
            object_name=full_key,
            put_object_body=body,
            content_type=content_type,
            opc_meta=metadata,
        )
        logger.info("Uploaded object to bucket=%s classification=%s", bucket, classification)
        return {"bucket": bucket, "key": full_key, "namespace": self._namespace, "classification": classification}
