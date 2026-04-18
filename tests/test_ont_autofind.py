from __future__ import annotations

from types import SimpleNamespace

from app.models.network import OLTDevice, OntUnit
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

    ok, _msg, stats = autofind_service.sync_olt_autofind_candidates(
        db_session, str(olt.id)
    )
    assert ok is True
    assert stats["created"] == 1

    item = db_session.query(OltAutofindCandidate).one()
    assert item.is_active is True
    assert item.serial_number == "HWTC7D4806C3"

    monkeypatch.setattr(
        "app.services.network.olt_ssh.get_autofind_onts",
        lambda _olt: (True, "Found 0 unregistered ONTs", []),
    )
    ok, _msg, stats = autofind_service.sync_olt_autofind_candidates(
        db_session, str(olt.id)
    )
    assert ok is True
    assert stats["resolved"] == 1

    db_session.refresh(item)
    assert item.is_active is False
    assert item.resolution_reason == "disappeared"
    assert item.resolved_at is not None


def test_sync_olt_autofind_candidates_reactivates_disappeared_entry(
    db_session, monkeypatch
):
    olt = OLTDevice(name="OLT-Reappeared", mgmt_ip="198.51.100.203", is_active=True)
    db_session.add(olt)
    db_session.commit()

    item = OltAutofindCandidate(
        olt_id=olt.id,
        fsp="0/2/1",
        serial_number="HWTC-7D4806C3",
        is_active=False,
        resolution_reason="disappeared",
    )
    db_session.add(item)
    db_session.commit()

    entries = [
        SimpleNamespace(
            fsp="0/2/1",
            serial_number="HWTC7D4806C3",
            serial_hex="485754437D4806C3",
            vendor_id="HWTC",
            model="EG8145V5",
            software_version="V1",
            mac="E0:37:68:80:50:11",
            equipment_sn="EQ-1",
            autofind_time="2026-03-23 12:05",
        )
    ]
    monkeypatch.setattr(
        "app.services.network.olt_ssh.get_autofind_onts",
        lambda _olt: (True, "Found 1 unregistered ONT", entries),
    )

    ok, _msg, stats = autofind_service.sync_olt_autofind_candidates(
        db_session, str(olt.id)
    )

    assert ok is True
    assert stats["created"] == 0
    assert stats["updated"] == 1
    assert db_session.query(OltAutofindCandidate).count() == 1
    db_session.refresh(item)
    assert item.is_active is True
    assert item.resolution_reason is None
    assert item.resolved_at is None
    assert item.serial_number == "HWTC7D4806C3"


def test_sync_olt_autofind_candidates_uses_hex_when_display_serial_blank(
    db_session, monkeypatch
):
    olt = OLTDevice(name="OLT-Hex-Reappeared", mgmt_ip="198.51.100.205", is_active=True)
    ont = OntUnit(serial_number="48575443348F8A84", is_active=True)
    db_session.add_all([olt, ont])
    db_session.commit()

    item = OltAutofindCandidate(
        olt_id=olt.id,
        ont_unit_id=ont.id,
        fsp="0/1/13",
        serial_number="48575443348F8A84",
        serial_hex="48575443348F8A84",
        is_active=False,
        resolution_reason="disappeared",
    )
    db_session.add(item)
    db_session.commit()

    entries = [
        SimpleNamespace(
            fsp="0/1/13",
            serial_number="",
            serial_hex="48575443348F8A84",
            vendor_id="HWTC",
            model="EG8145V5",
            software_version="V5R019C00S050",
            mac="",
            equipment_sn="",
            autofind_time="",
        )
    ]
    monkeypatch.setattr(
        "app.services.network.olt_ssh.get_autofind_onts",
        lambda _olt: (True, "Found 1 unregistered ONT", entries),
    )

    ok, _msg, stats = autofind_service.sync_olt_autofind_candidates(
        db_session, str(olt.id)
    )

    assert ok is True
    assert stats["created"] == 0
    assert stats["updated"] == 1
    assert db_session.query(OltAutofindCandidate).count() == 1
    db_session.refresh(item)
    assert item.is_active is True
    assert item.resolution_reason is None
    assert item.resolved_at is None
    assert item.serial_number == "48575443348F8A84"
    assert item.serial_hex == "48575443348F8A84"
    assert item.ont_unit_id == ont.id


def test_sync_olt_autofind_candidates_prefers_exact_serial_on_duplicate_variants(
    db_session, monkeypatch
):
    olt = OLTDevice(
        name="OLT-Duplicate-Serials", mgmt_ip="198.51.100.207", is_active=True
    )
    db_session.add(olt)
    db_session.commit()

    hyphenated = OltAutofindCandidate(
        olt_id=olt.id,
        fsp="0/2/11",
        serial_number="HWTC-0EB23F9B",
        serial_hex="485754430EB23F9B",
        is_active=False,
        resolution_reason="disappeared",
    )
    compact = OltAutofindCandidate(
        olt_id=olt.id,
        fsp="0/2/11",
        serial_number="HWTC0EB23F9B",
        serial_hex="485754430EB23F9B",
        is_active=False,
        resolution_reason="disappeared",
    )
    db_session.add_all([hyphenated, compact])
    db_session.commit()

    entries = [
        SimpleNamespace(
            fsp="0/2/11",
            serial_number="HWTC-0EB23F9B",
            serial_hex="485754430EB23F9B",
            vendor_id="HWTC",
            model="HG8546M",
            software_version="V1",
            mac="-",
            equipment_sn="-",
            autofind_time="2026-04-17 11:27",
        )
    ]
    monkeypatch.setattr(
        "app.services.network.olt_ssh.get_autofind_onts",
        lambda _olt: (True, "Found 1 unregistered ONT", entries),
    )

    ok, _msg, stats = autofind_service.sync_olt_autofind_candidates(
        db_session, str(olt.id)
    )

    assert ok is True
    assert stats["created"] == 0
    assert stats["updated"] == 1
    db_session.refresh(hyphenated)
    db_session.refresh(compact)
    assert hyphenated.is_active is True
    assert hyphenated.resolution_reason is None
    assert compact.is_active is False
    assert db_session.query(OltAutofindCandidate).count() == 2


