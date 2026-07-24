"""Tests for the assigned_at backfill reconciler on the ONT-assignment owner."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.models.network import OntAssignment, OntUnit
from app.services.network.ont_assignment_commands import (
    preview_assigned_at_drift,
    reconcile_assigned_at,
)

_CREATED = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _ont(db):
    ont = OntUnit(serial_number=f"ONT-{uuid.uuid4().hex[:10]}")
    db.add(ont)
    db.flush()
    return ont


def _assignment(db, **kw):
    a = OntAssignment(ont_unit_id=_ont(db).id, **kw)
    db.add(a)
    return a


def test_preview_only_active_missing_assigned_at(db_session):
    missing = _assignment(
        db_session, active=True, assigned_at=None, created_at=_CREATED
    )
    has = _assignment(
        db_session,
        active=True,
        assigned_at=datetime(2026, 2, 2, tzinfo=UTC),
        created_at=_CREATED,
    )
    inactive = _assignment(
        db_session, active=False, assigned_at=None, created_at=_CREATED
    )
    db_session.commit()

    ids = {d.assignment_id for d in preview_assigned_at_drift(db_session)}
    assert str(missing.id) in ids
    assert str(has.id) not in ids
    assert str(inactive.id) not in ids


def test_apply_backfills_from_created_at_and_is_idempotent(db_session):
    missing = _assignment(
        db_session, active=True, assigned_at=None, created_at=_CREATED
    )
    db_session.commit()

    result = reconcile_assigned_at(db_session, apply=True)
    assert result["backfilled"] == 1
    db_session.refresh(missing)
    # Backfilled from created_at (not now()); compare against the same stored
    # value so the assertion is robust to SQLite dropping tzinfo on round-trip.
    assert missing.assigned_at is not None
    assert missing.assigned_at == missing.created_at

    again = reconcile_assigned_at(db_session, apply=True)
    assert again["backfilled"] == 0


def test_preview_is_read_only(db_session):
    missing = _assignment(
        db_session, active=True, assigned_at=None, created_at=_CREATED
    )
    db_session.commit()

    preview_assigned_at_drift(db_session)
    db_session.refresh(missing)
    assert missing.assigned_at is None
