"""Tests for ``acquire_reconcile_lock``.

These tests run against the sqlite ``db_session`` fixture. SQLite ignores
``SELECT FOR UPDATE`` at the SQL level (it serializes at the connection
level instead), so concurrent-lock contention behavior is not testable here
— it's exercised against PostgreSQL in production runs. Everything else
(yield, status transition, crash recovery, missing row, caller-driven status
persistence) is unit-testable.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.models.network import OLTDevice, OntSyncStatus, OntUnit
from app.services.network.reconcile import (
    OntNotFound,
    acquire_reconcile_lock,
)

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def olt(db_session):
    olt = OLTDevice(
        name="OLT-LOCK-TEST",
        mgmt_ip="172.20.100.99",
        is_active=True,
    )
    db_session.add(olt)
    db_session.commit()
    db_session.refresh(olt)
    return olt


@pytest.fixture
def ont(db_session, olt):
    ont = OntUnit(
        serial_number="HWTCLOCK0001",
        olt_device_id=olt.id,
        board="0/1",
        port="3",
        external_id="11",
        is_active=True,
        desired_config={},
    )
    db_session.add(ont)
    db_session.commit()
    ont = db_session.get(OntUnit, ont.id)
    return ont


# ── Basic acquisition ───────────────────────────────────────────────────────


def test_acquire_yields_the_ont_unit(db_session, ont):
    with acquire_reconcile_lock(db_session, ont.id) as locked_ont:
        assert locked_ont.id == ont.id
        assert locked_ont.serial_number == ont.serial_number


def test_acquire_accepts_string_id(db_session, ont):
    with acquire_reconcile_lock(db_session, str(ont.id)) as locked_ont:
        assert locked_ont.id == ont.id


def test_acquire_raises_when_ont_does_not_exist(db_session):
    missing = uuid.uuid4()
    with pytest.raises(OntNotFound) as excinfo:
        with acquire_reconcile_lock(db_session, missing):
            pass
    assert str(missing) in str(excinfo.value)


def test_acquire_rejects_invalid_uuid_string(db_session):
    with pytest.raises(ValueError):
        with acquire_reconcile_lock(db_session, "not-a-uuid"):
            pass


# ── Crashed-prior detection ─────────────────────────────────────────────────


def test_crashed_prior_is_flipped_to_out_of_sync(db_session, ont):
    """If a previous reconcile left sync_status='reconciling' (crashed), the
    next acquire flips it to out_of_sync with a crash note. The lock then
    yields normally. We verify the state on the yielded instance (the same
    object the caller will write to before committing)."""
    prior_started = datetime.now(UTC) - timedelta(minutes=5)
    ont.sync_status = OntSyncStatus.reconciling
    ont.last_reconcile_started_at = prior_started
    ont.last_error = None
    db_session.flush()

    with acquire_reconcile_lock(db_session, ont.id) as locked_ont:
        assert locked_ont.sync_status == OntSyncStatus.out_of_sync
        assert "did not finalise" in (locked_ont.last_error or "")
        assert prior_started.isoformat() in (locked_ont.last_error or "")


def test_crashed_prior_with_no_started_at_records_unknown(db_session, ont):
    """If somehow sync_status='reconciling' without a started_at (shouldn't
    happen in practice but guard against it), the error message says 'unknown'
    rather than crashing."""
    ont.sync_status = OntSyncStatus.reconciling
    ont.last_reconcile_started_at = None
    db_session.commit()

    with acquire_reconcile_lock(db_session, ont.id) as locked_ont:
        assert locked_ont.sync_status == OntSyncStatus.out_of_sync
        assert "unknown" in (locked_ont.last_error or "")


def test_synced_status_passes_through_unchanged(db_session, ont):
    """An ONT in 'synced' state is yielded with the status untouched; the
    caller is responsible for transitioning to 'reconciling' if it wants to."""
    assert ont.sync_status == OntSyncStatus.synced
    with acquire_reconcile_lock(db_session, ont.id) as locked_ont:
        assert locked_ont.sync_status == OntSyncStatus.synced
        assert locked_ont.last_error is None


def test_out_of_sync_status_passes_through_unchanged(db_session, ont):
    """The lock doesn't gate on out_of_sync — mode-specific blocking lives in
    reconcile_ont, not here. Sweep mode needs to be able to acquire an
    out_of_sync ONT to attempt repair."""
    ont.sync_status = OntSyncStatus.out_of_sync
    ont.last_error = "previous failure"
    db_session.commit()

    with acquire_reconcile_lock(db_session, ont.id) as locked_ont:
        assert locked_ont.sync_status == OntSyncStatus.out_of_sync
        assert locked_ont.last_error == "previous failure"


# ── Caller-driven status persistence ────────────────────────────────────────


def test_caller_status_change_inside_context_persists_after_flush(db_session, ont):
    """The whole point of yielding the row is so callers can mutate it. Mutations
    are visible after flush (which is what the surrounding reconcile transaction
    does before commit)."""
    started = datetime.now(UTC)
    with acquire_reconcile_lock(db_session, ont.id) as locked_ont:
        locked_ont.sync_status = OntSyncStatus.reconciling
        locked_ont.last_reconcile_started_at = started
    db_session.flush()

    row = db_session.execute(
        text("SELECT sync_status FROM ont_units WHERE id = :id"),
        {"id": str(ont.id)},
    ).scalar_one()
    assert row == OntSyncStatus.reconciling.value


def test_caller_can_finalise_to_synced(db_session, ont):
    """End-to-end happy path: caller sets reconciling at entry, synced at exit."""
    now = datetime.now(UTC)
    with acquire_reconcile_lock(db_session, ont.id) as locked_ont:
        locked_ont.sync_status = OntSyncStatus.reconciling
        locked_ont.last_reconcile_started_at = now
        # ...do reconcile work...
        locked_ont.sync_status = OntSyncStatus.synced
        locked_ont.last_reconciled_at = now
        locked_ont.last_error = None
    db_session.flush()

    row = db_session.execute(
        text("SELECT sync_status, last_error FROM ont_units WHERE id = :id"),
        {"id": str(ont.id)},
    ).one()
    assert row[0] == OntSyncStatus.synced.value
    assert row[1] is None


def test_caller_can_finalise_to_out_of_sync(db_session, ont):
    """Failure path: caller sets out_of_sync with an error message."""
    with acquire_reconcile_lock(db_session, ont.id) as locked_ont:
        locked_ont.sync_status = OntSyncStatus.reconciling
        # ...reconcile fails...
        locked_ont.sync_status = OntSyncStatus.out_of_sync
        locked_ont.last_error = "ACS unreachable"
    db_session.flush()

    row = db_session.execute(
        text("SELECT sync_status, last_error FROM ont_units WHERE id = :id"),
        {"id": str(ont.id)},
    ).one()
    assert row[0] == OntSyncStatus.out_of_sync.value
    assert row[1] == "ACS unreachable"


# ── Exception propagation ──────────────────────────────────────────────────


def test_exception_inside_context_propagates(db_session, ont):
    """The lock doesn't suppress exceptions; it just provides the row.
    Transaction cleanup is the caller's job."""

    class _Boom(Exception):
        pass

    with pytest.raises(_Boom):
        with acquire_reconcile_lock(db_session, ont.id) as locked_ont:
            locked_ont.sync_status = OntSyncStatus.reconciling
            raise _Boom("forced")
