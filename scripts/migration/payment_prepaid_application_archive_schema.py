"""Immutable schema contract for retired prepaid-application evidence.

This module is intentionally imported by the retirement, compatibility, and
forward-validation Alembic revisions.  The archive has no runtime ORM model or
writer, so migrations need one checked-in structural contract that can detect
missing or malformed evidence instead of treating a table name as proof.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

LEGACY_TABLE = "payment_prepaid_applications"
ARCHIVE_TABLE = "payment_prepaid_applications_archive"


@dataclass(frozen=True)
class ColumnContract:
    name: str
    kind: str
    nullable: bool
    length: int | None = None
    precision: int | None = None
    scale: int | None = None
    timezone: bool | None = None
    default_token: str | None = None


COLUMN_CONTRACTS = (
    ColumnContract("id", "uuid", False),
    ColumnContract("payment_id", "uuid", False),
    ColumnContract("settlement_id", "uuid", False),
    ColumnContract("account_id", "uuid", False),
    ColumnContract("subscription_id", "uuid", False),
    ColumnContract("credit_ledger_entry_id", "uuid", False),
    ColumnContract("debit_ledger_entry_id", "uuid", False),
    ColumnContract("entitlement_id", "uuid", False),
    ColumnContract("retired_allocation_id", "uuid", True),
    ColumnContract("historical_invoice_id", "uuid", True),
    ColumnContract("invoice_closure_id", "uuid", True),
    ColumnContract("origin", "string", False, length=32),
    ColumnContract("amount", "numeric", False, precision=12, scale=2),
    ColumnContract("currency", "string", False, length=3),
    ColumnContract("period_start", "datetime", False, timezone=True),
    ColumnContract("period_end", "datetime", False, timezone=True),
    ColumnContract("reason", "text", False),
    ColumnContract("preview_fingerprint", "string", False, length=64),
    ColumnContract("idempotency_key", "string", False, length=120),
    ColumnContract(
        "access_recheck_status",
        "string",
        False,
        length=24,
        default_token="not_required",
    ),
    ColumnContract("access_recheck_error", "string", True, length=120),
    ColumnContract("access_rechecked_at", "datetime", True, timezone=True),
    ColumnContract(
        "created_at", "datetime", False, timezone=True, default_token="timestamp"
    ),
    ColumnContract(
        "updated_at", "datetime", False, timezone=True, default_token="timestamp"
    ),
)

FOREIGN_KEY_CONTRACTS = frozenset(
    {
        (("payment_id",), "payments", ("id",), "RESTRICT"),
        (("settlement_id",), "payment_settlements", ("id",), "RESTRICT"),
        (("account_id",), "subscribers", ("id",), "RESTRICT"),
        (("subscription_id",), "subscriptions", ("id",), "RESTRICT"),
        (("credit_ledger_entry_id",), "ledger_entries", ("id",), "RESTRICT"),
        (("debit_ledger_entry_id",), "ledger_entries", ("id",), "RESTRICT"),
        (("entitlement_id",), "service_entitlements", ("id",), "RESTRICT"),
        (("retired_allocation_id",), "payment_allocations", ("id",), "RESTRICT"),
        (("historical_invoice_id",), "invoices", ("id",), "RESTRICT"),
        (("invoice_closure_id",), "invoice_closures", ("id",), "RESTRICT"),
    }
)

CHECK_CONTRACTS = {
    "ck_payment_prepaid_applications_amount_positive": ("amount", ">", "0"),
    "ck_payment_prepaid_applications_period_order": (
        "period_end",
        ">",
        "period_start",
    ),
    "ck_payment_prepaid_applications_origin": (
        "origin",
        "historical_reconciliation",
        "post_settlement",
    ),
    "ck_payment_prepaid_applications_access_status": (
        "access_recheck_status",
        "not_required",
        "pending",
        "completed",
        "deferred",
    ),
}

INDEX_CONTRACTS = tuple(
    (
        f"uq_payment_prepaid_applications_{column}",
        (column,),
        True,
    )
    for column in (
        "payment_id",
        "settlement_id",
        "credit_ledger_entry_id",
        "debit_ledger_entry_id",
        "entitlement_id",
        "retired_allocation_id",
        "invoice_closure_id",
        "idempotency_key",
    )
)


def archive_table_elements() -> tuple[sa.SchemaItem, ...]:
    """Return fresh SQLAlchemy elements for the compatibility archive table."""

    return (
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("settlement_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "credit_ledger_entry_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "debit_ledger_entry_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("entitlement_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "retired_allocation_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column(
            "historical_invoice_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("invoice_closure_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("origin", sa.String(length=32), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("preview_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column(
            "access_recheck_status",
            sa.String(length=24),
            nullable=False,
            server_default="not_required",
        ),
        sa.Column("access_recheck_error", sa.String(length=120), nullable=True),
        sa.Column("access_rechecked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "amount > 0", name="ck_payment_prepaid_applications_amount_positive"
        ),
        sa.CheckConstraint(
            "period_end > period_start",
            name="ck_payment_prepaid_applications_period_order",
        ),
        sa.CheckConstraint(
            "origin IN ('historical_reconciliation', 'post_settlement')",
            name="ck_payment_prepaid_applications_origin",
        ),
        sa.CheckConstraint(
            "access_recheck_status IN "
            "('not_required', 'pending', 'completed', 'deferred')",
            name="ck_payment_prepaid_applications_access_status",
        ),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["settlement_id"], ["payment_settlements.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["subscribers.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"], ["subscriptions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["credit_ledger_entry_id"], ["ledger_entries.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["debit_ledger_entry_id"], ["ledger_entries.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["entitlement_id"], ["service_entitlements.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["retired_allocation_id"],
            ["payment_allocations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["historical_invoice_id"], ["invoices.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["invoice_closure_id"], ["invoice_closures.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def _type_errors(
    *, dialect_name: str, actual: Any, expected: ColumnContract
) -> list[str]:
    if expected.kind == "uuid":
        if dialect_name == "postgresql" and not isinstance(actual, postgresql.UUID):
            return [f"{expected.name} must be PostgreSQL UUID, got {actual!r}"]
        return []

    if expected.kind == "string":
        if not isinstance(actual, sa.String) or actual.length != expected.length:
            return [
                f"{expected.name} must be VARCHAR({expected.length}), got {actual!r}"
            ]
    elif expected.kind == "numeric":
        if (
            not isinstance(actual, sa.Numeric)
            or actual.precision != expected.precision
            or actual.scale != expected.scale
        ):
            return [
                f"{expected.name} must be NUMERIC({expected.precision},"
                f"{expected.scale}), got {actual!r}"
            ]
    elif expected.kind == "datetime":
        if not isinstance(actual, sa.DateTime):
            return [f"{expected.name} must be TIMESTAMPTZ, got {actual!r}"]
        if dialect_name == "postgresql" and actual.timezone is not expected.timezone:
            return [f"{expected.name} must retain timezone semantics"]
    elif expected.kind == "text" and not isinstance(actual, sa.Text):
        return [f"{expected.name} must be TEXT, got {actual!r}"]
    return []


def _default_matches(actual: Any, token: str | None) -> bool:
    if token is None:
        return actual is None
    if actual is None:
        return False
    normalized = str(actual).lower().replace('"', "").replace("'", "")
    if token == "timestamp":
        return "current_timestamp" in normalized or "now()" in normalized
    return token in normalized


def validate_archive_schema(
    bind: sa.engine.Connection,
    *,
    expected_row_count: int | None = None,
) -> int:
    """Validate the complete archive contract and return its row count."""

    inspector = sa.inspect(bind)
    if not inspector.has_table(ARCHIVE_TABLE):
        raise RuntimeError(f"required evidence archive {ARCHIVE_TABLE} is missing")

    errors: list[str] = []
    actual_columns = inspector.get_columns(ARCHIVE_TABLE)
    actual_column_names = tuple(column["name"] for column in actual_columns)
    expected_column_names = tuple(contract.name for contract in COLUMN_CONTRACTS)
    if actual_column_names != expected_column_names:
        errors.append(
            "column order/set mismatch: "
            f"expected={expected_column_names}, actual={actual_column_names}"
        )
    else:
        for actual, expected in zip(actual_columns, COLUMN_CONTRACTS, strict=True):
            if bool(actual["nullable"]) is not expected.nullable:
                errors.append(
                    f"{expected.name} nullable={actual['nullable']!r}, "
                    f"expected {expected.nullable!r}"
                )
            errors.extend(
                _type_errors(
                    dialect_name=bind.dialect.name,
                    actual=actual["type"],
                    expected=expected,
                )
            )
            if not _default_matches(actual.get("default"), expected.default_token):
                errors.append(
                    f"{expected.name} default={actual.get('default')!r}, "
                    f"expected token {expected.default_token!r}"
                )

    primary_key = inspector.get_pk_constraint(ARCHIVE_TABLE)
    if tuple(primary_key.get("constrained_columns") or ()) != ("id",):
        errors.append(f"primary key mismatch: {primary_key!r}")

    actual_foreign_keys = frozenset(
        (
            tuple(foreign_key.get("constrained_columns") or ()),
            str(foreign_key.get("referred_table") or ""),
            tuple(foreign_key.get("referred_columns") or ()),
            str((foreign_key.get("options") or {}).get("ondelete") or "").upper(),
        )
        for foreign_key in inspector.get_foreign_keys(ARCHIVE_TABLE)
    )
    if actual_foreign_keys != FOREIGN_KEY_CONTRACTS:
        errors.append(
            "foreign-key mismatch: "
            f"expected={sorted(FOREIGN_KEY_CONTRACTS)!r}, "
            f"actual={sorted(actual_foreign_keys)!r}"
        )

    actual_checks = {
        str(check.get("name")): str(check.get("sqltext") or "").lower()
        for check in inspector.get_check_constraints(ARCHIVE_TABLE)
    }
    if set(actual_checks) != set(CHECK_CONTRACTS):
        errors.append(
            "check-constraint name mismatch: "
            f"expected={sorted(CHECK_CONTRACTS)!r}, actual={sorted(actual_checks)!r}"
        )
    else:
        for name, required_fragments in CHECK_CONTRACTS.items():
            expression = actual_checks[name]
            if any(fragment not in expression for fragment in required_fragments):
                errors.append(
                    f"check constraint {name} has unexpected expression {expression!r}"
                )

    actual_indexes = frozenset(
        (
            str(index.get("name")),
            tuple(index.get("column_names") or ()),
            bool(index.get("unique")),
        )
        for index in inspector.get_indexes(ARCHIVE_TABLE)
    )
    if actual_indexes != frozenset(INDEX_CONTRACTS):
        errors.append(
            "index mismatch: "
            f"expected={sorted(INDEX_CONTRACTS)!r}, actual={sorted(actual_indexes)!r}"
        )

    table = sa.Table(ARCHIVE_TABLE, sa.MetaData(), autoload_with=bind)
    row_count = int(bind.scalar(sa.select(sa.func.count()).select_from(table)) or 0)
    if expected_row_count is not None and row_count != expected_row_count:
        errors.append(
            f"row-count mismatch: expected={expected_row_count}, actual={row_count}"
        )

    if errors:
        raise RuntimeError(
            "prepaid payment-application archive schema validation failed: "
            + "; ".join(errors)
        )
    return row_count
