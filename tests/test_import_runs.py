"""DB-backed bulk-import orchestrator: dry-run/apply, per-row records, idempotency."""

from __future__ import annotations

import pytest

from app.models.imports import ImportRowStatus, ImportRunStatus
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
