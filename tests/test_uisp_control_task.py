from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import CPEDevice, DeviceType, VendorModelCapability
from app.models.network_operation import NetworkOperationStatus
from app.models.uisp_control import (
    UispConfigSnapshot,
    UispDeviceIntent,
    UispIntentStatus,
    UispIntentTargetType,
    UispSnapshotSource,
)
from app.services.uisp_control_plane import request_apply, stage_intent
from app.services.uisp_write_adapter import (
    UispApplyResult,
    UispPostWriteReadbackError,
    UispWriteUnsupported,
)
from app.tasks.uisp_control import (
    _mark_pending_readback,
    execute_uisp_apply,
    reconcile_uisp_config_readback,
)


def _records(db_session, subscriber, catalog_offer):
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
    )
    cpe = CPEDevice(
        subscriber_id=subscriber.id,
        subscription=subscription,
        device_type=DeviceType.wireless_radio,
        vendor="ubiquiti",
        model="airCube-ISP",
        uisp_device_id="uisp-device-1",
    )
    capability = VendorModelCapability(
        vendor="ubiquiti",
        model="airCube-ISP",
        supported_features={
            "uisp": {
                "configuration_write": True,
                "transport": "onu",
                "fields": {"wifi.ssid": "/wireless/ssid"},
            }
        },
    )
    db_session.add_all([subscription, cpe, capability])
    db_session.flush()
    intent = stage_intent(
        db_session,
        target_type=UispIntentTargetType.cpe,
        target_id=cpe.id,
        desired_state={"wifi": {"ssid": "Customer"}},
    )
    operation = request_apply(db_session, intent, enqueue=False)
    return intent, operation


def _use_session(monkeypatch, db_session):
    @contextmanager
    def session():
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    monkeypatch.setattr("app.tasks.uisp_control.db_session_adapter.session", session)


def test_task_marks_success_only_after_adapter_verification(
    db_session, subscriber, catalog_offer, monkeypatch
):
    intent, operation = _records(db_session, subscriber, catalog_offer)
    _use_session(monkeypatch, db_session)
    adapter = SimpleNamespace(
        apply=lambda db, item: UispApplyResult(
            outcome="verified",
            message="matched",
            write_accepted=True,
            verified=True,
            attempts=2,
            observed_config={"wifi.ssid": "Customer"},
        )
    )

    result = execute_uisp_apply(str(operation.id), str(intent.id), adapter=adapter)

    db_session.refresh(intent)
    db_session.refresh(operation)
    assert result["success"] is True
    assert intent.status == UispIntentStatus.verified
    assert intent.verified_revision == intent.desired_revision
    assert operation.status == NetworkOperationStatus.succeeded
    assert operation.output_payload["verified"] is True


def test_task_marks_readback_drift_failed(
    db_session, subscriber, catalog_offer, monkeypatch
):
    intent, operation = _records(db_session, subscriber, catalog_offer)
    _use_session(monkeypatch, db_session)
    adapter = SimpleNamespace(
        apply=lambda db, item: UispApplyResult(
            outcome="drifted",
            message="readback did not converge",
            write_accepted=True,
            verified=False,
            attempts=5,
            observed_config={"wifi.ssid": "Old"},
            drift={"wifi.ssid": {"desired": "Customer", "observed": "Old"}},
        )
    )

    result = execute_uisp_apply(str(operation.id), str(intent.id), adapter=adapter)

    db_session.refresh(intent)
    db_session.refresh(operation)
    assert result["success"] is False
    assert intent.status == UispIntentStatus.drifted
    assert operation.status == NetworkOperationStatus.failed


