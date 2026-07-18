from __future__ import annotations

import uuid
from dataclasses import asdict

from app.models.network import OLTDevice, OntAssignment, OntUnit, PonPort
from scripts.network import import_smartolt_unconfigured as smartolt_audit


class _BorrowedSession:
    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, exc_type, exc, traceback):
        return False


def test_csv_loader_retains_only_credential_presence(tmp_path):
    source = tmp_path / "smartolt.csv"
    source.write_text(
        "SN,OLT,Board,Port,Username,Password\n"
        "HWTC1234,OLT-A,1,2,customer-user,super-secret-value\n",
        encoding="utf-8",
    )

    rows = smartolt_audit._load_csv(source)

    assert len(rows) == 1
    assert rows[0].username_present is True
    assert rows[0].password_present is True
    assert "super-secret-value" not in repr(rows[0])
    assert "customer-user" not in repr(rows[0])
    assert "password" not in asdict(rows[0])
    assert "username" not in asdict(rows[0])


def test_smartolt_audit_uses_exact_existing_edges_without_writing(
    db_session,
    monkeypatch,
    subscription,
):
    olt = OLTDevice(
        name=f"SmartOLT Exact {uuid.uuid4().hex[:8]}",
        hostname=f"smartolt-{uuid.uuid4().hex[:8]}",
        is_active=True,
    )
    db_session.add(olt)
    db_session.flush()
    pon = PonPort(olt_id=olt.id, name="0/1/2", is_active=True)
    db_session.add(pon)
    db_session.flush()
    ont = OntUnit(
        serial_number=f"HWTC{uuid.uuid4().hex[:12].upper()}",
        olt_device_id=olt.id,
        pon_port_id=pon.id,
        is_active=True,
    )
    db_session.add(ont)
    db_session.flush()
    assignment = OntAssignment(
        ont_unit_id=ont.id,
        subscription_id=subscription.id,
        subscriber_id=subscription.subscriber_id,
        pon_port_id=pon.id,
        active=True,
    )
    db_session.add(assignment)
    db_session.commit()
    monkeypatch.setattr(
        smartolt_audit,
        "SessionLocal",
        lambda: _BorrowedSession(db_session),
    )
    observation = smartolt_audit.CsvObservation(
        row_number=2,
        serial_number=ont.serial_number,
        olt_name=olt.name,
        observed_fsp="0/1/2",
        username_present=True,
        password_present=True,
    )

    result = smartolt_audit._audit([observation])[0]
    db_session.refresh(ont)
    db_session.refresh(assignment)

    assert result.status == "confirmed"
    assert result.ont_unit_id == str(ont.id)
    assert result.observed_olt_id == str(olt.id)
    assert result.observed_pon_port_id == str(pon.id)
    assert result.active_assignment_ids == (str(assignment.id),)
    assert len(result.observation_sha256) == 64
    assert ont.olt_device_id == olt.id
    assert ont.pon_port_id == pon.id
    assert assignment.pon_port_id == pon.id
    assert assignment.active is True
