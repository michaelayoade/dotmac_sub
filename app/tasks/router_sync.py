import logging
import time
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from app.celery_app import celery_app
from app.models.network_operation import (
    NetworkOperation,
    NetworkOperationStatus,
    NetworkOperationTargetType,
    NetworkOperationType,
)
from app.models.router_management import (
    Router,
    RouterConfigPush,
    RouterConfigPushResult,
    RouterConfigPushStatus,
    RouterConfigSnapshot,
    RouterPushResultStatus,
    RouterSnapshotSource,
    RouterStatus,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.network_operations import network_operations
from app.services.router_management.config import RouterConfigService
from app.services.router_management.config_export import fetch_config_export
from app.services.router_management.connection import RouterConnectionService
from app.services.router_management.inventory import RouterInventory
from app.services.router_management.sot_policy import (
    RouterSotIntent,
    parse_routeros_sot_intents,
)
from app.services.router_management.write_adapter import (
    RouterPostWriteReadbackError,
    RouterSotWriteAdapter,
    RouterWriteRejected,
)

logger = logging.getLogger(__name__)


def _fetch_config_export(router) -> str:
    """Compatibility wrapper around the canonical snapshot transport."""
    return fetch_config_export(router)


@celery_app.task(name="router_sync.sync_all_system_info")
def sync_all_system_info() -> dict:
    db = db_session_adapter.create_session()
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
    db = db_session_adapter.create_session()
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
    db = db_session_adapter.create_session()
    try:
        routers = list(
            db.execute(
                select(Router)
                .options(selectinload(Router.jump_host))
                .where(
                    Router.is_active.is_(True),
                    Router.status == RouterStatus.online,
                )
            )
            .scalars()
            .all()
        )
        # Detach the routers and close the read transaction BEFORE the slow
        # per-router REST /export loop. Otherwise expire_on_commit re-loads each
        # router's attributes on the next iteration, reopening a transaction
        # that then sits idle through the network call (one router is
        # unreachable → full timeout) and trips Postgres'
        # idle_in_transaction_session_timeout, aborting the whole run with zero
        # snapshots. jump_host is eager-loaded so the connection layer can still
        # read it while detached.
        db.expunge_all()
        db.rollback()

        success = 0
        failed = 0
        for router in routers:
            try:
                config_text = _fetch_config_export(router)
                # store_snapshot commits its own short transaction.
                RouterConfigService.store_snapshot(
                    db,
                    router_id=router.id,
                    config_export=config_text,
                    source=RouterSnapshotSource.scheduled,
                )
                # router is detached; update the timestamp with a targeted UPDATE.
                db.execute(
                    update(Router)
                    .where(Router.id == router.id)
                    .values(last_config_sync_at=datetime.now(UTC))
                )
                db.commit()
                success += 1
            except Exception as exc:
                db.rollback()
                logger.warning("Failed to snapshot %s: %s", router.name, exc)
                failed += 1

        return {"success": success, "failed": failed, "total": len(routers)}
    finally:
        db.close()


@celery_app.task(name="router_sync.cleanup_idle_tunnels")
def cleanup_idle_tunnels() -> dict:
    closed = RouterConnectionService.cleanup_idle_tunnels()
    return {"closed": closed}


def _capture_post_snapshot(
    db, router: Router, *, required: bool = False
) -> RouterConfigSnapshot | None:
    """Capture current state; successful writes require this audit evidence."""
    try:
        post_text = _fetch_config_export(router)
        return RouterConfigService.store_snapshot(
            db,
            router_id=router.id,
            config_export=post_text,
            source=RouterSnapshotSource.post_change,
            commit=False,
        )
    except Exception as exc:
        logger.warning("Post-change snapshot failed for %s: %s", router.name, exc)
        if required:
            raise RouterPostWriteReadbackError(
                f"Device readback completed, but post-change snapshot failed: {exc}"
            ) from exc
        return None


def _active_operation(db, operation_id) -> bool:
    if not operation_id:
        return False
    operation = network_operations.get(db, str(operation_id))
    return operation.status in {
        NetworkOperationStatus.pending,
        NetworkOperationStatus.running,
        NetworkOperationStatus.waiting,
    }


def _mark_result_failed(db, result: RouterConfigPushResult, message: str) -> None:
    result.status = RouterPushResultStatus.failed
    result.error_message = message[:500]
    if _active_operation(db, result.operation_id):
        network_operations.mark_failed(
            db,
            str(result.operation_id),
            message,
            output_payload={"push_result_id": str(result.id), "verified": False},
        )


def _mark_result_skipped(db, result: RouterConfigPushResult, message: str) -> None:
    result.status = RouterPushResultStatus.skipped
    result.error_message = message[:500]
    if _active_operation(db, result.operation_id):
        network_operations.mark_warning(
            db,
            str(result.operation_id),
            message,
            output_payload={"push_result_id": str(result.id), "skipped": True},
        )


def _mark_result_pending_readback(
    db,
    result: RouterConfigPushResult,
    message: str,
    *,
    response_data: dict | list | None = None,
) -> None:
    result.status = RouterPushResultStatus.pending_readback
    result.error_message = message[:500]
    if response_data is not None:
        result.response_data = response_data
    if _active_operation(db, result.operation_id):
        network_operations.mark_waiting(db, str(result.operation_id), message)


def _derive_push_status(push: RouterConfigPush) -> RouterConfigPushStatus:
    statuses = {result.status for result in push.results}
    if RouterPushResultStatus.running in statuses:
        return RouterConfigPushStatus.running
    if RouterPushResultStatus.pending_readback in statuses:
        return RouterConfigPushStatus.pending_readback
    if RouterPushResultStatus.pending in statuses:
        return RouterConfigPushStatus.pending
    has_failed = RouterPushResultStatus.failed in statuses
    has_success = RouterPushResultStatus.success in statuses
    has_skipped = RouterPushResultStatus.skipped in statuses
    if (has_failed or has_skipped) and has_success:
        return RouterConfigPushStatus.partial_failure
    if has_failed or has_skipped:
        return RouterConfigPushStatus.failed
    return RouterConfigPushStatus.completed


def _refresh_parent_operation(db, push: RouterConfigPush) -> None:
    if push.operation_id:
        network_operations.update_parent_status(db, str(push.operation_id))


def _recover_pending_readback(
    db,
    result_id,
    message: str,
    response_data: dict | list | None,
) -> None:
    """Persist an ambiguity marker in a fresh transaction after audit failure."""
    db.rollback()
    recovery_db = db_session_adapter.create_session()
    try:
        result = recovery_db.get(RouterConfigPushResult, result_id)
        if result is None:
            raise RuntimeError(f"Router push result {result_id} no longer exists")
        _mark_result_pending_readback(
            recovery_db,
            result,
            message,
            response_data=response_data,
        )
        push = recovery_db.get(RouterConfigPush, result.push_id)
        if push is not None:
            push.status = RouterConfigPushStatus.pending_readback
            push.completed_at = None
            _refresh_parent_operation(recovery_db, push)
        recovery_db.commit()
    except Exception:
        recovery_db.rollback()
        raise
    finally:
        recovery_db.close()


@celery_app.task(name="router_sync.execute_config_push")
def execute_config_push(push_id: str) -> dict:
    db = db_session_adapter.create_session()
    try:
        push = db.get(RouterConfigPush, push_id)
        if not push:
            return {"error": "Push not found"}

        try:
            plans = parse_routeros_sot_intents(push.commands)
        except Exception as exc:
            message = f"Stored RouterOS desired state is not verifiable: {exc}"
            for result in push.results:
                _mark_result_failed(db, result, message)
            push.status = RouterConfigPushStatus.failed
            push.completed_at = datetime.now(UTC)
            _refresh_parent_operation(db, push)
            db.commit()
            return {
                "push_id": push_id,
                "status": push.status.value,
                "success": 0,
                "failed": len(push.results),
                "pending_readback": 0,
                "skipped": 0,
                "dry_run": push.dry_run,
                "failure_policy": push.failure_policy,
            }
        push.status = RouterConfigPushStatus.running
        if _active_operation(db, push.operation_id):
            network_operations.mark_running(db, str(push.operation_id))
        db.commit()

        success_count = 0
        fail_count = 0
        pending_readback_count = 0
        skipped_count = 0
        abort_remaining = False
        adapter = RouterSotWriteAdapter()

        for result in push.results:
            if abort_remaining:
                _mark_result_skipped(
                    db,
                    result,
                    "Skipped because failure policy aborted after a prior failure.",
                )
                db.commit()
                skipped_count += 1
                continue

            router = db.get(Router, result.router_id)
            if not router or not router.is_active:
                _mark_result_skipped(db, result, "Router inactive or not found")
                db.commit()
                skipped_count += 1
                continue

            start_time = time.time()
            apply_payload: dict | None = None
            result.status = RouterPushResultStatus.running
            if _active_operation(db, result.operation_id):
                network_operations.mark_running(db, str(result.operation_id))
            db.commit()
            try:
                pre_text = _fetch_config_export(router)
                pre_snap = RouterConfigService.store_snapshot(
                    db,
                    router_id=router.id,
                    config_export=pre_text,
                    source=RouterSnapshotSource.pre_change,
                )
                result.pre_snapshot_id = pre_snap.id
                db.commit()

                if push.dry_run:
                    result.response_data = {
                        "dry_run": True,
                        "planned_intents": [plan.preview() for plan in plans],
                        "verified": False,
                        "write_accepted": False,
                    }
                    result.status = RouterPushResultStatus.success
                    result.duration_ms = int((time.time() - start_time) * 1000)
                    if _active_operation(db, result.operation_id):
                        network_operations.mark_succeeded(
                            db,
                            str(result.operation_id),
                            output_payload={
                                "dry_run": True,
                                "write_accepted": False,
                                "verified": False,
                            },
                        )
                    db.commit()
                    success_count += 1
                    continue

                apply_result = adapter.apply(router, plans)
                apply_payload = apply_result.to_dict()
                result.response_data = apply_payload
                if not apply_result.verified:
                    raise RuntimeError(
                        "RouterOS readback does not match the requested configuration"
                    )
                post_snap = _capture_post_snapshot(db, router, required=True)
                result.post_snapshot_id = post_snap.id if post_snap else None
                result.status = RouterPushResultStatus.success
                result.duration_ms = int((time.time() - start_time) * 1000)
                router.last_config_change_at = datetime.now(UTC)
                if _active_operation(db, result.operation_id):
                    network_operations.mark_succeeded(
                        db,
                        str(result.operation_id),
                        output_payload={
                            **apply_payload,
                            "post_snapshot_id": str(post_snap.id)
                            if post_snap
                            else None,
                        },
                    )
                db.commit()
                success_count += 1

            except RouterPostWriteReadbackError as exc:
                result.duration_ms = int((time.time() - start_time) * 1000)
                if exc.partial_result is not None:
                    apply_payload = exc.partial_result.to_dict()
                _mark_result_pending_readback(
                    db,
                    result,
                    str(exc),
                    response_data=apply_payload,
                )
                post_snap = _capture_post_snapshot(db, router)
                if post_snap is not None:
                    result.post_snapshot_id = post_snap.id
                db.commit()
                pending_readback_count += 1
                logger.warning("Push to %s is pending readback: %s", router.name, exc)
                if push.failure_policy == "abort":
                    abort_remaining = True
            except RouterWriteRejected as exc:
                result.duration_ms = int((time.time() - start_time) * 1000)
                if exc.partial_result is not None:
                    apply_payload = exc.partial_result.to_dict()
                _mark_result_failed(db, result, str(exc))
                if apply_payload is not None:
                    result.response_data = apply_payload
                post_snap = _capture_post_snapshot(db, router)
                if post_snap is not None:
                    result.post_snapshot_id = post_snap.id
                db.commit()
                fail_count += 1
                if push.failure_policy == "abort":
                    abort_remaining = True
            except Exception as exc:
                result.duration_ms = int((time.time() - start_time) * 1000)
                if apply_payload and apply_payload.get("verified") is True:
                    recovery_message = (
                        "RouterOS write and readback completed, but atomic audit "
                        f"persistence failed: {exc}"
                    )
                    _recover_pending_readback(
                        db,
                        result.id,
                        recovery_message,
                        apply_payload,
                    )
                    pending_readback_count += 1
                    if push.failure_policy == "abort":
                        abort_remaining = True
                    continue
                _mark_result_failed(db, result, str(exc))
                if apply_payload is not None:
                    result.response_data = apply_payload
                post_snap = _capture_post_snapshot(db, router)
                if post_snap is not None:
                    result.post_snapshot_id = post_snap.id
                db.commit()
                fail_count += 1
                logger.warning("Push to %s failed: %s", router.name, exc)
                if push.failure_policy == "abort":
                    abort_remaining = True

        push.status = _derive_push_status(push)
        if push.status not in {
            RouterConfigPushStatus.pending,
            RouterConfigPushStatus.running,
            RouterConfigPushStatus.pending_readback,
        }:
            push.completed_at = datetime.now(UTC)
        _refresh_parent_operation(db, push)
        db.commit()

        return {
            "push_id": push_id,
            "status": push.status.value,
            "success": success_count,
            "failed": fail_count,
            "pending_readback": pending_readback_count,
            "skipped": skipped_count,
            "dry_run": push.dry_run,
            "failure_policy": push.failure_policy,
        }
    finally:
        db.close()


@celery_app.task(name="router_sync.reconcile_config_push_readback")
def reconcile_config_push_readback(max_results: int = 25) -> dict[str, int]:
    """Retry readback and snapshot capture for writes with ambiguous outcomes."""
    db = db_session_adapter.create_session()
    stats = {"checked": 0, "verified": 0, "drifted": 0, "pending": 0}
    try:
        rows = list(
            db.scalars(
                select(RouterConfigPushResult)
                .where(
                    RouterConfigPushResult.status
                    == RouterPushResultStatus.pending_readback
                )
                .order_by(RouterConfigPushResult.created_at.asc())
                .limit(max_results)
            ).all()
        )
        adapter = RouterSotWriteAdapter()
        touched_pushes: set[str] = set()
        for result in rows:
            stats["checked"] += 1
            push = db.get(RouterConfigPush, result.push_id)
            router = db.get(Router, result.router_id)
            if push is None or router is None or not router.is_active:
                _mark_result_failed(
                    db, result, "Readback target is inactive or missing"
                )
                stats["drifted"] += 1
                db.commit()
                continue
            touched_pushes.add(str(push.id))
            try:
                readback = adapter.readback(
                    router, parse_routeros_sot_intents(push.commands)
                )
                payload = readback.to_dict()
                result.response_data = payload
                if not readback.verified:
                    _mark_result_failed(
                        db,
                        result,
                        "RouterOS reconciliation found configuration drift",
                    )
                    stats["drifted"] += 1
                else:
                    post_snap = _capture_post_snapshot(db, router, required=True)
                    result.post_snapshot_id = post_snap.id if post_snap else None
                    result.status = RouterPushResultStatus.success
                    result.error_message = None
                    if _active_operation(db, result.operation_id):
                        network_operations.mark_succeeded(
                            db,
                            str(result.operation_id),
                            output_payload={
                                **payload,
                                "reconciled": True,
                                "post_snapshot_id": (
                                    str(post_snap.id) if post_snap else None
                                ),
                            },
                        )
                    stats["verified"] += 1
                db.commit()
            except RouterPostWriteReadbackError as exc:
                _mark_result_pending_readback(db, result, str(exc))
                db.commit()
                stats["pending"] += 1
            except Exception as exc:
                _mark_result_failed(
                    db, result, f"RouterOS reconciliation failed: {exc}"
                )
                db.commit()
                stats["drifted"] += 1

        for push_id in touched_pushes:
            push = db.get(RouterConfigPush, push_id)
            if push is None:
                continue
            push.status = _derive_push_status(push)
            if push.status not in {
                RouterConfigPushStatus.pending,
                RouterConfigPushStatus.running,
                RouterConfigPushStatus.pending_readback,
            }:
                push.completed_at = datetime.now(UTC)
            _refresh_parent_operation(db, push)
        db.commit()
        return stats
    finally:
        db.close()


@celery_app.task(name="router_sync.audit_sot_drift")
def audit_sot_drift(max_results: int = 1000, max_routers: int = 100) -> dict[str, int]:
    """Compare each router's latest typed intent with live owned resources."""
    db = db_session_adapter.create_session()
    stats = {
        "routers": 0,
        "intents": 0,
        "in_sync": 0,
        "drifted": 0,
        "unreachable": 0,
        "invalid": 0,
    }
    active_findings: dict[str, dict[str, Any]] = {}
    try:
        rows = db.execute(
            select(RouterConfigPushResult, RouterConfigPush)
            .join(
                RouterConfigPush,
                RouterConfigPush.id == RouterConfigPushResult.push_id,
            )
            .where(RouterConfigPush.dry_run.is_(False))
            .order_by(
                RouterConfigPush.created_at.desc(),
                RouterConfigPushResult.created_at.desc(),
            )
            .limit(max_results)
        ).all()
        latest: dict[UUID, dict[tuple[str, str], RouterSotIntent]] = {}
        for result, push in rows:
            router_key = result.router_id
            if router_key not in latest and len(latest) >= max_routers:
                continue
            try:
                intents = parse_routeros_sot_intents(push.commands)
            except Exception:
                stats["invalid"] += 1
                continue
            owned = latest.setdefault(router_key, {})
            for intent in intents:
                owned.setdefault((intent.resource.value, intent.key), intent)

        routers = {
            router.id: router
            for router in db.scalars(
                select(Router).where(
                    Router.id.in_(list(latest)),
                    Router.is_active.is_(True),
                )
            ).all()
        }
        db.expunge_all()
        db.rollback()

        adapter = RouterSotWriteAdapter()
        for router_id, desired in latest.items():
            router = routers.get(router_id)
            if router is None:
                continue
            stats["routers"] += 1
            intents = list(desired.values())
            stats["intents"] += len(intents)
            try:
                result = adapter.readback(router, intents)
            except RouterPostWriteReadbackError as exc:
                stats["unreachable"] += 1
                fingerprint = f"router-sot:{router.id}"
                active_findings[fingerprint] = {
                    "router": router,
                    "summary": str(exc),
                    "details": {"unreachable": True},
                    "critical": True,
                }
                continue
            drift_details = []
            for command in result.commands:
                if command.verified:
                    stats["in_sync"] += 1
                else:
                    stats["drifted"] += 1
                    plan = cast(RouterSotIntent, command.plan)
                    drift_details.append(
                        {
                            "resource": plan.resource.value,
                            "key": plan.key,
                            "drift": command.drift,
                        }
                    )
            if drift_details:
                fingerprint = f"router-sot:{router.id}"
                active_findings[fingerprint] = {
                    "router": router,
                    "summary": (
                        f"{len(drift_details)} owned RouterOS resource(s) differ "
                        "from desired state."
                    ),
                    "details": {"drift": drift_details},
                    "critical": False,
                }

        try:
            from app.models.network_monitoring import AlertSeverity
            from app.services.observability import (
                Finding,
                StateObservation,
                publish_state_snapshot,
                record_finding,
                resolve_findings,
            )

            for fingerprint, finding in active_findings.items():
                router = cast(Router, finding["router"])
                record_finding(
                    db,
                    Finding(
                        fingerprint=fingerprint,
                        domain="router_sot",
                        source="router_sot_drift",
                        severity=(
                            AlertSeverity.critical
                            if finding["critical"]
                            else AlertSeverity.warning
                        ),
                        title=f"Router SOT drift: {router.name}",
                        summary=str(finding["summary"]),
                        details=dict(finding["details"]),
                        target_url=f"/admin/network/routers/{router.id}",
                    ),
                )
            resolve_findings(
                db,
                managed_prefix="router-sot:",
                active_fingerprints=set(active_findings),
            )
            db.commit()

            status = (
                "error"
                if stats["unreachable"]
                else "degraded"
                if stats["drifted"] or stats["invalid"]
                else "ok"
            )
            publish_state_snapshot(
                "router_sot",
                [
                    StateObservation(signal=signal, scope="fleet", value=value)
                    for signal, value in stats.items()
                ],
                status=status,
            )
        except Exception:
            logger.exception("router_sot_snapshot_publish_failed")
        return stats
    finally:
        db.close()


@celery_app.task(name="router_sync.reconcile_nas_vlan_readback")
def reconcile_nas_vlan_readback(max_operations: int = 25) -> dict[str, int]:
    """Resolve waiting MikroTik NAS VLAN operations from live RouterOS state."""
    from app.models.catalog import NasDevice
    from app.services.nas._mikrotik_vlan import get_vlan_status

    db = db_session_adapter.create_session()
    stats = {"checked": 0, "verified": 0, "drifted": 0, "pending": 0}
    try:
        operations = list(
            db.scalars(
                select(NetworkOperation)
                .where(
                    NetworkOperation.status == NetworkOperationStatus.waiting,
                    NetworkOperation.operation_type
                    == NetworkOperationType.nas_vlan_provision,
                    NetworkOperation.target_type == NetworkOperationTargetType.nas,
                    NetworkOperation.correlation_key.like("nas-vlan:%"),
                )
                .order_by(NetworkOperation.created_at.asc())
                .limit(max_operations)
            ).all()
        )
        for operation in operations:
            stats["checked"] += 1
            desired = operation.input_payload or {}
            nas = db.get(NasDevice, operation.target_id)
            if nas is None:
                network_operations.mark_failed(
                    db, str(operation.id), "NAS readback target no longer exists"
                )
                stats["drifted"] += 1
                db.commit()
                continue
            try:
                observed = get_vlan_status(
                    nas,
                    vlan_id=int(desired["vlan_id"]),
                    parent_interface=str(desired["parent_interface"]),
                )
            except Exception as exc:
                network_operations.mark_waiting(
                    db, str(operation.id), f"NAS VLAN readback failed: {exc}"
                )
                stats["pending"] += 1
                db.commit()
                continue
            if observed.get("error"):
                network_operations.mark_waiting(
                    db,
                    str(operation.id),
                    f"NAS VLAN readback failed: {observed['error']}",
                )
                stats["pending"] += 1
                db.commit()
                continue

            expected_service = desired.get("pppoe_service_name")
            verified = (
                observed.get("has_vlan") is True
                and observed.get("has_ip") is True
                and observed.get("ip_address") == desired.get("ip_address")
                and observed.get("has_pppoe") is True
                and (
                    not expected_service
                    or observed.get("pppoe_service") == expected_service
                )
            )
            payload = {
                "verified": verified,
                "desired": desired,
                "observed": observed,
                "reconciled": True,
            }
            if verified:
                network_operations.mark_succeeded(
                    db, str(operation.id), output_payload=payload
                )
                stats["verified"] += 1
            else:
                network_operations.mark_failed(
                    db,
                    str(operation.id),
                    "NAS VLAN reconciliation found configuration drift",
                    output_payload=payload,
                )
                stats["drifted"] += 1
            if operation.parent_id:
                network_operations.update_parent_status(db, str(operation.parent_id))
            db.commit()
        return stats
    finally:
        db.close()
