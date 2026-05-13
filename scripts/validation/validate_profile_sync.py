#!/usr/bin/env python3
"""
Live Operational Validation for Profile Sync

This script validates the profile sync subsystem by:
1. Running profile bundle drift check against real OLTs
2. Creating and observing a profile sync task through celery-beat -> tr069 queue
3. Verifying audit/history output from the runs

Usage:
    docker exec dotmac_sub_app python scripts/validation/validate_profile_sync.py --step drift-check
    docker exec dotmac_sub_app python scripts/validation/validate_profile_sync.py --step create-task
    docker exec dotmac_sub_app python scripts/validation/validate_profile_sync.py --step verify-audit
    docker exec dotmac_sub_app python scripts/validation/validate_profile_sync.py --all
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text

from app.db import SessionLocal
from app.models.audit import AuditEvent
from app.models.catalog import CatalogOffer
from app.models.network import (
    OLTDevice,
    OltProfileBundle,
    OltProfileSyncTask,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def step_1_drift_check(db, *, dry_run: bool = False) -> bool:
    """Step 1: Run profile bundle drift check against real OLTs.

    This validates SSH connectivity and profile parsing.
    """
    logger.info("=" * 60)
    logger.info("STEP 1: Profile Bundle Drift Check")
    logger.info("=" * 60)

    # Check for active bundles
    bundles = list(
        db.scalars(
            select(OltProfileBundle)
            .where(OltProfileBundle.is_active.is_(True))
            .limit(10)
        )
    )

    if not bundles:
        logger.warning("No active profile bundles found.")
        logger.info("Creating a test bundle requires:")
        logger.info("  1. A catalog offer with OLT profile mapping")
        logger.info("  2. Running: POST /admin/network/profile-sync-tasks/create")
        logger.info("  OR: Navigate to Admin > Network > Profile Sync Tasks")

        # Show available OLTs for manual bundle creation
        olts = list(
            db.scalars(
                select(OLTDevice)
                .where(OLTDevice.is_active.is_(True))
                .where(OLTDevice.ssh_username.isnot(None))
                .limit(5)
            )
        )
        if olts:
            logger.info("\nAvailable OLTs with SSH:")
            for olt in olts:
                logger.info(f"  - {olt.name} ({olt.mgmt_ip})")
        return False

    logger.info(f"Found {len(bundles)} active profile bundle(s):")
    for b in bundles:
        olt_name = b.olt.name if b.olt else "no-olt"
        logger.info(f"  - {b.name} on {olt_name} (drift: {b.drift_status})")

    if dry_run:
        logger.info("\n[DRY RUN] Would run drift check on these bundles")
        return True

    # Import the drift check function
    from app.services import web_network_olt_profiles as profile_service

    logger.info("\nRunning drift check...")
    ok, message = profile_service.check_profile_bundle_drift(
        db,
        checked_by="validation-script",
        request=None,
        limit=10,
    )

    if ok:
        logger.info(f"SUCCESS: {message}")
    else:
        logger.warning(f"ISSUES FOUND: {message}")

    # Show updated drift status
    db.expire_all()
    bundles = list(
        db.scalars(
            select(OltProfileBundle)
            .where(OltProfileBundle.is_active.is_(True))
            .limit(10)
        )
    )
    logger.info("\nUpdated drift status:")
    for b in bundles:
        olt_name = b.olt.name if b.olt else "no-olt"
        details = b.drift_details or {}
        logger.info(f"  - {b.name}: {b.drift_status}")
        if b.drift_status == "drifted":
            if details.get("missing"):
                logger.info(f"    Missing: {details['missing']}")
            if details.get("mismatched"):
                logger.info(f"    Mismatched: {details['mismatched']}")
        elif b.drift_status == "drift_unknown":
            logger.info(f"    Error: {details.get('message', 'unknown')}")

    return ok


def step_2_create_task(db, *, dry_run: bool = False) -> bool:
    """Step 2: Create a profile sync task for celery-beat observation.

    This creates a scheduled task that will be picked up by the worker.
    """
    logger.info("=" * 60)
    logger.info("STEP 2: Create Profile Sync Task")
    logger.info("=" * 60)

    # Find an OLT with SSH
    olt = db.scalar(
        select(OLTDevice)
        .where(OLTDevice.is_active.is_(True))
        .where(OLTDevice.ssh_username.isnot(None))
        .limit(1)
    )
    if not olt:
        logger.error("No OLT with SSH credentials found")
        return False

    # Find an offer
    offer = db.scalar(
        select(CatalogOffer)
        .where(CatalogOffer.is_active.is_(True))
        .where(CatalogOffer.offer_type == "primary")
        .limit(1)
    )
    if not offer:
        logger.error("No primary catalog offer found")
        return False

    logger.info(f"Selected OLT: {olt.name} ({olt.mgmt_ip})")
    logger.info(f"Selected Offer: {offer.name} ({offer.code})")

    # Check for existing pending/approved tasks
    existing = db.scalar(
        select(OltProfileSyncTask)
        .where(OltProfileSyncTask.olt_id == olt.id)
        .where(OltProfileSyncTask.offer_id == offer.id)
        .where(OltProfileSyncTask.status.in_(["pending", "approved", "scheduled"]))
    )
    if existing:
        logger.info(f"Existing task found: {existing.id} ({existing.status})")
        logger.info("To observe the workflow, approve this task from the UI")
        logger.info("  URL: /admin/network/profile-sync-tasks")
        return True

    if dry_run:
        logger.info("\n[DRY RUN] Would create a sync task for observation")
        return True

    # Create the task

    # First, we need to create a profile bundle for this OLT+offer
    # The bundle defines what profiles should exist
    logger.info("\nCreating profile sync task...")

    task = OltProfileSyncTask(
        olt_id=olt.id,
        offer_id=offer.id,
        status="pending",
        trigger="validation_script",
        requested_by="validation-script",
        preview_payload={
            "olt_name": olt.name,
            "offer_name": offer.name,
            "offer_code": offer.code,
            "validation_test": True,
        },
    )
    db.add(task)
    db.flush()
    logger.info(f"Created task: {task.id}")
    logger.info(f"Status: {task.status}")

    # Schedule it for 2 minutes from now
    scheduled_for = datetime.now(UTC) + timedelta(minutes=2)
    task.status = "scheduled"
    task.approved_by = "validation-script"
    task.scheduled_for = scheduled_for
    db.commit()

    logger.info(f"Scheduled for: {scheduled_for.isoformat()}")
    logger.info("\nNext steps:")
    logger.info("  1. Watch celery-beat logs: docker logs -f dotmac_sub_celery_beat")
    logger.info(
        "  2. Watch tr069 worker: docker logs -f dotmac_sub_celery_worker_tr069"
    )
    logger.info("  3. Check audit events: --step verify-audit")
    logger.info("  4. Or view in UI: /admin/network/profile-sync-tasks")

    return True


def step_3_verify_audit(db) -> bool:
    """Step 3: Verify audit/history output from profile sync runs."""
    logger.info("=" * 60)
    logger.info("STEP 3: Verify Audit/History Output")
    logger.info("=" * 60)

    # Get recent audit events
    events = list(
        db.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.entity_type.in_(
                    ("olt_profile_sync_task", "olt_profile_bundle")
                )
            )
            .order_by(AuditEvent.occurred_at.desc())
            .limit(20)
        )
    )

    if not events:
        logger.warning("No profile sync audit events found")
        logger.info("Run drift check or execute a sync task first")
        return False

    logger.info(f"Found {len(events)} audit event(s):\n")

    # Group by type
    bundle_events = [e for e in events if e.entity_type == "olt_profile_bundle"]
    task_events = [e for e in events if e.entity_type == "olt_profile_sync_task"]

    if bundle_events:
        logger.info("Profile Bundle Events:")
        logger.info("-" * 40)
        for e in bundle_events[:5]:
            status = "OK" if e.is_success else "FAIL"
            meta = e.metadata_ or {}
            logger.info(f"  [{status}] {e.action}")
            logger.info(f"      Time: {e.occurred_at}")
            logger.info(f"      Actor: {e.actor_id}")
            if "drift_status" in meta:
                logger.info(f"      Drift: {meta['drift_status']}")
            if "missing" in meta and meta["missing"]:
                logger.info(f"      Missing: {meta['missing'][:3]}")
            if "mismatched" in meta and meta["mismatched"]:
                logger.info(f"      Mismatched: {meta['mismatched'][:3]}")

    if task_events:
        logger.info("\nProfile Sync Task Events:")
        logger.info("-" * 40)
        for e in task_events[:5]:
            status = "OK" if e.is_success else "FAIL"
            meta = e.metadata_ or {}
            logger.info(f"  [{status}] {e.action}")
            logger.info(f"      Time: {e.occurred_at}")
            logger.info(f"      Actor: {e.actor_id}")
            if "status" in meta:
                logger.info(f"      Task Status: {meta['status']}")
            if "error" in meta:
                logger.info(f"      Error: {meta['error']}")

    # Summary stats
    logger.info("\nAudit Summary:")
    logger.info(f"  Total events: {len(events)}")
    logger.info(f"  Bundle drift checks: {len(bundle_events)}")
    logger.info(f"  Task operations: {len(task_events)}")
    success_count = sum(1 for e in events if e.is_success)
    logger.info(f"  Success rate: {success_count}/{len(events)}")

    # Check for expected audit actions
    expected_actions = {
        "olt_profile_bundle_drift_checked",
        "olt_profile_sync_task_approved",
        "olt_profile_sync_task_scheduled",
        "olt_profile_sync_task_completed",
    }
    found_actions = {e.action for e in events}
    missing_actions = expected_actions - found_actions

    if missing_actions:
        logger.info(f"\nNot yet observed: {sorted(missing_actions)}")
        logger.info("Run more operations to see all audit types")

    return True


def show_status(db) -> None:
    """Show current system status."""
    logger.info("=" * 60)
    logger.info("Profile Sync System Status")
    logger.info("=" * 60)

    # OLTs
    olt_count = db.scalar(select(func.count(OLTDevice.id)))
    olt_ssh_count = db.scalar(
        select(func.count(OLTDevice.id)).where(OLTDevice.ssh_username.isnot(None))
    )
    logger.info(f"\nOLTs: {olt_count} total, {olt_ssh_count} with SSH")

    # Bundles
    bundle_count = db.scalar(
        select(func.count(OltProfileBundle.id)).where(
            OltProfileBundle.is_active.is_(True)
        )
    )
    logger.info(f"Active Profile Bundles: {bundle_count}")

    # Tasks by status
    task_rows = db.execute(
        text("""
            SELECT status, COUNT(*)
            FROM olt_profile_sync_tasks
            GROUP BY status
        """)
    ).fetchall()
    logger.info("Sync Tasks:")
    for status, count in task_rows:
        logger.info(f"  {status}: {count}")
    if not task_rows:
        logger.info("  (none)")

    # Worker status
    result = db.execute(
        text("""
            SELECT key, value_text
            FROM domain_settings
            WHERE domain = 'network'
            AND key IN ('olt_profile_sync_worker_enabled', 'olt_profile_sync_interval_seconds')
        """)
    ).fetchall()
    settings = {row[0]: row[1] for row in result}
    worker_enabled = settings.get("olt_profile_sync_worker_enabled", "false")
    interval = settings.get("olt_profile_sync_interval_seconds", "300")
    logger.info(f"\nWorker: enabled={worker_enabled}, interval={interval}s")

    # Scheduled task
    sched = db.execute(
        text("""
            SELECT enabled, interval_seconds, last_run_at
            FROM scheduled_tasks
            WHERE task_name = 'app.tasks.profile_sync.execute_due_profile_sync_tasks'
        """)
    ).fetchone()
    if sched:
        logger.info(f"Scheduler: enabled={sched[0]}, interval={sched[1]}s")
        logger.info(f"           last_run={sched[2]}")


def main():
    parser = argparse.ArgumentParser(
        description="Validate profile sync operational behavior"
    )
    parser.add_argument(
        "--step",
        choices=["status", "drift-check", "create-task", "verify-audit"],
        help="Run a specific validation step",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run all validation steps",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )
    args = parser.parse_args()

    if not args.step and not args.all:
        args.step = "status"

    db = SessionLocal()
    try:
        if args.step == "status" or args.all:
            show_status(db)

        if args.step == "drift-check" or args.all:
            step_1_drift_check(db, dry_run=args.dry_run)

        if args.step == "create-task" or args.all:
            step_2_create_task(db, dry_run=args.dry_run)

        if args.step == "verify-audit" or args.all:
            step_3_verify_audit(db)

        logger.info("\n" + "=" * 60)
        logger.info("Validation complete")
        logger.info("=" * 60)

    except Exception as e:
        logger.exception(f"Validation failed: {e}")
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
