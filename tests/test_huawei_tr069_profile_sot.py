"""Focused Huawei OLT TR-069 profile SOT tests."""

from __future__ import annotations

from types import SimpleNamespace

from app.models.network import OLTDevice, OntUnit
from app.services.network.olt_ssh_ont.status import parse_ont_info_detail
from app.services.network.ont_profile_reconcile import (
    reconcile_tr069_profile_binding,
)


def test_profile_override_is_mapped_only_on_ont_inventory():
    assert "desired_tr069_profile_id" in OntUnit.__table__.c
    assert "desired_tr069_profile_id" not in OLTDevice.__table__.c


def test_olt_detail_parser_reads_bound_tr069_profile():
    parsed = parse_ont_info_detail(
        """
        Description              : Customer ONT
        Line profile ID          : 40
        Service profile ID       : 41
        TR069 server profile ID  : 5
        """
    )

    assert parsed["tr069_profile_id"] == 5


def test_profile_service_surfaces_reconcile_failure(db_session, monkeypatch):
    monkeypatch.setattr(
        "app.services.network.reconcile.reconcile_ont",
        lambda *args, **kwargs: SimpleNamespace(
            success=False,
            failure=SimpleNamespace(message="OLT profile readback mismatch"),
        ),
    )

    ok, message = reconcile_tr069_profile_binding(db_session, "ont-1", 5)

    assert ok is False
    assert message == "OLT profile readback mismatch"


def test_profile_service_queues_bootstrap_only_after_verified_reconcile(
    db_session, monkeypatch
):
    monkeypatch.setattr(
        "app.services.network.reconcile.reconcile_ont",
        lambda *args, **kwargs: SimpleNamespace(success=True, failure=None),
    )
    monkeypatch.setattr(
        "app.services.network.ont_provision_steps.wait_tr069_bootstrap",
        lambda db, ont_id: SimpleNamespace(message="Waiting for ACS inform."),
    )

    ok, message = reconcile_tr069_profile_binding(db_session, "ont-1", 5)

    assert ok is True
    assert "verified on OLT readback" in message
    assert "Waiting for ACS inform" in message
