#!/usr/bin/env python3
"""Identify strict subscriber retirement candidates without changing data."""

from __future__ import annotations

import argparse
import csv
import json
import os
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy import inspect, text
from sqlalchemy.engine.reflection import Inspector
from sqlalchemy.orm import Session

from app.db import read_only_snapshot_session
from app.services import party_identity_audit

CsvCell = str | int | bool


@dataclass(frozen=True)
class LocalDependencyScan:
    dependencies: dict[UUID, tuple[str, ...]]
    scanned_sources: tuple[str, ...]


@dataclass(frozen=True)
class RetainedServiceScan:
    subscriber_ids: frozenset[UUID]
    schema_present: bool
    service_table_present: bool
    service_row_count: int | None


def _private_text_file(path: Path):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)
    return os.fdopen(descriptor, "w", encoding="utf-8", newline="")


def _write_csv(
    path: Path,
    header: Sequence[str],
    rows: Iterable[Sequence[CsvCell]],
) -> None:
    with _private_text_file(path) as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def _quote(inspector: Inspector, identifier: str) -> str:
    return inspector.bind.dialect.identifier_preparer.quote_identifier(identifier)


def _subscriber_reference_columns(
    inspector: Inspector,
    *,
    table_name: str,
    schema: str,
) -> tuple[str, ...]:
    column_names = {
        str(column["name"])
        for column in inspector.get_columns(table_name, schema=schema)
    }
    reference_columns = {
        column_name
        for column_name in column_names
        if column_name == "subscriber_id" or column_name.endswith("_subscriber_id")
    }
    for foreign_key in inspector.get_foreign_keys(table_name, schema=schema):
        referred_schema = foreign_key.get("referred_schema")
        if referred_schema not in (None, schema):
            continue
        if foreign_key.get("referred_table") != "subscribers":
            continue
        referred_columns = tuple(foreign_key.get("referred_columns") or ())
        constrained_columns = tuple(foreign_key.get("constrained_columns") or ())
        if referred_columns != ("id",) or len(constrained_columns) != 1:
            continue
        reference_columns.add(str(constrained_columns[0]))
    return tuple(sorted(reference_columns))


def _uuid_values(db: Session, statement: str) -> frozenset[UUID]:
    values: set[UUID] = set()
    for raw_value in db.execute(text(statement)).scalars():
        if raw_value is None:
            continue
        try:
            values.add(UUID(str(raw_value)))
        except ValueError:
            continue
    return frozenset(values)


def collect_local_dependency_evidence(
    db: Session,
    *,
    subscriber_ids: frozenset[UUID],
) -> LocalDependencyScan:
    """Scan every public Subscriber FK and subscriber-id shadow column.

    Foreign keys cover canonical dependencies.  The suffix scan also includes
    legacy UUID shadows that predate a constraint, so an unconstrained imported
    relationship cannot silently become absence evidence.
    """

    inspector = inspect(db.get_bind())
    schema = "public"
    dependencies: dict[UUID, set[str]] = defaultdict(set)
    scanned_sources: list[str] = []
    quoted_schema = _quote(inspector, schema)
    for table_name in sorted(inspector.get_table_names(schema=schema)):
        if table_name == "subscribers":
            continue
        quoted_table = _quote(inspector, table_name)
        for column_name in _subscriber_reference_columns(
            inspector,
            table_name=table_name,
            schema=schema,
        ):
            source = f"{schema}.{table_name}.{column_name}"
            scanned_sources.append(source)
            quoted_column = _quote(inspector, column_name)
            values = _uuid_values(
                db,
                f"SELECT DISTINCT {quoted_column}::text "
                f"FROM {quoted_schema}.{quoted_table} "
                f"WHERE {quoted_column} IS NOT NULL",
            )
            for subscriber_id in values.intersection(subscriber_ids):
                dependencies[subscriber_id].add(source)

    if "service_extensions" in inspector.get_table_names(schema=schema):
        extension_columns = {
            str(column["name"])
            for column in inspector.get_columns("service_extensions", schema=schema)
        }
        if "scope_subscriber_ids" in extension_columns:
            source = "public.service_extensions.scope_subscriber_ids"
            scanned_sources.append(source)
            values = _uuid_values(
                db,
                "SELECT DISTINCT jsonb_array_elements_text(scope_subscriber_ids::jsonb) "
                "FROM public.service_extensions "
                "WHERE scope_subscriber_ids IS NOT NULL "
                "AND jsonb_typeof(scope_subscriber_ids::jsonb) = 'array'",
            )
            for subscriber_id in values.intersection(subscriber_ids):
                dependencies[subscriber_id].add(source)

    return LocalDependencyScan(
        dependencies={
            subscriber_id: tuple(sorted(sources))
            for subscriber_id, sources in dependencies.items()
        },
        scanned_sources=tuple(sorted(set(scanned_sources))),
    )


