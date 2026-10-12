# S3 to OCI Object Storage lifecycle mapping sample

This community sample illustrates a bucket-and-prefix design for routing objects
to OCI Object Storage lifecycle domains. It is not an Oracle product and is not
supported by Oracle. Review, test, and adapt it for your own security,
retention, licensing, and organizational approval requirements.

## What it does

- `config/classifications.json` is shared by Terraform and the Python upload router.
- Terraform creates buckets and lifecycle rules that scope objects by prefix.
- The router validates a classification and uploads under its configured prefix.
- The migration script inventories S3 objects, produces a reviewable historical
  cohort plan, and can copy approved inventory rows without deleting S3 data.

OCI evaluates lifecycle timing from the destination object's Last-Modified time.
Metadata such as `source-last-modified` is informational; it does not alter that
calculation. Historical cohorts can avoid deleting objects earlier than their
configured retention duration, but a cohort is an approximation and may retain
some older objects longer. Validate the plan with your retention owner.

## Safety model

Deletion is disabled by default. A DELETE rule is created only when both:

1. `delete_days` is configured for the classification or generated cohort; and
2. `enable_delete = true` is supplied to Terraform after reviewing its plan.

The migration tool never deletes source S3 objects and never writes OCI lifecycle
policies through the SDK. Terraform is the sole lifecycle-policy writer, which
prevents a later `terraform apply` from overwriting migration rules.

## Quick start

```bash
cd terraform
cp terraform.tfvars.template terraform.tfvars
# Set compartment_id, namespace, and region. Keep enable_delete = false.
terraform init
terraform fmt -check
terraform validate
terraform plan
```

Use the router with a neutral sample classification:

```python
from router.oci_upload_router import OCIUploadRouter

router = OCIUploadRouter()
router.upload(
    object_name="2026-10/service.log",
    body=b"example",
    classification="application-logs",
)
```

## Historical migration workflow

1. Inventory and plan only:

   ```bash
   python migration/migration_script.py --source-bucket YOUR_BUCKET
   ```

2. Review `migration/s3_inventory.csv` and
   `terraform/migration_cohorts.auto.tfvars.json`. Resolve rows without a known
   classification and obtain retention-owner approval.
3. Run `terraform plan` with `enable_delete = false`. If deletion is authorized,
   change it to `true`, review the new plan, and apply only through your approved
   change process.
4. Copy the approved inventory without deleting S3 data:

   ```bash
   python migration/migration_script.py --source-bucket YOUR_BUCKET --copy --confirm-copy
   ```

5. Reconcile object counts and checksums before any separate source-decommission
   decision.

## Prerequisites and security

- Python packages: `oci` and `boto3`.
- Terraform 1.3+ and the OCI Terraform provider.
- OCI IAM permissions for the deployment identity and, when lifecycle actions
  are enabled, permissions for the Object Storage service to perform them.
- Do not commit OCI credentials, inventory files, Terraform state, or generated
  migration cohort plans.

This sample uses the OCI SDK configuration file for simplicity. For production,
use an approved workload identity or credential-management pattern and apply
least-privilege IAM policies.
