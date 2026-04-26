from __future__ import annotations

from datetime import UTC, datetime

from app.models.network import OLTDevice, OntProvisioningStatus, OntUnit
from app.models.ont_autofind import OltAutofindCandidate
from app.tasks.ont_authorization import can_auto_normalize_wan_after_authorization


def _new_autofind_authorized_ont(db_session) -> OntUnit:
    olt = OLTDevice(name="OLT-Auto-Normalize", mgmt_ip="198.51.100.222", is_active=True)
    ont = OntUnit(
        serial_number="HWTCNEWNORMALIZE",
        olt_device=olt,
        is_active=True,
        provisioning_status=OntProvisioningStatus.unprovisioned,
    )
    db_session.add_all([olt, ont])
    db_session.flush()
    db_session.add(
        OltAutofindCandidate(
            olt_id=olt.id,
            ont_unit_id=ont.id,
            fsp="0/1/1",
            serial_number=ont.serial_number,
            is_active=False,
            resolution_reason="authorized",
            resolved_at=datetime.now(UTC),
        )
    )
    db_session.commit()
    db_session.refresh(ont)
    return ont


def test_auto_wan_normalization_allowed_for_new_autofind_authorized_ont(db_session):
    ont = _new_autofind_authorized_ont(db_session)

    assert can_auto_normalize_wan_after_authorization(db_session, ont) is True


def test_auto_wan_normalization_denied_when_no_autofind_authorization(db_session):
    ont = OntUnit(
        serial_number="HWTCEEXISTING",
        is_active=True,
        provisioning_status=OntProvisioningStatus.unprovisioned,
    )
    db_session.add(ont)
    db_session.commit()
    db_session.refresh(ont)

    assert can_auto_normalize_wan_after_authorization(db_session, ont) is False


def test_auto_wan_normalization_denied_when_ont_has_service_history(db_session):
    ont = _new_autofind_authorized_ont(db_session)
    ont.observed_wan_ip = "203.0.113.10"
    db_session.commit()
    db_session.refresh(ont)

    assert can_auto_normalize_wan_after_authorization(db_session, ont) is False

