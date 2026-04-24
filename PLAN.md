# OLT Authorization Workflow: Gap Analysis Status

## Summary

Original analysis identified 15 gaps. **All 15 are now resolved.** ✅

---

## Resolved Gaps

### Critical (Data Loss / Inconsistent State)

#### Gap 1: Race Condition in Autofind Candidate Resolution ✅
**Fixed in:** `app/services/web_network_ont_autofind.py:359`
```python
.with_for_update(skip_locked=True)
```

#### Gap 2: Orphaned Service Port Allocations on Return-to-Inventory ✅
**Fixed in:** `app/services/web_network_ont_actions/inventory.py:100-102, 177-179`
```python
from app.services.network.service_port_allocator import release_all_for_ont
released_allocations = release_all_for_ont(db, ont_id)
```

#### Gap 3: Missing Rollback on Partial Return-to-Inventory Failure ✅
**Fixed in:** `app/services/web_network_ont_actions/inventory.py:232`
```python
with db.begin_nested():  # Savepoint
```

#### Gap 4: Multiple db.commit() in Authorization ✅
**Fixed in:** `app/services/network/olt_authorization_workflow.py`
- Internal calls use `db.flush()` (lines 87, 259, 337, 352, 358, 614, 2529, 2590)
- Remaining commits are intentional transaction boundaries

---

### High (Operational Impact)

#### Gap 5: Missing Locking on OntAssignment During Return-to-Inventory ✅
**Fixed in:** `app/services/web_network_ont_actions/inventory.py:241`
```python
.with_for_update()  # Lock to prevent concurrent modifications
```

#### Gap 6: ACS Profile Not Cleaned on Return-to-Inventory ✅
**Fixed in:** `app/services/web_network_ont_actions/inventory.py:212-227`
```python
tr069_device.ont_unit_id = None  # Clear association
```

#### Gap 7: Incomplete Service Port Rollback on ACS Bind Failure ✅
**Fixed in:** `app/services/network/olt_authorization_workflow.py:651-757`
- Added `_cleanup_mgmt_service_port_on_bind_failure()` function

#### Gap 8: Blocking Sleep in Authorization Retry Loop ✅
**Fixed in:** `app/services/network/olt_authorization_workflow.py:775, 863-899`
```python
use_async_rediscovery: bool = True  # Default to async
# Queues Celery task instead of blocking
queued_ok, task_id_or_error = queue_rediscovery_poll(...)
```

---

### Medium (Audit/Operational Visibility)

#### Gap 9: Autofind Sync Commits Unconditionally ✅
**Fixed in:** `app/services/web_network_ont_autofind.py`
- Uses `db.flush()` (lines 114, 330, 370) instead of `commit()`

#### Gap 10: Missing OLT Connectivity Pre-check ✅
**Fixed in:** `app/services/network/olt_authorization_workflow.py:1267-1293`
```python
ssh_ok, ssh_msg = test_reachability(olt, timeout_sec=10)
if not ssh_ok:
    return _fail("Verify OLT connectivity", f"Cannot reach OLT {olt.name} via SSH: {ssh_msg}")
```

#### Gap 11: Assignment History Incomplete ✅
**Fixed in:**
- `app/models/network.py:1669, 3058` - Added `released_at` field
- `app/services/web_network_ont_actions/inventory.py:246-247`:
```python
active_assignment.released_at = datetime.now(UTC)
active_assignment.release_reason = "returned_to_inventory"
```

#### Gap 12: Missing Event for ONT Bundle Unassignment ✅
**Fixed in:** `app/services/network/ont_inventory.py:73, 171`
```python
emit_event(db, EventType.ont_bundle_unassigned, {...})
```

---

### Low (Minor Issues)

#### Gap 13: Concurrent Autofind Refresh Causes Redundant OLT Queries ✅
**Fixed in:** `app/services/web_network_ont_autofind.py:91-101`
```python
# Deduplication: skip if recently synced (Gap 13 fix)
if skip_if_recent_seconds > 0 and olt.autofind_last_sync_at:
    age_seconds = (datetime.now(UTC) - olt.autofind_last_sync_at).total_seconds()
    if age_seconds < skip_if_recent_seconds:
        return True, f"Using cached autofind (synced {int(age_seconds)}s ago)", {}
```
Called with `skip_if_recent_seconds=5` at line 828 in authorization workflow.

#### Gap 14: Hard Delete Not Supported ✅
**Fixed in:** `app/services/network/ont_decommission.py`
- New module with `preview_decommission()` and `decommission_ont()` functions

#### Gap 15: Status Transition Logging Only on Violation ✅
**Fixed in:** `app/services/network/ont_status_transitions.py:121-128, 156-163`
```python
logger.info("ont_status_transition", extra={
    "event": "ont_status_transition",
    "ont_id": str(ont.id),
    "from": current.value if current else None,
    "to": next_status.value,
})
```

---

## TR-069 Layer Status

Deep code review confirms the TR-069/ACS layer is production-ready with:

| Feature | Status |
|---------|--------|
| Task deduplication | ✅ Prevents duplicate setParameterValues |
| Stale task cleanup | ✅ 30-min TTL auto-cleanup |
| Inform-safe mode | ✅ Blocks reads when pending tasks exist |
| Max pending limit | ✅ Default 20 tasks per device |
| Dual data model | ✅ TR-181 and TR-098 support |
| WAN instance mgmt | ✅ addObject for factory-fresh ONTs |
| Background tasks | ✅ Celery-based async operations |

No gaps identified in TR-069 layer.
