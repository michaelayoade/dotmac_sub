"""Catalog-verified, concurrent repair of the two regional-report indexes.

Alembic owns these physical read optimizations; financial records and the
report projection retain their existing owners. Only an interrupted build
with the exact expected structure may be dropped. A name is never success.
Call repair inside Alembic's autocommit block; keep its bounded lock budget.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

import sqlalchemy as sa


@dataclass(frozen=True)
class IndexSpec:
    name: str
    table: str
    keys: tuple[str, str, str, str]

    @property
    def create_sql(self) -> str:
        # Specifications are checked-in constants, never operator input.
        return (
            f"CREATE INDEX CONCURRENTLY {self.name} "
            f"ON public.{self.table} ({', '.join(self.keys)})"
        )

    @property
    def drop_sql(self) -> str:
        return f"DROP INDEX CONCURRENTLY IF EXISTS public.{self.name}"


INDEX_SPECS = (
    IndexSpec(
        name="ix_invoices_regional_report_period",
        table="invoices",
        keys=("is_active", "status", "issued_at", "account_id"),
    ),
    IndexSpec(
        name="ix_payments_regional_report_period",
        table="payments",
        keys=("is_active", "status", "paid_at", "account_id"),
    ),
)


@dataclass(frozen=True)
class IndexState:
    relation_kind: str
    table_schema: str
    table_name: str
    valid: bool
    ready: bool
    live: bool
    unique: bool
    constraint_backed: bool
    access_method: str
    key_attribute_count: int
    total_attribute_count: int
    has_predicate: bool
    has_expressions: bool
    keys: tuple[str, ...]
    descending: tuple[bool | None, ...]
    nulls_first: tuple[bool | None, ...]
    building: bool


class RepairOutcome(StrEnum):
    unchanged = "unchanged"
    created = "created"
    rebuilt = "rebuilt"


class RegionalReportIndexContractError(RuntimeError):
    code = "migration.regional_report_index.contract_failed"

    def __init__(self, *, spec: IndexSpec, issues: tuple[str, ...]) -> None:
        self.spec = spec
        self.issues = issues
        super().__init__(f"public.{spec.name}: " + "; ".join(issues))


def postgres_index_state(
    bind: sa.engine.Connection, *, spec: IndexSpec
) -> IndexState | None:
    """Include same-name non-index objects, wrong tables and invalid builds."""

    row = (
        bind.execute(
            sa.text(
                """
            SELECT c.relkind AS relation_kind,
                   tn.nspname AS table_schema, t.relname AS table_name,
                   i.indisvalid AS valid, i.indisready AS ready,
                   i.indislive AS live, i.indisunique AS unique,
                   EXISTS (SELECT 1 FROM pg_constraint WHERE conindid=c.oid)
                       AS constraint_backed,
                   am.amname AS access_method,
                   i.indnkeyatts AS key_attribute_count,
                   i.indnatts AS total_attribute_count,
                   i.indpred IS NOT NULL AS has_predicate,
                   i.indexprs IS NOT NULL AS has_expressions,
                   ARRAY(SELECT pg_get_indexdef(c.oid, p, true)
                         FROM generate_series(1, i.indnkeyatts) AS p) AS keys,
                   ARRAY(SELECT pg_index_column_has_property(c.oid, p, 'desc')
                         FROM generate_series(1, i.indnkeyatts) AS p) AS descending,
                   ARRAY(SELECT pg_index_column_has_property(c.oid, p, 'nulls_first')
                         FROM generate_series(1, i.indnkeyatts) AS p) AS nulls_first,
                   EXISTS (SELECT 1 FROM pg_stat_progress_create_index
                           WHERE index_relid=c.oid) AS building
            FROM pg_class AS c
            JOIN pg_namespace AS n ON n.oid=c.relnamespace
            LEFT JOIN pg_index AS i ON i.indexrelid=c.oid
            LEFT JOIN pg_class AS t ON t.oid=i.indrelid
            LEFT JOIN pg_namespace AS tn ON tn.oid=t.relnamespace
            LEFT JOIN pg_am AS am ON am.oid=c.relam
            WHERE n.nspname='public' AND c.relname=:index_name
            """
            ),
            {"index_name": spec.name},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    return IndexState(
        relation_kind=str(row["relation_kind"]),
        table_schema=str(row["table_schema"] or ""),
        table_name=str(row["table_name"] or ""),
        valid=bool(row["valid"]),
        ready=bool(row["ready"]),
        live=bool(row["live"]),
        unique=bool(row["unique"]),
        constraint_backed=bool(row["constraint_backed"]),
        access_method=str(row["access_method"] or ""),
        key_attribute_count=int(row["key_attribute_count"] or 0),
        total_attribute_count=int(row["total_attribute_count"] or 0),
        has_predicate=bool(row["has_predicate"]),
        has_expressions=bool(row["has_expressions"]),
        keys=tuple(str(key) for key in row["keys"]),
        descending=tuple(
            None if flag is None else bool(flag) for flag in row["descending"]
        ),
        nulls_first=tuple(
            None if flag is None else bool(flag) for flag in row["nulls_first"]
        ),
        building=bool(row["building"]),
    )


def structural_errors(state: IndexState, *, spec: IndexSpec) -> tuple[str, ...]:
    """Refuse unexpected objects even if they are invalid (never drop them)."""

    issues: list[str] = []
    if state.relation_kind != "i":
        issues.append("same-name object is not an ordinary index")
    if (state.table_schema, state.table_name) != ("public", spec.table):
        issues.append(f"wrong table; expected public.{spec.table}")
    if state.unique or state.constraint_backed:
        issues.append("must be non-unique and not constraint-backed")
    if not state.live:
        issues.append("index is being dropped")
    if state.building:
        issues.append("index build is still in progress")
    if state.access_method != "btree":
        issues.append("expected btree")
    if state.key_attribute_count != 4 or state.total_attribute_count != 4:
        issues.append("expected four keys with no included columns")
    if state.has_predicate or state.has_expressions:
        issues.append("partial or expression index is not the reporting contract")
    if state.keys != spec.keys:
        issues.append(
            f"wrong keys, collation or operator class; expected {spec.keys!r}"
        )
    if state.descending != (False,) * 4 or state.nulls_first != (False,) * 4:
        issues.append("expected ascending keys with nulls last")
    return tuple(issues)


def index_contract_errors(
    state: IndexState | None, *, spec: IndexSpec
) -> tuple[str, ...]:
    if state is None:
        return ("index is missing",)
    issues = list(structural_errors(state, spec=spec))
    if not state.valid:
        issues.append("index is not valid")
    if not state.ready:
        issues.append("index is not ready")
    return tuple(issues)


def validate_postgres_indexes(bind: sa.engine.Connection) -> None:
    """Read-only deploy gate: require both exact, usable reporting indexes."""

    for spec in INDEX_SPECS:
        issues = index_contract_errors(postgres_index_state(bind, spec=spec), spec=spec)
        if issues:
            raise RegionalReportIndexContractError(spec=spec, issues=issues)


def ensure_postgres_index(
    bind: sa.engine.Connection,
    execute: Callable[[str], None],
    *,
    spec: IndexSpec,
) -> RepairOutcome:
    """Preserve valid indexes; rebuild only a proven interrupted owned index."""

    if spec not in INDEX_SPECS:
        raise RegionalReportIndexContractError(
            spec=spec, issues=("unknown index specification",)
        )
    state = postgres_index_state(bind, spec=spec)
    outcome = RepairOutcome.created
    if state is not None:
        issues = structural_errors(state, spec=spec)
        if issues:
            raise RegionalReportIndexContractError(spec=spec, issues=issues)
        if state.valid and state.ready:
            return RepairOutcome.unchanged
        execute(spec.drop_sql)
        outcome = RepairOutcome.rebuilt
    # No IF NOT EXISTS: a competing creation must not become false success.
    execute(spec.create_sql)
    issues = index_contract_errors(postgres_index_state(bind, spec=spec), spec=spec)
    if issues:
        raise RegionalReportIndexContractError(spec=spec, issues=issues)
    return outcome


def ensure_postgres_indexes(
    bind: sa.engine.Connection, execute: Callable[[str], None]
) -> tuple[RepairOutcome, ...]:
    return tuple(
        ensure_postgres_index(bind, execute, spec=spec) for spec in INDEX_SPECS
    )