def collect_retained_splynx_service_evidence(
    db: Session,
    *,
    schema: str,
    service_table: str,
) -> RetainedServiceScan:
    inspector = inspect(db.get_bind())
    schemas = set(inspector.get_schema_names())
    if schema not in schemas:
        return RetainedServiceScan(frozenset(), False, False, None)
    tables = set(inspector.get_table_names(schema=schema))
    if service_table not in tables:
        return RetainedServiceScan(frozenset(), True, False, None)
    columns = {
        str(column["name"])
        for column in inspector.get_columns(service_table, schema=schema)
    }
    if "customer_id" not in columns:
        return RetainedServiceScan(frozenset(), True, False, None)

    quoted_schema = _quote(inspector, schema)
    quoted_table = _quote(inspector, service_table)
    service_row_count = int(
        db.execute(
            text(f"SELECT count(*) FROM {quoted_schema}.{quoted_table}")
        ).scalar_one()
    )
    subscriber_ids = _uuid_values(
        db,
        "SELECT DISTINCT subscriber.id::text "
        "FROM public.subscribers AS subscriber "
        f"JOIN {quoted_schema}.{quoted_table} AS legacy_service "
        "ON legacy_service.customer_id::text = "
        "subscriber.splynx_customer_id::text "
        "WHERE subscriber.splynx_customer_id IS NOT NULL",
    )
    return RetainedServiceScan(
        subscriber_ids,
        True,
        True,
        service_row_count,
    )


def build_retirement_audit(
    db: Session,
    *,
    archive_sha256: str,
    expected_archive_sha256: str,
    splynx_schema: str,
    splynx_service_table: str,
) -> party_identity_audit.SubscriberRetirementAudit:
    identity = party_identity_audit.build_subscriber_identity_audit(db)
    subscriber_ids = frozenset(row.subscriber_id for row in identity.rows)
    dependencies = collect_local_dependency_evidence(db, subscriber_ids=subscriber_ids)
    retained_services = collect_retained_splynx_service_evidence(
        db,
        schema=splynx_schema,
        service_table=splynx_service_table,
    )
    retained_evidence = party_identity_audit.RetainedSplynxEvidence(
        schema_present=retained_services.schema_present,
        service_table_present=retained_services.service_table_present,
        archive_sha256=archive_sha256,
        expected_archive_sha256=expected_archive_sha256,
        service_row_count=retained_services.service_row_count,
    )
    return party_identity_audit.resolve_subscriber_retirement_audit(
        identity,
        local_dependencies=dependencies.dependencies,
        retained_splynx_service_subscriber_ids=retained_services.subscriber_ids,
        retained_splynx_evidence=retained_evidence,
        local_dependency_scan_complete=True,
        scanned_local_dependency_sources=dependencies.scanned_sources,
    )


def write_retirement_artifacts(
    audit: party_identity_audit.SubscriberRetirementAudit,
    output_dir: Path,
) -> tuple[Path, ...]:
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    output_dir.chmod(0o700)
    summary_path = output_dir / "summary.json"
    worklist_path = output_dir / "retirement_worklist.csv"
    candidates_path = output_dir / "retirement_candidates.csv"
    provisional_path = output_dir / "provisional_full_negative.csv"

    with _private_text_file(summary_path) as handle:
        json.dump(audit.summary(), handle, indent=2, sort_keys=True)
        handle.write("\n")

    header = (
        "subscriber_id",
        "disposition",
        "reason_codes",
        "local_dependency_source_count",
        "local_dependency_sources",
        "has_retained_splynx_service",
        "identity_row_fingerprint",
        "retirement_row_fingerprint",
    )

    def _row(
        item: party_identity_audit.SubscriberRetirementAuditRow,
    ) -> tuple[CsvCell, ...]:
        return (
            str(item.subscriber_id),
            item.disposition.value,
            "|".join(item.reason_codes),
            len(item.local_dependency_sources),
            "|".join(item.local_dependency_sources),
            item.has_retained_splynx_service,
            item.identity_row_fingerprint,
            party_identity_audit.subscriber_retirement_audit_row_fingerprint(item),
        )

    _write_csv(worklist_path, header, (_row(item) for item in audit.rows))
    _write_csv(
        candidates_path,
        header,
        (
            _row(item)
            for item in audit.rows
            if item.disposition
            is party_identity_audit.RetirementDisposition.automatic_retirement_candidate
        ),
    )
    _write_csv(
        provisional_path,
        header,
        (
            _row(item)
            for item in audit.rows
            if item.disposition
            is party_identity_audit.RetirementDisposition.blocked_incomplete_evidence
        ),
    )
    return summary_path, worklist_path, candidates_path, provisional_path


def run(
    output_dir: Path,
    *,
    archive_sha256: str,
    expected_archive_sha256: str,
    splynx_schema: str,
    splynx_service_table: str,
) -> party_identity_audit.SubscriberRetirementAudit:
    with read_only_snapshot_session() as db:
        audit = build_retirement_audit(
            db,
            archive_sha256=archive_sha256,
            expected_archive_sha256=expected_archive_sha256,
            splynx_schema=splynx_schema,
            splynx_service_table=splynx_service_table,
        )
        db.rollback()
    write_retirement_artifacts(audit, output_dir)
    return audit


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--splynx-archive-sha256", required=True)
    parser.add_argument("--expected-splynx-archive-sha256", required=True)
    parser.add_argument("--splynx-schema", default="splynx_staging")
    parser.add_argument("--splynx-service-table", default="splynx_services_internet")
    return parser


def main() -> int:
    args = _parser().parse_args()
    audit = run(
        args.out,
        archive_sha256=args.splynx_archive_sha256,
        expected_archive_sha256=args.expected_splynx_archive_sha256,
        splynx_schema=args.splynx_schema,
        splynx_service_table=args.splynx_service_table,
    )
    print(json.dumps(audit.summary(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
