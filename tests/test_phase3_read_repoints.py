"""Permanent native work lifecycle and independent quote/referral cutovers."""

from __future__ import annotations

from uuid import uuid4

from app.api import me as me_api
from app.schemas.portal import MyProjectsResponse
from app.services import customer_experience_lifecycle
from app.services import referrals as referrals_service
from app.services.sales import selfserve as selfserve_service


def _principal() -> dict[str, str]:
    return {"principal_type": "subscriber", "subscriber_id": str(uuid4())}


def test_projects_have_no_read_flip_control():
    from app.services import control_registry

    keys = {control.key for control in control_registry.all_controls()}
    assert "projects.native_read" not in keys
    assert "crm.work_order_pull" not in keys


def test_me_projects_unconditionally_uses_native_lifecycle(monkeypatch):
    principal = _principal()
    expected = MyProjectsResponse()
    calls: list[str] = []

    def _read(db, subscriber_id):
        calls.append(subscriber_id)
        return expected

    monkeypatch.setattr(customer_experience_lifecycle, "projects_for_subscriber", _read)

    assert me_api.my_projects(db=None, principal=principal) is expected
    assert calls == [principal["subscriber_id"]]


def test_quote_read_control_remains_independent(db_session):
    assert selfserve_service.native_read_enabled(db_session) is False


def test_referral_reads_are_permanently_native(monkeypatch):
    principal = _principal()
    expected = {"code": "", "referrals": []}
    monkeypatch.setattr(
        referrals_service.referrals,
        "read_for_subscriber",
        lambda db, subscriber_id: {**expected, "subscriber_id": subscriber_id},
    )

    result = me_api.my_referrals(db=None, principal=principal)

    assert result["subscriber_id"] == principal["subscriber_id"]
