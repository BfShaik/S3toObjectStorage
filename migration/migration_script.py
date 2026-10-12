"""Plan and optionally copy S3 objects to OCI Object Storage.

This sample never writes lifecycle policies through the SDK. It produces a
Terraform input file for reviewed historical cohorts, so Terraform remains the
single owner of each OCI bucket lifecycle policy.
"""

import argparse
import csv
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import boto3
import oci
import oci.object_storage

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "classifications.json"
DEFAULT_INVENTORY = Path(__file__).with_name("s3_inventory.csv")
DEFAULT_COHORTS = ROOT / "terraform" / "migration_cohorts.auto.tfvars.json"


def load_classifications() -> dict:
    with CONFIG.open(encoding="utf-8") as source:
        return json.load(source)["classifications"]


def inventory(s3, source_bucket: str, output: Path) -> int:
    """Write source LastModified and classification tags to a reviewable CSV."""
    rows = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=source_bucket):
        for obj in page.get("Contents", []):
            tags = s3.get_object_tagging(Bucket=source_bucket, Key=obj["Key"]).get("TagSet", [])
            tag_map = {item["Key"]: item["Value"] for item in tags}
            rows.append({
                "source_key": obj["Key"],
                "source_last_modified": obj["LastModified"].astimezone(timezone.utc).isoformat(),
                "size_bytes": obj["Size"],
                "classification": tag_map.get("classification", ""),
            })
    with output.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=["source_key", "source_last_modified", "size_bytes", "classification"])
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def build_cohorts(inventory_file: Path, classifications: dict, output: Path) -> tuple[dict, list[str]]:
    """Create conservative month cohorts from source LastModified timestamps.

    Each cohort uses its most recently modified source object to avoid deleting
    any object earlier than the configured retention duration. This can retain
    some older objects longer; it does not claim to preserve per-object timing.
    """
    grouped = defaultdict(list)
    unknown = []
    with inventory_file.open(newline="", encoding="utf-8") as source:
        for row in csv.DictReader(source):
            classification = row["classification"]
            if classification not in classifications:
                unknown.append(row["source_key"])
                continue
            modified = datetime.fromisoformat(row["source_last_modified"].replace("Z", "+00:00"))
            grouped[(classification, modified.strftime("%Y-%m"))].append(modified)

    now = datetime.now(timezone.utc)
    cohorts = {}
    for (classification, month), dates in grouped.items():
        delete_days = classifications[classification].get("delete_days")
        if delete_days is None:
            continue
        newest_source_date = max(dates)
        remaining_days = delete_days - max(0, (now - newest_source_date).days)
        if remaining_days <= 0:
            # Do not generate an immediate-delete rule. Escalate this cohort for
            # retention-owner review instead.
            continue
        value = classifications[classification]
        cohorts[f"{classification}-{month}"] = {
            "bucket": value["bucket"],
            "prefix": f"migration/{value['prefix']}{month}/",
            "remaining_delete_days": remaining_days,
        }
    with output.open("w", encoding="utf-8") as target:
        json.dump({"migration_cohorts": cohorts}, target, indent=2)
        target.write("\n")
    return cohorts, unknown


def copy_objects(inventory_file: Path, classifications: dict, source_bucket: str, s3, oci_client, namespace: str) -> tuple[int, list[str]]:
    """Copy reviewed inventory rows. This function never deletes S3 objects."""
    copied, skipped = 0, []
    with inventory_file.open(newline="", encoding="utf-8") as source:
        for row in csv.DictReader(source):
            classification = row["classification"]
            if classification not in classifications:
                skipped.append(row["source_key"])
                continue
            route = classifications[classification]
            source_modified = datetime.fromisoformat(row["source_last_modified"].replace("Z", "+00:00"))
            destination_key = f"migration/{route['prefix']}{source_modified:%Y-%m}/{row['source_key']}"
            try:
                oci_client.head_object(namespace, route["bucket"], destination_key)
                continue  # idempotent resume
            except oci.exceptions.ServiceError as error:
                if error.status != 404:
                    raise
            response = s3.get_object(Bucket=source_bucket, Key=row["source_key"])
            oci_client.put_object(
                namespace_name=namespace,
                bucket_name=route["bucket"],
                object_name=destination_key,
                put_object_body=response["Body"].read(),
                opc_meta={
                    "classification": classification,
                    "source-last-modified": source_modified.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "migration-source": "s3",
                },
            )
            copied += 1
    return copied, skipped


def main() -> None:
    parser = argparse.ArgumentParser(description="Inventory, plan, and optionally copy S3 objects to OCI.")
    parser.add_argument("--source-bucket", required=True)
    parser.add_argument("--source-region", default="us-east-1")
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--cohorts", type=Path, default=DEFAULT_COHORTS)
    parser.add_argument("--copy", action="store_true", help="Copy reviewed inventory; never deletes source objects.")
    parser.add_argument("--confirm-copy", action="store_true", help="Required with --copy.")
    args = parser.parse_args()
    if args.copy and not args.confirm_copy:
        parser.error("--copy requires --confirm-copy after inventory and plan review")

    classifications = load_classifications()
    s3 = boto3.client("s3", region_name=args.source_region)
    count = inventory(s3, args.source_bucket, args.inventory)
    cohorts, unknown = build_cohorts(args.inventory, classifications, args.cohorts)
    print(f"Inventoried {count} objects; generated {len(cohorts)} cohort rules in {args.cohorts}.")
    if unknown:
        print(f"Skipped {len(unknown)} object(s) with an unknown or missing classification; review the inventory.")
    if not args.copy:
        print("Review the inventory and Terraform plan. Re-run with --copy --confirm-copy to copy; source objects are never deleted.")
        return

    cfg = oci.config.from_file(os.path.expanduser("~/.oci/config"))
    client = oci.object_storage.ObjectStorageClient(cfg)
    copied, skipped = copy_objects(args.inventory, classifications, args.source_bucket, s3, client, client.get_namespace().data)
    print(f"Copied {copied} object(s); skipped {len(skipped)} unclassified object(s).")


if __name__ == "__main__":
    main()
