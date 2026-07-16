"""DB-backed bulk-import orchestrator: dry-run/apply, per-row records, idempotency."""

from __future__ import annotations

import pytest

from app.models.billing import LedgerEntry, Payment
from app.models.imports import ImportRowStatus, ImportRun, ImportRunStatus
from app.models.network import IpPool
from app.services import import_runs

_CSV = (
    "name,ip_version,cidr\n"
    "Pool A,ipv4,10.10.0.0/24\n"
    "Bad Pool,notaversion,10.11.0.0/24\n"  # invalid ip_version -> validation error
)


def _pool_count(db, name):
    return db.query(IpPool).filter(IpPool.name == name).count()


def test_dry_run_validates_without_persisting(db_session):
    run = import_runs.create_import_run(
        db_session, module="ip_pools", raw_text=_CSV, dry_run=True
    )
    run = import_runs.process_import_run(db_session, run.id)

    assert run.status == ImportRunStatus.dry_run_ready
    assert run.total_rows == 2
    assert run.ok_rows == 1
    assert run.failed_rows == 1

    rows = sorted(run.rows, key=lambda r: r.row_number)
    assert rows[0].status == ImportRowStatus.ok
    assert rows[1].status == ImportRowStatus.error
    assert rows[1].error_message  # carries the validation detail

    # Dry-run persists nothing.
    assert _pool_count(db_session, "Pool A") == 0


def test_apply_persists_only_valid_rows(db_session):
    run = import_runs.create_import_run(
        db_session, module="ip_pools", raw_text=_CSV, dry_run=False
    )
    run = import_runs.process_import_run(db_session, run.id)

    assert run.status == ImportRunStatus.completed
    assert run.ok_rows == 1
    assert run.failed_rows == 1
    assert _pool_count(db_session, "Pool A") == 1
    assert _pool_count(db_session, "Bad Pool") == 0

    ok_row = next(r for r in run.rows if r.status == ImportRowStatus.ok)
    assert ok_row.result and ok_row.result.get("id")  # records the created id


def test_reprocess_is_idempotent(db_session):
    csv = "name,ip_version,cidr\nPool Z,ipv4,10.20.0.0/24\n"
    run = import_runs.create_import_run(
        db_session, module="ip_pools", raw_text=csv, dry_run=False
    )
    import_runs.process_import_run(db_session, run.id)
    # Re-processing a completed run is a no-op (does not double-import).
    run2 = import_runs.process_import_run(db_session, run.id)
    assert run2.status == ImportRunStatus.completed
    assert _pool_count(db_session, "Pool Z") == 1


def test_unsupported_module_rejected(db_session):
    with pytest.raises(ValueError):
        import_runs.create_import_run(
            db_session, module="not_a_module", raw_text="x", dry_run=True
        )


def test_apply_from_dry_run(db_session):
    csv = "name,ip_version,cidr\nPool DR,ipv4,10.30.0.0/24\n"
    dry = import_runs.create_import_run(
        db_session, module="ip_pools", raw_text=csv, dry_run=True
    )
    dry = import_runs.process_import_run(db_session, dry.id)
    assert dry.status == ImportRunStatus.dry_run_ready
    assert _pool_count(db_session, "Pool DR") == 0

    applied = import_runs.apply_from_dry_run(db_session, dry.id)
    assert applied.status == ImportRunStatus.completed
    assert applied.dry_run is False
    assert _pool_count(db_session, "Pool DR") == 1

    # Re-applying a now-completed run is rejected (only dry_run_ready can apply).
    with pytest.raises(ValueError):
        import_runs.apply_from_dry_run(db_session, applied.id)
    with pytest.raises(ValueError, match="already applied"):
        import_runs.apply_from_dry_run(db_session, dry.id)


def test_financial_apply_run_cannot_bypass_dry_run(db_session):
    with pytest.raises(ValueError, match="validated as a dry run"):
        import_runs.create_import_run(
            db_session,
            module="payments",
            raw_text="account_id,amount,external_id\n",
            dry_run=False,
        )

    bypass = ImportRun(
        module="payments",
        status=ImportRunStatus.pending,
        dry_run=False,
        data_format="csv",
        input_text="account_id,amount,external_id\n",
    )
    db_session.add(bypass)
    db_session.commit()
    with pytest.raises(ValueError, match="missing its validated source"):
        import_runs.process_import_run(db_session, bypass.id)


def test_repeated_payment_source_is_idempotent_across_runs(db_session, subscriber):
    csv = (
        "account_id,amount,status,external_id\n"
        f"{subscriber.id},25,succeeded,bank-import-001\n"
    )

    applied_runs = []
    for _ in range(2):
        dry = import_runs.create_import_run(
            db_session,
            module="payments",
            raw_text=csv,
            source_name="bank.csv",
            dry_run=True,
        )
        import_runs.process_import_run(db_session, dry.id)
        applied = import_runs.apply_from_dry_run(db_session, dry.id)
        assert applied.failed_rows == 0
        applied_runs.append(applied)

    payment = (
        db_session.query(Payment).filter(Payment.external_id == "bank-import-001").one()
    )
    assert (
        db_session.query(LedgerEntry)
        .filter(LedgerEntry.payment_id == payment.id)
        .count()
        == 1
    )
    first_row = applied_runs[0].rows[0]
    second_row = applied_runs[1].rows[0]
    assert first_row.record_created is True
    assert first_row.payment_id == payment.id
    assert second_row.record_created is False
    assert second_row.payment_id == payment.id
    assert payment.import_run_id == applied_runs[0].id


def test_ipv4_assignments_import_dry_run_then_apply(db_session):
    import uuid

    from app.models.network import IPAssignment, IpPool, IPVersion
    from app.models.subscriber import Subscriber

    pool = IpPool(
        id=uuid.uuid4(),
        name="Import Pool",
        ip_version=IPVersion.ipv4,
        cidr="10.50.0.0/24",
        is_active=True,
    )
    sub = Subscriber(
        first_name="A", last_name="L", email=f"{uuid.uuid4().hex[:8]}@e.com"
    )
    db_session.add_all([pool, sub])
    db_session.commit()
    csv = f"pool_id,ip_address,subscriber_id\n{pool.id},10.50.0.5,{sub.id}\n"

    # Dry-run validates, assigns nothing.
    run = import_runs.create_import_run(
        db_session, module="ipv4_assignments", raw_text=csv, dry_run=True
    )
    run = import_runs.process_import_run(db_session, run.id)
    assert run.status == ImportRunStatus.dry_run_ready
    assert run.ok_rows == 1
    assert db_session.query(IPAssignment).count() == 0

    # Apply creates the assignment.
    run2 = import_runs.create_import_run(
        db_session, module="ipv4_assignments", raw_text=csv, dry_run=False
    )
    run2 = import_runs.process_import_run(db_session, run2.id)
    assert run2.status == ImportRunStatus.completed
    assert run2.ok_rows == 1
    assignment = (
        db_session.query(IPAssignment)
        .filter(IPAssignment.subscriber_id == sub.id)
        .first()
    )
    assert assignment is not None and assignment.is_active
