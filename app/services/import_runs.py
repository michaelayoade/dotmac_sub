"""DB-backed bulk-import orchestrator.

Wraps the mature ``web_system_import_wizard`` (parsing, per-entity validation,
per-row persistence) but records every run + row to the ``import_runs`` /
``import_run_rows`` tables instead of a settings-log — durable, queryable, and
scalable for large imports. Drives the dry-run -> apply split:

  * dry-run  : validate every row, record ok/error, change nothing.
  * apply    : validate + persist each row inside a SAVEPOINT, record the outcome.

``process_import_run`` is idempotent (only a ``pending`` run is processed) and
commits in chunks so progress is durable and a crash doesn't lose work.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.imports import (
    ImportRowStatus,
    ImportRun,
    ImportRunRow,
    ImportRunStatus,
)
from app.services.common import coerce_uuid

_CHUNK_COMMIT = 200


def supported_modules() -> dict:
    from app.services import web_system_import_wizard as wiz

    return wiz.ENTITY_CONFIG


def create_import_run(
    db: Session,
    *,
    module: str,
    raw_text: str,
    data_format: str = "csv",
    source_name: str | None = None,
    column_mapping: dict | None = None,
    csv_delimiter: str = ",",
    dry_run: bool = True,
    created_by: str | None = None,
) -> ImportRun:
    """Create a pending import run holding the input. Process it via
    ``process_import_run`` (inline or from the Celery task)."""
    if module not in supported_modules():
        raise ValueError(f"Unsupported import module: {module}")
    from app.services.financial_imports import FINANCIAL_IMPORT_MODULES

    if module in FINANCIAL_IMPORT_MODULES and not dry_run:
        raise ValueError(
            "Financial and subscription imports must be validated as a dry run "
            "and applied from that run"
        )
    return _create_import_run(
        db,
        module=module,
        raw_text=raw_text,
        data_format=data_format,
        source_name=source_name,
        column_mapping=column_mapping,
        csv_delimiter=csv_delimiter,
        dry_run=dry_run,
        created_by=created_by,
    )


def _create_import_run(
    db: Session,
    *,
    module: str,
    raw_text: str,
    data_format: str,
    source_name: str | None,
    column_mapping: dict | None,
    csv_delimiter: str,
    dry_run: bool,
    created_by: str | None,
    source_run_id=None,
) -> ImportRun:
    run = ImportRun(
        module=module,
        source_run_id=source_run_id,
        status=ImportRunStatus.pending,
        dry_run=dry_run,
        data_format=data_format,
        source_name=source_name,
        csv_delimiter=csv_delimiter or ",",
        column_mapping=column_mapping or None,
        input_text=raw_text,
        created_by=created_by,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def list_import_runs(db: Session, *, limit: int = 50) -> list[ImportRun]:
    return db.query(ImportRun).order_by(ImportRun.created_at.desc()).limit(limit).all()


def get_import_run(db: Session, run_id) -> ImportRun | None:
    return db.get(ImportRun, coerce_uuid(run_id))


def apply_from_dry_run(
    db: Session, run_id, *, created_by: str | None = None
) -> ImportRun:
    """Create and process an apply run from a dry-run-ready run's stored input.
    The dry-run record is preserved as the validation audit."""
    src = (
        db.query(ImportRun)
        .filter(ImportRun.id == coerce_uuid(run_id))
        .with_for_update()
        .one_or_none()
    )
    if src is None:
        raise ValueError("Import run not found")
    if src.status != ImportRunStatus.dry_run_ready:
        raise ValueError("Only a validated (dry-run) run can be applied.")
    if src.applied_run is not None:
        raise ValueError(f"Import run was already applied as {src.applied_run.id}")
    try:
        run = _create_import_run(
            db,
            module=src.module,
            raw_text=src.input_text or "",
            data_format=src.data_format,
            source_name=src.source_name,
            column_mapping=src.column_mapping,
            csv_delimiter=src.csv_delimiter,
            dry_run=False,
            created_by=created_by,
            source_run_id=src.id,
        )
    except IntegrityError as exc:
        db.rollback()
        raise ValueError("Import run has already been applied") from exc
    return process_import_run(db, run.id)


def _record_row(
    db: Session,
    run_id,
    row_number: int,
    raw: dict,
    status: ImportRowStatus,
    *,
    error: str | None = None,
    result: dict | None = None,
) -> None:
    db.add(
        ImportRunRow(
            run_id=run_id,
            row_number=row_number,
            raw={str(k): ("" if v is None else str(v)) for k, v in (raw or {}).items()},
            status=status,
            error_message=error,
            result=result,
        )
    )


def process_import_run(db: Session, run_id) -> ImportRun:
    """Validate (and, unless dry-run, persist) every row of a pending run,
    recording per-row outcomes. Idempotent: a non-pending run is returned as-is."""
    from app.services import web_system_import_wizard as wiz

    run = db.get(ImportRun, coerce_uuid(run_id))
    if run is None:
        raise ValueError("Import run not found")
    if run.status != ImportRunStatus.pending:
        return run

    from app.services.financial_imports import FINANCIAL_IMPORT_MODULES

    if run.module in FINANCIAL_IMPORT_MODULES and not run.dry_run:
        source = run.source_run
        if source is None:
            raise ValueError("Financial apply run is missing its validated source run")
        if source.status != ImportRunStatus.dry_run_ready or not source.dry_run:
            raise ValueError("Financial apply source is not a validated dry run")
        if source.module != run.module or source.input_text != run.input_text:
            raise ValueError("Financial apply input differs from its validated source")

    run.status = ImportRunStatus.running
    run.started_at = datetime.now(UTC)
    db.commit()

    try:
        cfg = wiz.ENTITY_CONFIG.get(run.module)
        if not cfg:
            raise ValueError(f"Unsupported import module: {run.module}")
        model_cls = cfg["model"]
        parsed = wiz.parse_payload(
            data_format=run.data_format,
            raw_text=run.input_text or "",
            source_name=run.source_name or "import",
            csv_delimiter=run.csv_delimiter or ",",
        )
        mapped = wiz.apply_column_mapping(parsed.rows, run.column_mapping or {})

        ok = failed = 0
        for idx, raw in enumerate(mapped, start=1):
            try:
                parsed_row = model_cls.model_validate(raw)
            except Exception as exc:  # noqa: BLE001 - per-row validation error
                _record_row(db, run.id, idx, raw, ImportRowStatus.error, error=str(exc))
                failed += 1
            else:
                if run.dry_run:
                    _record_row(
                        db,
                        run.id,
                        idx,
                        raw,
                        ImportRowStatus.ok,
                        result={"validated": True},
                    )
                    ok += 1
                else:
                    nested = db.begin_nested()
                    try:
                        obj = wiz._persist_row(
                            db,
                            run.module,
                            parsed_row,
                            source_name=run.source_name or "import",
                        )
                        db.flush()
                        obj_id = getattr(obj, "id", None)
                        nested.commit()
                    except Exception as exc:  # noqa: BLE001 - per-row apply error
                        nested.rollback()
                        _record_row(
                            db, run.id, idx, raw, ImportRowStatus.error, error=str(exc)
                        )
                        failed += 1
                    else:
                        _record_row(
                            db,
                            run.id,
                            idx,
                            raw,
                            ImportRowStatus.ok,
                            result={"id": str(obj_id)} if obj_id is not None else None,
                        )
                        ok += 1
            if idx % _CHUNK_COMMIT == 0:
                run.total_rows, run.ok_rows, run.failed_rows = idx, ok, failed
                db.commit()

        run.total_rows = len(mapped)
        run.ok_rows = ok
        run.failed_rows = failed
        run.status = (
            ImportRunStatus.dry_run_ready if run.dry_run else ImportRunStatus.completed
        )
        run.completed_at = datetime.now(UTC)
        run.summary = {
            "module": run.module,
            "total_rows": len(mapped),
            "ok_rows": ok,
            "failed_rows": failed,
            "dry_run": run.dry_run,
        }
        db.commit()
        return run
    except Exception as exc:  # noqa: BLE001 - whole-run failure
        db.rollback()
        failed_run = db.get(ImportRun, coerce_uuid(run_id))
        if failed_run is None:
            raise
        failed_run.status = ImportRunStatus.failed
        failed_run.error_message = str(exc)
        failed_run.completed_at = datetime.now(UTC)
        db.commit()
        return failed_run