def test_snapshot_failure_after_verified_write_becomes_pending_readback(
    db_session, subscriber, catalog_offer, monkeypatch
):
    intent, operation = _records(db_session, subscriber, catalog_offer)
    _use_session(monkeypatch, db_session)
    original_add = db_session.add

    def fail_snapshot_add(instance):
        if isinstance(instance, UispConfigSnapshot):
            if instance.intent is not None and instance in instance.intent.snapshots:
                instance.intent.snapshots.remove(instance)
            raise RuntimeError("snapshot persistence failed")
        return original_add(instance)

    monkeypatch.setattr(db_session, "add", fail_snapshot_add)
    recovery_calls = []
    monkeypatch.setattr(
        "app.tasks.uisp_control._mark_pending_readback",
        lambda db, operation_id, intent_id, message: recovery_calls.append(
            (operation_id, intent_id, message)
        ),
    )
    adapter = SimpleNamespace(
        apply=lambda db, item: UispApplyResult(
            outcome="verified",
            message="matched",
            write_accepted=True,
            verified=True,
            attempts=1,
            observed_config={"wifi.ssid": "Customer"},
        )
    )

    result = execute_uisp_apply(str(operation.id), str(intent.id), adapter=adapter)

    assert result["outcome"] == "pending_readback"
    assert recovery_calls == [
        (
            str(operation.id),
            str(intent.id),
            "UISP write/readback completed but atomic audit persistence failed: "
            "snapshot persistence failed",
        )
    ]


def test_atomic_finalize_commit_failure_rolls_back_success_and_marks_pending(
    db_session, subscriber, catalog_offer, monkeypatch
):
    intent, operation = _records(db_session, subscriber, catalog_offer)
    _use_session(monkeypatch, db_session)
    original_commit = db_session.commit
    state = {"write_returned": False, "failed": False}

    def apply(db, item):
        state["write_returned"] = True
        return UispApplyResult(
            outcome="verified",
            message="matched",
            write_accepted=True,
            verified=True,
            attempts=1,
            observed_config={"wifi.ssid": "Customer"},
        )

    def fail_finalize_once():
        if state["write_returned"] and not state["failed"]:
            state["failed"] = True
            raise RuntimeError("commit failed")
        return original_commit()

    monkeypatch.setattr(db_session, "commit", fail_finalize_once)
    recovery_calls = []
    monkeypatch.setattr(
        "app.tasks.uisp_control._mark_pending_readback",
        lambda db, operation_id, intent_id, message: recovery_calls.append(
            (operation_id, intent_id, message)
        ),
    )

    result = execute_uisp_apply(
        str(operation.id), str(intent.id), adapter=SimpleNamespace(apply=apply)
    )

    assert result["outcome"] == "pending_readback"
    assert recovery_calls == [
        (
            str(operation.id),
            str(intent.id),
            "UISP write/readback completed but atomic audit persistence failed: "
            "commit failed",
        )
    ]


def test_post_write_readback_error_is_recoverable(
    db_session, subscriber, catalog_offer, monkeypatch
):
    intent, operation = _records(db_session, subscriber, catalog_offer)
    _use_session(monkeypatch, db_session)
    recovery_calls = []
    monkeypatch.setattr(
        "app.tasks.uisp_control._mark_pending_readback",
        lambda db, operation_id, intent_id, message: recovery_calls.append(
            (operation_id, intent_id, message)
        ),
    )

    def readback_failed(db, item):
        raise UispPostWriteReadbackError("write accepted; readback unavailable")

    result = execute_uisp_apply(
        str(operation.id),
        str(intent.id),
        adapter=SimpleNamespace(apply=readback_failed),
    )

    assert result["outcome"] == "pending_readback"
    assert recovery_calls == [
        (
            str(operation.id),
            str(intent.id),
            "write accepted; readback unavailable",
        )
    ]


