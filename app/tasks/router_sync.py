import json
import logging
import time
from datetime import UTC, datetime

from sqlalchemy import select

from app.celery_app import celery_app
from app.db import SessionLocal
from app.models.router_management import (
    Router,
    RouterConfigPush,
    RouterConfigPushStatus,
    RouterPushResultStatus,
    RouterSnapshotSource,
    RouterStatus,
)
from app.services.router_management.config import RouterConfigService
from app.services.router_management.connection import RouterConnectionService
from app.services.router_management.inventory import RouterInventory

logger = logging.getLogger(__name__)


@celery_app.task(name="router_sync.sync_all_system_info")
def sync_all_system_info() -> dict:
    db = SessionLocal()
    try:
        routers = list(
            db.execute(select(Router).where(Router.is_active.is_(True))).scalars().all()
        )

        success = 0
        failed = 0
        for router in routers:
            try:
                RouterInventory.sync_system_info(db, router)
                success += 1
            except Exception as exc:
                logger.warning("Failed to sync %s: %s", router.name, exc)
                router.status = RouterStatus.unreachable
                db.commit()
                failed += 1

        return {"success": success, "failed": failed, "total": len(routers)}
    finally:
        db.close()


@celery_app.task(name="router_sync.sync_all_interfaces")
def sync_all_interfaces() -> dict:
    db = SessionLocal()
    try:
        routers = list(
            db.execute(
                select(Router).where(
                    Router.is_active.is_(True),
                    Router.status == RouterStatus.online,
                )
            )
            .scalars()
            .all()
        )

        success = 0
        failed = 0
        for router in routers:
            try:
                RouterInventory.sync_interfaces(db, router)
                success += 1
            except Exception as exc:
                logger.warning("Failed to sync interfaces for %s: %s", router.name, exc)
                failed += 1

        return {"success": success, "failed": failed, "total": len(routers)}
    finally:
        db.close()


@celery_app.task(name="router_sync.capture_scheduled_snapshots")
def capture_scheduled_snapshots() -> dict:
    db = SessionLocal()
    try:
        routers = list(
            db.execute(
                select(Router).where(
                    Router.is_active.is_(True),
                    Router.status == RouterStatus.online,
                )
            )
            .scalars()
            .all()
        )

        success = 0
        failed = 0
        for router in routers:
            try:
                data = RouterConnectionService.execute(router, "GET", "/export")
                config_text = data if isinstance(data, str) else str(data)
                RouterConfigService.store_snapshot(
                    db,
                    router_id=router.id,
                    config_export=config_text,
                    source=RouterSnapshotSource.scheduled,
                )
                router.last_config_sync_at = datetime.now(UTC)
                db.commit()
                success += 1
            except Exception as exc:
                logger.warning("Failed to snapshot %s: %s", router.name, exc)
                failed += 1

        return {"success": success, "failed": failed, "total": len(routers)}
    finally:
        db.close()


@celery_app.task(name="router_sync.cleanup_idle_tunnels")
def cleanup_idle_tunnels() -> dict:
    closed = RouterConnectionService.cleanup_idle_tunnels()
    return {"closed": closed}


@celery_app.task(name="router_sync.execute_config_push")
def execute_config_push(push_id: str) -> dict:
    db = SessionLocal()
    try:
        push = db.get(RouterConfigPush, push_id)
        if not push:
            return {"error": "Push not found"}

        push.status = RouterConfigPushStatus.running
        db.commit()

        success_count = 0
        fail_count = 0

        for result in push.results:
            router = db.get(Router, result.router_id)
            if not router or not router.is_active:
                result.status = RouterPushResultStatus.skipped
                result.error_message = "Router inactive or not found"
                db.commit()
                continue

            start_time = time.time()
            try:
                pre_data = RouterConnectionService.execute(router, "GET", "/export")
                pre_text = pre_data if isinstance(pre_data, str) else str(pre_data)
                pre_snap = RouterConfigService.store_snapshot(
                    db,
                    router_id=router.id,
                    config_export=pre_text,
                    source=RouterSnapshotSource.pre_change,
                )
                result.pre_snapshot_id = pre_snap.id
                db.commit()

                # Commands are RouterOS REST API paths, optionally followed by
                # a space-separated JSON payload string, e.g.:
                #   "/ip/address/add" '{"address":"192.168.1.1/24","interface":"ether1"}'
                responses = []
                for cmd in push.commands:
                    parts = cmd.strip().split(" ", 1)
                    path = parts[0]
                    payload: dict | None = None
                    if len(parts) == 2:
                        try:
                            payload = json.loads(parts[1])
                        except json.JSONDecodeError:
                            logger.warning(
                                "Could not parse payload for command %r — sending without payload",
                                cmd,
                            )
                    resp = RouterConnectionService.execute(
                        router, "POST", path, payload=payload
                    )
                    responses.append(resp)

                post_data = RouterConnectionService.execute(router, "GET", "/export")
                post_text = post_data if isinstance(post_data, str) else str(post_data)
                post_snap = RouterConfigService.store_snapshot(
                    db,
                    router_id=router.id,
                    config_export=post_text,
                    source=RouterSnapshotSource.post_change,
                )

                result.post_snapshot_id = post_snap.id
                result.response_data = responses
                result.status = RouterPushResultStatus.success
                result.duration_ms = int((time.time() - start_time) * 1000)
                router.last_config_change_at = datetime.now(UTC)
                db.commit()
                success_count += 1

            except Exception as exc:
                result.status = RouterPushResultStatus.failed
                result.error_message = str(exc)[:500]
                result.duration_ms = int((time.time() - start_time) * 1000)
                db.commit()
                fail_count += 1
                logger.warning("Push to %s failed: %s", router.name, exc)

        if fail_count == 0:
            push.status = RouterConfigPushStatus.completed
        elif success_count == 0:
            push.status = RouterConfigPushStatus.failed
        else:
            push.status = RouterConfigPushStatus.partial_failure
        push.completed_at = datetime.now(UTC)
        db.commit()

        return {
            "push_id": push_id,
            "status": push.status.value,
            "success": success_count,
            "failed": fail_count,
        }
    finally:
        db.close()
