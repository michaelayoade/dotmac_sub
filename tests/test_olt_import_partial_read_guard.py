"""OLT import must not retire live ONTs on a partial read (review task #18).

A flaky/partial SSH enumeration leaves the "seen" set missing live ONTs;
deactivating everything-not-seen would silently retire them. The destructive
sweep is now gated on the read being complete.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.models.network import OLTDevice, OltOntRegistration
from app.services.network.olt_state_import import _deactivate_unseen_registrations


def _olt(db):
    olt = OLTDevice(name="OLT-IMP", mgmt_ip="172.20.1.1", is_active=True)
    db.add(olt)
    db.flush()
    return olt


def _reg(db, olt, fsp, ont_id):
    reg = OltOntRegistration(
        olt_id=olt.id,
        fsp=fsp,
        ont_id_on_olt=ont_id,
        serial_number=f"SN{fsp}{ont_id}",
        is_active=True,
    )
    db.add(reg)
    db.flush()
    return reg


def test_complete_read_deactivates_unseen(db_session):
    olt = _olt(db_session)
    seen = _reg(db_session, olt, "0/1", 1)
    unseen = _reg(db_session, olt, "0/2", 2)

    n = _deactivate_unseen_registrations(
        db_session,
        olt.id,
        {("0/1", 1)},  # only the first was seen
        datetime.now(UTC),
        read_complete=True,
    )
    assert n == 1
    # Helper mutates in-memory (caller flushes); assert on the objects directly.
    assert seen.is_active is True
    assert unseen.is_active is False


def test_partial_read_deactivates_nothing(db_session):
    olt = _olt(db_session)
    seen = _reg(db_session, olt, "0/1", 1)
    unseen = _reg(db_session, olt, "0/2", 2)  # on a board the read skipped

    n = _deactivate_unseen_registrations(
        db_session,
        olt.id,
        {("0/1", 1)},
        datetime.now(UTC),
        read_complete=False,  # a board failed to enumerate
    )
    assert n == -1  # skipped
    db_session.refresh(seen)
    db_session.refresh(unseen)
    assert seen.is_active is True
    assert unseen.is_active is True  # NOT retired despite not being seen
