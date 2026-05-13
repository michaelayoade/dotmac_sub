"""Tests for the GenieACS BOOTSTRAP webhook → reconcile_ont integration."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.reconcile_webhooks import (
    _serial_from_device_id,
)
from app.api.reconcile_webhooks import (
    router as reconcile_router,
)
from app.db import get_db
from app.models.network import OLTDevice, OntSyncStatus, OntUnit
from app.services.network.reconcile import (
    ReconcileFailure,
    ReconcileFailureReason,
    ReconcileResult,
)

# ── App fixture ─────────────────────────────────────────────────────────────


@pytest.fixture
def client(db_session):
    """A FastAPI client wired to the webhook router with the project's
    db_session fixture overriding ``get_db``."""
    app = FastAPI()
    app.include_router(reconcile_router, prefix="/api/v1")

    def _override_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = _override_db
    return TestClient(app)


@pytest.fixture
def olt_device(db_session):
    olt = OLTDevice(
        name="OLT-WEBHOOK-TEST",
        mgmt_ip="172.20.100.30",
        is_active=True,
    )
    db_session.add(olt)
    db_session.commit()
    db_session.refresh(olt)
    return olt


@pytest.fixture
def ont(db_session, olt_device):
    ont = OntUnit(
        serial_number="HWTC8535819A",
        olt_device_id=olt_device.id,
        board="0/1",
        port="3",
        external_id="11",
        is_active=True,
        sync_status=OntSyncStatus.synced,
    )
    db_session.add(ont)
    db_session.commit()
    db_session.refresh(ont)
    return ont


def _stub_result(success: bool, **overrides) -> ReconcileResult:
    defaults = dict(
        success=success,
        sync_status="synced" if success else "out_of_sync",
        actions_applied=(),
        drift_before=(),
        drift_after=(),
        observed_after=None,
        failure=(
            None
            if success
            else ReconcileFailure(
                reason=ReconcileFailureReason.ACS_CR_FAILED,
                message="empty CR creds",
            )
        ),
        duration_ms=10,
        reconciled_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    return ReconcileResult(**defaults)


# ── _serial_from_device_id ──────────────────────────────────────────────────


def test_serial_from_device_id_extracts_trailing_segment():
    assert _serial_from_device_id("00259E-HG8546M-HWTC8535819A") == "HWTC8535819A"


def test_serial_from_device_id_returns_none_on_malformed():
    assert _serial_from_device_id("") is None
    assert _serial_from_device_id("no-dash") == "dash"  # has a dash
    assert _serial_from_device_id("nodash") is None


# ── Webhook routing ─────────────────────────────────────────────────────────


def test_bootstrap_webhook_triggers_reconcile_for_known_serial(
    client, ont, monkeypatch
):
    captured: dict = {}

    def _fake_reconcile(db, ont_unit_id, *, proposed_change, mode, **_):
        captured["ont_unit_id"] = ont_unit_id
        captured["proposed_change"] = proposed_change
        captured["mode"] = mode
        return _stub_result(True)

    monkeypatch.setattr("app.api.reconcile_webhooks.reconcile_ont", _fake_reconcile)

    response = client.post(
        "/api/v1/reconcile/webhooks/genieacs/bootstrap",
        json={
            "device_id": f"00259E-HG8546M-{ont.serial_number}",
            "event": "0 BOOTSTRAP",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["sync_status"] == "synced"
    assert body["ont_unit_id"] == str(ont.id)
    # The webhook fires bootstrap mode with no proposed_change — the
    # planner does the full bring-up against current desired state.
    assert captured["mode"] == "bootstrap"
    assert captured["proposed_change"] is None


def test_bootstrap_webhook_returns_ignored_for_unknown_serial(client, monkeypatch):
    """GenieACS may inform about devices the inventory doesn't know yet
    (autofind not yet authorized). Return 200/ignored so GenieACS stops
    retrying."""
    monkeypatch.setattr(
        "app.api.reconcile_webhooks.reconcile_ont",
        lambda *a, **k: pytest.fail("reconcile should not run for unknown serials"),
    )

    response = client.post(
        "/api/v1/reconcile/webhooks/genieacs/bootstrap",
        json={
            "device_id": "00259E-HG8546M-HWTCUNKNOWN",
            "event": "0 BOOTSTRAP",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ignored"
    assert body["reason"] == "unknown_serial"


def test_bootstrap_webhook_returns_400_for_malformed_device_id(client):
    response = client.post(
        "/api/v1/reconcile/webhooks/genieacs/bootstrap",
        json={"device_id": "nodashes"},
    )
    assert response.status_code == 400
    assert "extract serial" in response.json()["detail"]


def test_bootstrap_webhook_surfaces_failure_with_actionable_for_cr_failed(
    client, ont, monkeypatch
):
    monkeypatch.setattr(
        "app.api.reconcile_webhooks.reconcile_ont",
        lambda *a, **k: _stub_result(False),
    )

    response = client.post(
        "/api/v1/reconcile/webhooks/genieacs/bootstrap",
        json={
            "device_id": f"00259E-HG8546M-{ont.serial_number}",
            "event": "0 BOOTSTRAP",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["sync_status"] == "out_of_sync"
    assert body["failure_reason"] == ReconcileFailureReason.ACS_CR_FAILED
    assert body["actionable"] is True


def test_bootstrap_webhook_validates_payload_shape(client):
    """Pydantic rejects malformed payloads with 422."""
    response = client.post(
        "/api/v1/reconcile/webhooks/genieacs/bootstrap",
        json={"unknown_field": "x"},
    )
    assert response.status_code == 422