def test_pending_readback_recovery_uses_a_fresh_transaction(monkeypatch):
    current_db = SimpleNamespace(rollback=lambda: None)
    intent = SimpleNamespace(status=UispIntentStatus.applying, last_error=None)
    operation = SimpleNamespace(status=NetworkOperationStatus.running)
    calls = []

    class RecoverySession:
        def get(self, model, object_id):
            assert model is UispDeviceIntent
            assert object_id == "intent-1"
            return intent

        def commit(self):
            calls.append("commit")

        def rollback(self):
            calls.append("rollback")

        def close(self):
            calls.append("close")

    recovery_db = RecoverySession()
    monkeypatch.setattr(
        "app.tasks.uisp_control.db_session_adapter.create_session",
        lambda: recovery_db,
    )
    monkeypatch.setattr(
        "app.tasks.uisp_control.network_operations.get",
        lambda db, operation_id: operation,
    )

    def mark_warning(db, operation_id, message, *, output_payload):
        calls.append((operation_id, message, output_payload))
        operation.status = NetworkOperationStatus.warning

    monkeypatch.setattr(
        "app.tasks.uisp_control.network_operations.mark_warning", mark_warning
    )

    _mark_pending_readback(current_db, "operation-1", "intent-1", "audit failed")

    assert intent.status == UispIntentStatus.pending_readback
    assert intent.last_error == "audit failed"
    assert operation.status == NetworkOperationStatus.warning
    assert calls == [
        (
            "operation-1",
            "audit failed",
            {
                "outcome": "pending_readback",
                "verified": False,
                "write_may_have_applied": True,
            },
        ),
        "commit",
        "close",
    ]


def test_reconciler_materializes_snapshot_before_verifying_pending_readback(
    db_session, subscriber, catalog_offer, monkeypatch
):
    intent, _operation = _records(db_session, subscriber, catalog_offer)
    intent.status = UispIntentStatus.pending_readback
    db_session.commit()
    _use_session(monkeypatch, db_session)
    monkeypatch.setattr("app.services.uisp.uisp_configured", lambda: True)
    monkeypatch.setattr(
        "app.tasks.uisp_control.UispClient.from_env", lambda: SimpleNamespace()
    )
    adapter = SimpleNamespace(
        readback=lambda db, item: UispApplyResult(
            outcome="verified",
            message="matched",
            write_accepted=False,
            verified=True,
            attempts=1,
            observed_config={"wifi.ssid": "Customer"},
        )
    )
    monkeypatch.setattr(
        "app.tasks.uisp_control.UispConfigurationWriteAdapter",
        lambda *args, **kwargs: adapter,
    )

    stats = reconcile_uisp_config_readback(max_intents=25)

    db_session.refresh(intent)
    snapshot = (
        db_session.query(UispConfigSnapshot)
        .filter(UispConfigSnapshot.source == UispSnapshotSource.observed)
        .one()
    )
    assert stats["checked"] == 1
    assert stats["verified"] == 1
    assert intent.status == UispIntentStatus.verified
    assert intent.verified_revision == intent.desired_revision
    assert snapshot.revision == intent.desired_revision
    assert snapshot.config == {"wifi.ssid": "Customer"}


def test_task_marks_model_unsupported_warning(
    db_session, subscriber, catalog_offer, monkeypatch
):
    intent, operation = _records(db_session, subscriber, catalog_offer)
    _use_session(monkeypatch, db_session)

    def unsupported(db, item):
        raise UispWriteUnsupported("setServices is not implemented")

    result = execute_uisp_apply(
        str(operation.id),
        str(intent.id),
        adapter=SimpleNamespace(apply=unsupported),
    )

    db_session.refresh(intent)
    db_session.refresh(operation)
    assert result["success"] is False
    assert intent.status == UispIntentStatus.manual_required
    assert operation.status == NetworkOperationStatus.warning


def test_uisp_apply_uses_default_queue_and_reconcile_uses_ingestion():
    from app.celery_app import celery_app

    assert "app.tasks.uisp_control.apply_uisp_intent" not in celery_app.conf.task_routes
    assert celery_app.conf.task_routes[
        "app.tasks.uisp_control.reconcile_uisp_config_readback"
    ] == {"queue": "ingestion"}
