# OCI Object Storage Migration — S3 Lifecycle Solution

AWS S3 → OCI Object Storage migration with native OCI lifecycle management.  
Zero custom lifecycle code. Oracle enforces all archive and delete rules.

---

## Repository Structure

```
oci-migration/
├── terraform/
│   ├── main.tf                          # Core Terraform — never changes
│   ├── variables.tf                     # Input variable declarations
│   ├── lifecycle_config.auto.tfvars     # ← EDIT THIS to add classifications
│   └── terraform.tfvars.template        # Fill in your OCI values
│
├── router/
│   ├── router_config.py                 # ← EDIT THIS to match tfvars
│   └── oci_upload_router.py             # Production upload router class
│
├── migration/
│   └── migration_script.py              # One-time S3 → OCI migration
│
└── README.md
```

---

## The Two-File Rule

When adding a new data classification, edit **exactly two files**:

| File | Change |
|---|---|
| `terraform/lifecycle_config.auto.tfvars` | Add one line under the correct bucket's `prefixes` block |
| `router/router_config.py` | Add one matching line to `ROUTING_MAP` |

Then run `terraform apply`. No code changes. No developer needed.

---

## Quick Start

### 1. Infrastructure Setup (Week 1–2)

```bash
cd terraform

# Copy and fill in your OCI values
cp terraform.tfvars.template terraform.tfvars
# Edit terraform.tfvars with your compartment_id, namespace, region

# Deploy all buckets and lifecycle policies
terraform init
terraform plan    # review what will be created
terraform apply   # creates 4 buckets + all lifecycle rules
```

### 2. Application Integration (Week 2–3)

```python
from router.oci_upload_router import OCIUploadRouter

router = OCIUploadRouter()

# Upload a PII customer file
router.upload(
    object_name    = "customer-12345-profile.json",
    body           = file_bytes,
    classification = "pii-customers",
)
# → lands in: bucket-pii-prod/customers/customer-12345-profile.json
# → OCI archives after 90 days automatically
# → OCI deletes after 7 years automatically
```

### 3. Migration Cutover (Week 3–4)

```bash
cd migration

# Edit configuration at the top of migration_script.py
# Set: S3_SOURCE_BUCKET, S3_REGION, DRY_RUN = True first

# Dry run — review output before actual migration
python migration_script.py

# Real run
# Set DRY_RUN = False, then:
python migration_script.py
```

---

## Bucket Architecture

| Bucket | Classification | Archive After | Delete After |
|---|---|---|---|
| `bucket-pii-prod` | PII data | 30–90 days | 7–15 years |
| `bucket-compliance-prod` | Regulatory / Legal | 7–30 days | 7–10 years |
| `bucket-temp-processing` | Temp / pipeline | Never | 1–7 days |
| `bucket-logs-prod` | Logs | 7–30 days | 30–365 days |

---

## What Oracle Manages vs Your Team

**Oracle manages (zero effort after setup):**
- Daily lifecycle evaluation on every object
- Standard → Archive tier transitions
- Permanent deletion on schedule
- Retry logic, scaling, SLA coverage

**Your team owns (minimal):**
- `lifecycle_config.auto.tfvars` — edit when classifications change
- `router_config.py` — keep in sync with tfvars
- `oci_upload_router.py` — stable after initial build, ~80 lines

---

## Dependencies

```bash
# Python
pip install oci boto3

# Terraform
terraform >= 1.3.0
OCI provider >= 5.0.0
```

---

## Important Notes

- `terraform.tfvars` contains sensitive OCID values — **do not commit to source control**
- `migration_script.py` is a one-time use script — delete after cutover validation
- The migration script is resumable — re-running skips objects already in OCI
- Always run with `DRY_RUN = True` first and review `migration.log` before the real run
- Monitor OCI lifecycle rules for the first 30 days after migration cutover
