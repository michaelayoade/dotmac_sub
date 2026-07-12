from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import CPEDevice, DeviceType
from app.models.network_operation import NetworkOperationStatus
from app.models.uisp_control import UispIntentStatus, UispIntentTargetType
from app.services.uisp_control_plane import request_apply, stage_intent
from app.services.uisp_write_adapter import UispApplyResult, UispWriteUnsupported
from app.tasks.uisp_control import execute_uisp_apply


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
    db_session.add_all([subscription, cpe])
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


def test_uisp_control_tasks_use_ingestion_queue():
    from app.celery_app import celery_app

    assert celery_app.conf.task_routes["app.tasks.uisp_control.apply_uisp_intent"] == {
        "queue": "ingestion"
    }
    assert celery_app.conf.task_routes[
        "app.tasks.uisp_control.reconcile_uisp_config_readback"
    ] == {"queue": "ingestion"}