def test_sync_olt_autofind_candidates_skips_unmatchable_serial(db_session, monkeypatch):
    olt = OLTDevice(name="OLT-Malformed", mgmt_ip="198.51.100.206", is_active=True)
    db_session.add(olt)
    db_session.commit()

    entries = [
        SimpleNamespace(
            fsp="0/1/1",
            serial_number="---",
            serial_hex="",
            vendor_id="",
            model="",
            software_version="",
            mac="",
            equipment_sn="",
            autofind_time="",
        )
    ]
    monkeypatch.setattr(
        "app.services.network.olt_ssh.get_autofind_onts",
        lambda _olt: (True, "Found 1 unregistered ONT", entries),
    )

    ok, _msg, stats = autofind_service.sync_olt_autofind_candidates(
        db_session, str(olt.id)
    )

    assert ok is True
    assert stats["discovered"] == 1
    assert stats["created"] == 0
    assert db_session.query(OltAutofindCandidate).count() == 0


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


def test_restore_candidate_clears_disappeared_state_for_authorization(db_session):
    from app.services.network.olt_authorization_workflow import (
        get_autofind_candidate_by_serial,
    )

    olt = OLTDevice(name="OLT-Restore", mgmt_ip="198.51.100.204", is_active=True)
    ont = OntUnit(serial_number="HWTC22222222", is_active=True)
    db_session.add_all([olt, ont])
    db_session.commit()

    item = OltAutofindCandidate(
        olt_id=olt.id,
        fsp="0/2/6",
        serial_number="HWTC22222222",
        is_active=False,
        resolution_reason="disappeared",
    )
    db_session.add(item)
    db_session.commit()

    ok, message = autofind_service.restore_candidate(
        db_session, candidate_id=str(item.id)
    )

    assert ok is True
    assert "Restored autofind candidate" in message
    db_session.refresh(item)
    assert item.is_active is True
    assert item.resolution_reason is None
    assert item.resolved_at is None
    assert item.ont_unit_id == ont.id
    assert (
        get_autofind_candidate_by_serial(
            db_session,
            str(olt.id),
            "HWTC22222222",
            fsp="0/2/6",
        )
        is not None
    )


def test_build_unconfigured_onts_page_data_supports_history_filters(db_session):
    olt = OLTDevice(name="OLT-History", mgmt_ip="198.51.100.202", is_active=True)
    db_session.add(olt)
    db_session.commit()

    active_item = OltAutofindCandidate(
        olt_id=olt.id,
        fsp="0/2/3",
        serial_number="ACTIVE-ONT",
        is_active=True,
    )
    authorized_item = OltAutofindCandidate(
        olt_id=olt.id,
        fsp="0/2/4",
        serial_number="AUTH-ONT",
        is_active=False,
        resolution_reason="authorized",
    )
    disappeared_item = OltAutofindCandidate(
        olt_id=olt.id,
        fsp="0/2/5",
        serial_number="DISC-ONT",
        is_active=False,
        resolution_reason="disappeared",
    )
    db_session.add_all([active_item, authorized_item, disappeared_item])
    db_session.commit()

    history_data = autofind_service.build_unconfigured_onts_page_data(
        db_session,
        view="history",
    )
    assert history_data["selected_view"] == "history"
    assert {entry["serial_number"] for entry in history_data["entries"]} == {
        "AUTH-ONT",
        "DISC-ONT",
    }
    assert history_data["stats"]["history_candidates"] == 2

    disappeared_data = autofind_service.build_unconfigured_onts_page_data(
        db_session,
        view="history",
        resolution="disappeared",
    )
    assert [entry["serial_number"] for entry in disappeared_data["entries"]] == [
        "DISC-ONT"
    ]


def test_build_unconfigured_onts_page_data_searches_hex_serial_variants(db_session):
    olt = OLTDevice(name="OLT-Hex-Search", mgmt_ip="198.51.100.208", is_active=True)
    db_session.add(olt)
    db_session.commit()

    item = OltAutofindCandidate(
        olt_id=olt.id,
        fsp="0/2/15",
        serial_number="HWTC-C044CD9A",
        serial_hex="48575443C044CD9A",
        is_active=True,
    )
    db_session.add(item)
    db_session.commit()

    hex_data = autofind_service.build_unconfigured_onts_page_data(
        db_session,
        search="48575443C044CD9A",
    )
    assert [entry["serial_number"] for entry in hex_data["entries"]] == [
        "HWTC-C044CD9A"
    ]
    assert hex_data["entries"][0]["serial_hex"] == "48575443C044CD9A"

    display_data = autofind_service.build_unconfigured_onts_page_data(
        db_session,
        search="HWTCC044CD9A",
    )
    assert [entry["serial_number"] for entry in display_data["entries"]] == [
        "HWTC-C044CD9A"
    ]
