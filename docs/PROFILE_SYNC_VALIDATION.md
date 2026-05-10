# Profile Sync Operational Validation Runbook

This document guides you through validating the profile sync subsystem against real OLTs.

## Prerequisites

- Docker services running (`docker compose ps`)
- Active OLTs with SSH credentials configured
- At least one catalog offer with OLT profile mappings
- Access to admin UI at `/admin/network/profile-sync-tasks`

## Quick Start (End-to-End Test)

If no profile bundles exist, follow these steps to create test data:

### 1. Create a Profile Bundle (Required First)

```bash
# Navigate to an OLT's profile page in the browser:
# /admin/network/olts/{OLT_ID}/profiles

# Or via CLI:
docker exec dotmac_sub_app python -c "
from app.db import SessionLocal
from app.models.network import OLTDevice
from app.models.catalog import CatalogOffer
from app.services import web_network_olt_profiles as profile_service
from sqlalchemy import select

db = SessionLocal()

# Get first OLT and offer
olt = db.scalar(select(OLTDevice).where(OLTDevice.is_active.is_(True)).limit(1))
offer = db.scalar(select(CatalogOffer).where(CatalogOffer.is_active.is_(True)).limit(1))

if olt and offer:
    result = profile_service.save_offer_profile_bundle(
        db,
        str(olt.id),
        offer_id=str(offer.id),
        vlan_id=100,  # Adjust as needed
    )
    print('Result:', result.get('ok'), result.get('message'))
else:
    print('No OLT or offer found')
db.close()
"
```

### 2. Run Drift Check

After creating a bundle, the drift check will compare it to live OLT state:
- Via UI: `/admin/network/profile-sync-tasks` → Click "Check Drift"
- Via CLI: See Step 1 below

### 3. Create and Execute a Sync Task

Create a sync task to observe the celery-beat → tr069 workflow:
- Via UI: Navigate to the profile sync tasks page and approve/schedule a task
- Via CLI: See Step 2 below

## Current System Status

```bash
# Check scheduler is enabled
docker exec dotmac_sub_app python -c "
from app.db import SessionLocal
from sqlalchemy import text
db = SessionLocal()
result = db.execute(text(\"\"\"
SELECT name, enabled, interval_seconds
FROM scheduled_tasks
WHERE task_name LIKE '%profile_sync%'
\"\"\")).fetchall()
print('Scheduled Tasks:', result)
db.close()
"
```

Expected output:
```
Scheduled Tasks: [('olt_profile_sync_due_task_runner', True, 300)]
```

---

## Step 1: Run Profile Bundle Drift Check Against Real OLTs

### Option A: Via Web UI (Recommended)

1. Navigate to `/admin/network/profile-sync-tasks`
2. Look for the "Bundle Drift" section
3. Click **"Check Drift"** button
4. Observe the results in the "Recent Profile Sync History" section

### Option B: Via CLI

```bash
# Run drift check
docker exec dotmac_sub_app python -c "
from app.db import SessionLocal
from app.services import web_network_olt_profiles as profile_service

db = SessionLocal()
ok, message = profile_service.check_profile_bundle_drift(
    db,
    checked_by='cli-validation',
    request=None,
    limit=10,
)
print('Success:', ok)
print('Message:', message)
db.close()
"
```

### What This Validates

- SSH connectivity to each OLT
- Profile parsing (DBA, traffic tables, line profiles, service profiles)
- Bundle inventory comparison (expected vs actual)

### Expected Outcomes

| Status | Meaning |
|--------|---------|
| `in_sync` | All profile IDs and names match |
| `drifted` | Profiles missing or names don't match |
| `drift_unknown` | SSH failed or parsing error |

---

## Step 2: Observe Approved/Scheduled Profile Sync Through Celery-Beat

### 2.1 Create or Approve a Sync Task

**Via Web UI:**
1. Go to `/admin/network/profile-sync-tasks`
2. Find a task with status "Pending"
3. Either:
   - Click **"Approve"** for immediate execution eligibility
   - Enter a datetime and click **"Schedule"** for delayed execution

**Or create programmatically:**
```bash
docker exec dotmac_sub_app python -c "
from datetime import datetime, timedelta, UTC
from app.db import SessionLocal
from app.models.network import OLTDevice, OltProfileSyncTask
from app.models.catalog import CatalogOffer
from sqlalchemy import select

db = SessionLocal()

# Get first OLT and offer
olt = db.scalar(select(OLTDevice).where(OLTDevice.is_active.is_(True)).limit(1))
offer = db.scalar(select(CatalogOffer).where(CatalogOffer.is_active.is_(True)).limit(1))

if olt and offer:
    task = OltProfileSyncTask(
        olt_id=olt.id,
        offer_id=offer.id,
        status='scheduled',
        trigger='cli_validation',
        requested_by='cli-validation',
        approved_by='cli-validation',
        scheduled_for=datetime.now(UTC) + timedelta(minutes=2),
        preview_payload={'test': True},
    )
    db.add(task)
    db.commit()
    print(f'Created task {task.id} scheduled for {task.scheduled_for}')
else:
    print('No OLT or offer found')
db.close()
"
```

### 2.2 Watch Celery-Beat Pick Up the Task

```bash
# Watch celery-beat logs (runs every 300 seconds by default)
docker logs -f dotmac_sub_celery_beat 2>&1 | grep -i profile

# Watch the tr069 worker for task execution
docker logs -f dotmac_sub_celery_worker_tr069 2>&1 | grep -i profile
```

### 2.3 Manually Trigger Due Tasks (Optional)

If you don't want to wait for the scheduler:

**Via Web UI:**
1. Go to `/admin/network/profile-sync-tasks`
2. Click **"Execute Due Tasks"** button

