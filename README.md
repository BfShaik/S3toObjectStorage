# OCI Object Storage Migration — S3 Lifecycle Solution

AWS S3 → OCI Object Storage migration with native OCI lifecycle management.
Zero custom lifecycle code. Oracle enforces all archive and delete rules.

---

## The One-File Rule

**There is exactly one file that ever changes:**

```
config/classifications.json
```

Both Terraform (infrastructure) and the Python upload router (application)
read directly from this file. They cannot get out of sync because there is
only one place where bucket names, prefixes, and retention rules live.

**To add a new data classification:**
1. Add one JSON block to `config/classifications.json`
2. Run `terraform apply`
3. Done — the router picks it up on next start automatically

---

## Repository Structure

```
oci-migration/
│
├── config/
│   └── classifications.json        ← THE ONLY FILE THAT EVER CHANGES
│                                     Read by: Terraform + Python router
│
├── terraform/
│   ├── main.tf                     ← reads classifications.json, never changes
│   ├── variables.tf                ← OCI env variables only
│   └── terraform.tfvars.template   ← copy → terraform.tfvars, fill in values
│
├── router/
│   └── oci_upload_router.py        ← reads classifications.json at startup
│
├── migration/
│   └── migration_script.py         ← one-time cutover, reads classifications.json
│
└── README.md
```

---

## Quick Start

### 1. Fill in OCI values

```bash
cd terraform
cp terraform.tfvars.template terraform.tfvars
# Edit terraform.tfvars — add compartment_id, namespace, region
```

### 2. Deploy infrastructure

```bash
terraform init
terraform plan    # review — should show 4 buckets + lifecycle rules
terraform apply
```

### 3. Use the router in your application

```python
from router.oci_upload_router import OCIUploadRouter

router = OCIUploadRouter()

router.upload(
    object_name    = "customer-9821-profile.json",
    body           = file_bytes,
    classification = "pii-customers",
)
# → lands in: bucket-pii-prod/customers/customer-9821-profile.json
# → OCI archives after 90 days, deletes after 7 years — automatically
```

### 4. Run migration (once, during cutover)

```bash
cd migration
# Edit S3_SOURCE_BUCKET and S3_REGION at top of migration_script.py
# Run dry run first:
#   set DRY_RUN = True, then:
python migration_script.py
# Review migration.log, then set DRY_RUN = False and run again
```

---

## Bucket Architecture

| Bucket | Classification Keys | Archive | Delete |
|---|---|---|---|
| `bucket-pii-prod` | pii-customers, pii-employees, pii-financial, pii-health | 30–90 days | 7–15 years |
| `bucket-compliance-prod` | compliance-sox, compliance-gdpr, compliance-contracts, compliance-audit | 7–30 days | 7–10 years |
| `bucket-temp-processing` | temp-raw, temp-processing, temp-staging | None | 1–7 days |
| `bucket-logs-prod` | log-application, log-access, log-security | 7–30 days | 30–365 days |

---

## What Oracle Manages vs Your Team

| Oracle Manages (zero effort) | Your Team Owns |
|---|---|
| Daily lifecycle evaluation | `classifications.json` — edit when rules change |
| Archive tier transitions | `terraform apply` — after each config change |
| Permanent deletion on schedule | `oci_upload_router.py` — stable after initial build |
| Retry logic and scaling | One-time migration script during cutover |
| Full audit trail | — |
| SLA coverage | — |

---

## Dependencies

```bash
# Python
pip install oci boto3

# Terraform
terraform >= 1.3.0
OCI Terraform provider >= 5.0.0
```

---

## Security Notes

- `terraform.tfvars` contains OCID values — **never commit, already in .gitignore**
- `migration_script.py` is a one-time script — **delete after cutover validation**
- Migration script is resumable — re-running with `SKIP_EXISTING = True` skips
  objects already copied to OCI
