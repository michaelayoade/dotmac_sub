"""Tests for the OLT is_active drift reconciler.

Confirms the auditable owner path repairs UISP OLTs left is_active=false while
status=active, leaves TR069 OLTs with an incomplete config pack blocked, and is
idempotent.
"""

from __future__ import annotations

from app.models.network import DeviceStatus, OLTDevice
from app.services.network.olt_lifecycle import (
    preview_active_drift,
    reconcile_active_flag,
)


def _olt(**kw) -> OLTDevice:
    kw.setdefault("status", DeviceStatus.active)
    return OLTDevice(**kw)


def test_preview_flags_reconcilable_and_blocked(db_session):
    uisp = _olt(
        name="GPON-GUDU-1", is_active=False, uisp_device_id="uf-1", config_pack=None
    )
    tr069 = _olt(
        name="Gudu Huawei OLT", is_active=False, uisp_device_id=None, config_pack=None
    )
    already = _olt(name="Garki Huawei OLT", is_active=True, uisp_device_id="uf-2")
    db_session.add_all([uisp, tr069, already])
    db_session.commit()

    by_name = {d.name: d for d in preview_active_drift(db_session)}
    assert by_name["GPON-GUDU-1"].can_activate is True
    assert by_name["GPON-GUDU-1"].uisp_managed is True
    assert by_name["Gudu Huawei OLT"].can_activate is False
    assert "Garki Huawei OLT" not in by_name  # already active, not drift


def test_reconcile_applies_only_reconcilable_and_is_idempotent(db_session):
    uisp = _olt(
        name="GPON-JABI-1", is_active=False, uisp_device_id="uf-3", config_pack=None
    )
    tr069 = _olt(
        name="Jabi Huawei OLT", is_active=False, uisp_device_id=None, config_pack=None
    )
    db_session.add_all([uisp, tr069])
    db_session.commit()

    result = reconcile_active_flag(db_session, apply=True)
    assert result["activated"] == 1
    assert len(result["blocked"]) == 1
    db_session.refresh(uisp)
    db_session.refresh(tr069)
    assert uisp.is_active is True
    assert tr069.is_active is False

    # Re-running repairs nothing.
    again = reconcile_active_flag(db_session, apply=True)
    assert again["activated"] == 0


def test_preview_is_read_only(db_session):
    uisp = _olt(
        name="GPON-SPDC-1", is_active=False, uisp_device_id="uf-4", config_pack=None
    )
    db_session.add(uisp)
    db_session.commit()

    preview_active_drift(db_session)
    db_session.refresh(uisp)
    assert uisp.is_active is False