**Via CLI:**
```bash
docker exec dotmac_sub_app python -c "
from app.db import SessionLocal
from app.services import web_network_olt_profiles as profile_service

db = SessionLocal()
result = profile_service.execute_due_profile_sync_tasks(
    db,
    executed_by='cli-validation',
    actor_is_admin=True,
    limit=5,
)
print('Result:', result)
db.close()
"
```

### What This Validates

- Celery-beat schedule configuration
- Task state machine (pending → approved/scheduled → running → completed/failed)
- OLT SSH command execution
- Profile application workflow

---

## Step 3: Verify Audit/History Output

### 3.1 Check Recent Audit Events

```bash
docker exec dotmac_sub_app python -c "
from app.db import SessionLocal
from app.models.audit import AuditEvent
from sqlalchemy import select

db = SessionLocal()
events = db.scalars(
    select(AuditEvent)
    .where(AuditEvent.entity_type.in_(['olt_profile_sync_task', 'olt_profile_bundle']))
    .order_by(AuditEvent.occurred_at.desc())
    .limit(10)
).all()

print(f'Found {len(events)} audit events:')
for e in events:
    status = 'OK' if e.is_success else 'FAIL'
    print(f'  [{status}] {e.action} by {e.actor_id} at {e.occurred_at}')
    if e.metadata_:
        for k, v in list(e.metadata_.items())[:3]:
            print(f'       {k}: {v}')
db.close()
"
```

### 3.2 View in Web UI

1. Go to `/admin/network/profile-sync-tasks`
2. Look at the **"Recent Profile Sync History"** section
3. Each event shows:
   - Action name (e.g., "Olt Profile Bundle Drift Checked")
   - Success/failure status
   - Timestamp and actor

### Expected Audit Actions

| Action | When Logged |
|--------|------------|
| `olt_profile_bundle_drift_checked` | After drift check completes |
| `olt_profile_sync_task_approved` | When task is approved |
| `olt_profile_sync_task_scheduled` | When task is scheduled |
| `olt_profile_sync_task_completed` | When task executes successfully |
| `olt_profile_sync_task_failed` | When task execution fails |
| `olt_profile_sync_task_cancelled` | When task is cancelled |
| `olt_profile_sync_task_retried` | When failed task is retried |

### Audit Event Structure

Each audit event contains:
- `entity_type`: "olt_profile_sync_task" or "olt_profile_bundle"
- `entity_id`: UUID of the task or bundle
- `action`: Specific action performed
- `actor_id`: User or system that performed the action
- `is_success`: Boolean success flag
- `metadata_`: JSON with action-specific details:
  - For drift checks: `drift_status`, `missing`, `mismatched`
  - For task operations: `status`, `trigger`, `olt_id`, `offer_id`

---

## Troubleshooting

### No Profile Bundles

Profile bundles are created when a catalog offer's profile mapping is applied to an OLT. To create bundles:

1. Ensure catalog offers have OLT profile mappings configured
2. Navigate to an offer's detail page
3. Check the "OLT Profiles" section
4. Bundles are created when profiles are synced

### Drift Check Shows "drift_unknown"

This usually means SSH connection failed. Check:
```bash
# Verify OLT SSH credentials are set
docker exec dotmac_sub_app python -c "
from app.db import SessionLocal
from app.models.network import OLTDevice
from sqlalchemy import select

db = SessionLocal()
olts = db.scalars(select(OLTDevice).where(OLTDevice.is_active.is_(True))).all()
for olt in olts[:5]:
    ssh_ok = bool(olt.ssh_username and olt.ssh_password)
    print(f'{olt.name}: SSH configured={ssh_ok}')
db.close()
"

# Test SSH connectivity manually
docker exec dotmac_sub_app python -c "
from app.services.network import olt_ssh_profiles
from app.db import SessionLocal
from app.models.network import OLTDevice
from sqlalchemy import select

db = SessionLocal()
olt = db.scalar(select(OLTDevice).where(OLTDevice.is_active.is_(True)).limit(1))
if olt:
    ok, msg, profiles = olt_ssh_profiles.get_dba_profiles(olt)
    print(f'SSH test to {olt.name}: ok={ok}, msg={msg}')
    if profiles:
        print(f'  Found {len(profiles)} DBA profiles')
db.close()
"
```

### Celery Worker Not Processing Tasks

Check worker health:
```bash
docker logs dotmac_sub_celery_worker_tr069 --tail 50

# Verify the worker is connected to Redis
docker exec dotmac_sub_celery_worker_tr069 celery -A app.celery_app inspect active_queues
```

### Tasks Stay in "Scheduled" Status

The worker only processes tasks where `scheduled_for <= now()`. Check:
- Is the scheduled time in the past?
- Is the celery-beat scheduler running?
- Is the profile sync worker enabled in settings?

---

## Validation Checklist

- [ ] Profile bundles exist for at least one OLT+offer combination
- [ ] Drift check runs successfully and returns `in_sync` or `drifted`
- [ ] SSH connection works to at least one OLT
- [ ] Celery-beat scheduler is enabled and running every 300s
- [ ] TR-069 worker is processing tasks
- [ ] Audit events are logged for all operations
- [ ] UI shows audit history in "Recent Profile Sync History"

---

## Files Reference

| File | Purpose |
|------|---------|
| `app/services/web_network_olt_profiles.py` | Drift check and sync task logic |
| `app/tasks/profile_sync.py` | Celery task wrapper |
| `app/services/scheduler_config.py` | Celery-beat schedule |
| `app/web/admin/network_olts_profiles.py` | Web routes |
| `templates/admin/network/olts/profile_sync_tasks.html` | UI template |
