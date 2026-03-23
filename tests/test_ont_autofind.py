from __future__ import annotations

from types import SimpleNamespace

from app.models.network import OLTDevice
from app.models.ont_autofind import OltAutofindCandidate
from app.services import web_network_ont_autofind as autofind_service


def test_sync_olt_autofind_candidates_creates_and_expires(db_session, monkeypatch):
    olt = OLTDevice(name="OLT-Autofind", mgmt_ip="198.51.100.200", is_active=True)
    db_session.add(olt)
    db_session.commit()

    entries_round1 = [
        SimpleNamespace(
            fsp="0/2/1",
            serial_number="HWTC7D4806C3",
            serial_hex="485754437D4806C3",
            vendor_id="HWTC",
            model="EG8145V5",
            software_version="V1",
            mac="E0:37:68:80:50:11",
            equipment_sn="EQ-1",
            autofind_time="2026-03-23 12:00",
        )
    ]
    monkeypatch.setattr(
        "app.services.network.olt_ssh.get_autofind_onts",
        lambda _olt: (True, "Found 1 unregistered ONT", entries_round1),
    )

    ok, _msg, stats = autofind_service.sync_olt_autofind_candidates(db_session, str(olt.id))
    assert ok is True
    assert stats["created"] == 1

    item = db_session.query(OltAutofindCandidate).one()
    assert item.is_active is True
    assert item.serial_number == "HWTC7D4806C3"

    monkeypatch.setattr(
        "app.services.network.olt_ssh.get_autofind_onts",
        lambda _olt: (True, "Found 0 unregistered ONTs", []),
    )
    ok, _msg, stats = autofind_service.sync_olt_autofind_candidates(db_session, str(olt.id))
    assert ok is True
    assert stats["resolved"] == 1

    db_session.refresh(item)
    assert item.is_active is False
    assert item.resolution_reason == "disappeared"
    assert item.resolved_at is not None


def test_resolve_candidate_authorized_marks_entry_inactive(db_session):
    olt = OLTDevice(name="OLT-Resolve", mgmt_ip="198.51.100.201", is_active=True)
    db_session.add(olt)
    db_session.commit()

    item = OltAutofindCandidate(
        olt_id=olt.id,
        fsp="0/2/2",
        serial_number="HWTC11111111",
        is_active=True,
    )
    db_session.add(item)
    db_session.commit()

    autofind_service.resolve_candidate_authorized(
        db_session,
        olt_id=str(olt.id),
        fsp="0/2/2",
        serial_number="HWTC11111111",
    )

    db_session.refresh(item)
    assert item.is_active is False
    assert item.resolution_reason == "authorized"
    assert item.resolved_at is not None
