"""Make Main (SPL-1) the canonical house reseller and retire the empty House row.

Revision ID: 663_main_canonical_house_reseller
Revises: 662_validate_regional_report_billing_indexes
Create Date: 2026-10-10

## Why

``resellers.is_house`` marks the row that represents the company itself: the
direct customer base. ``uq_resellers_one_house`` (``is_house WHERE is_house``)
allows exactly one such row. Two rows currently compete for that meaning:

- ``Main`` (``code = 'SPL-1'``, ``is_house = false``) owns the direct customer
  base migrated from Splynx -- about 14,888 subscribers on 2026-10-10;
- ``House`` (``is_house = true``), created 2026-06-03, owned 54 subscribers
  until audited owner updates moved every one of them on 2026-10-10. It has one
  unused billing account and one never-used ``reseller_users`` row.

Every ``is_house`` reader therefore treats the real direct base as an external
reseller's customers (no profile cleanup, no AI data-cleaning intake, no
captive eligibility, reseller notification copies attempted, Main offered as a
"managing reseller"), and treats an empty row as the company. This migration
moves the designation to Main. It is the cut-over step: the subscriber moves
(backfill) and their verification already happened as audited owner updates.

## Re-derived at apply time, never guessed

Nothing here is keyed to a hardcoded id. At apply time it requires ALL of:

1. exactly one reseller with ``code = 'SPL-1'`` (Main), currently
   ``is_house = false`` and ``is_active = true``;
2. exactly one OTHER reseller with ``is_house = true`` (House);
3. House owns zero ``subscribers`` rows (any status, active or not);
4. no billing activity references House: none of its billing accounts carries
   a non-zero balance or is referenced by ``billing_account_ledger_entries``,
   ``billing_account_credit_allocations``, ``payments``, ``topup_intents``,
   ``payment_proofs`` or ``withholding_tax_records``, and no
   ``withholding_tax_records`` row names House directly.

If any condition fails -- including on a fresh or CI database, where migration
116's seeded House row exists but no ``SPL-1`` row does -- the migration changes
nothing and prints why. A table or column that does not exist in the target
schema is treated as holding no activity, because there is nothing to count.

## What it changes, in index-safe order

``uq_resellers_one_house`` is not deferrable, so the order is fixed:

1. House -> ``is_house = false, is_active = false`` (never deleted: the row is
   the record that the June designation existed);
2. House's active ``reseller_users`` rows -> ``is_active = false``;
3. Main -> ``is_house = true``.

House's billing account is left as it is (no activity, retained as evidence).

## Reversibility

Before any update, one ``audit_events`` row (action
``reseller.house_designation_moved``, entity ``Reseller`` = Main) records both
reseller ids with their prior ``is_house``/``is_active`` values and the ids of
the ``reseller_users`` rows it deactivated. ``downgrade`` reads that row back and
restores exactly those values, in the reverse index-safe order. It does nothing
if no such audit row exists (upgrade was a no-op) or if Main is no longer the
house row (someone has since moved the designation; restoring blindly would
violate the unique index or overwrite a later decision).

## Budgets

Three single-row/low-cardinality UPDATEs on ``resellers`` and ``reseller_users``
plus indexed COUNT probes. ``lock_timeout = 5s`` and
``statement_timeout = 60s`` for this revision only (restored afterwards, because
Alembic runs the whole upgrade in one transaction). A lock timeout fails the
upgrade cleanly; retry is safe because every step is re-derived.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

import sqlalchemy as sa

from alembic import op

revision: str = "663_main_canonical_house_reseller"
down_revision: str | None = "662_validate_regional_report_billing_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MAIN_CODE = "SPL-1"
_ACTION = "reseller.house_designation_moved"
_ENTITY_TYPE = "Reseller"
_ACTOR_ID = "migration:663_main_canonical_house_reseller"
_LOG_PREFIX = "663_main_canonical_house_reseller"
_REASON = (
    "Main (SPL-1) owns the direct customer base migrated from Splynx; the "
    "June 2026 House row owns no subscribers and has no billing activity. "
    "uq_resellers_one_house allows one house reseller, so the designation is "
    "moved to Main and House is retired (deactivated, never deleted)."
)

#: (table, column) pairs whose rows are billing activity on a billing account.
_BILLING_ACCOUNT_ACTIVITY: tuple[tuple[str, str], ...] = (
    ("billing_account_ledger_entries", "billing_account_id"),
    ("billing_account_credit_allocations", "billing_account_id"),
    ("payments", "billing_account_id"),
    ("topup_intents", "billing_account_id"),
    ("payment_proofs", "billing_account_id"),
    ("withholding_tax_records", "billing_account_id"),
)
#: (table, column) pairs naming a reseller directly as a billing party.
_RESELLER_BILLING_ACTIVITY: tuple[tuple[str, str], ...] = (
    ("withholding_tax_records", "reseller_id"),
)


def _has_column(bind: sa.engine.Connection, table: str, column: str) -> bool:
    inspector = sa.inspect(bind)
    if not inspector.has_table(table):
        return False
    return column in {item["name"] for item in inspector.get_columns(table)}


@contextmanager
def _bounded_locks(bind: sa.engine.Connection) -> Iterator[None]:
    """Apply this revision's lock/statement budget, then restore the prior one."""

    previous = bind.execute(
        sa.text(
            "SELECT current_setting('lock_timeout') AS lock_timeout, "
            "current_setting('statement_timeout') AS statement_timeout"
        )
    ).one()
    bind.execute(sa.text("SELECT set_config('lock_timeout', '5s', true)"))
    bind.execute(sa.text("SELECT set_config('statement_timeout', '60s', true)"))
    try:
        yield
    finally:
        bind.execute(
            sa.text(
                "SELECT set_config('lock_timeout', :lock_timeout, true), "
                "set_config('statement_timeout', :statement_timeout, true)"
            ),
            {
                "lock_timeout": previous.lock_timeout,
                "statement_timeout": previous.statement_timeout,
            },
        )


def _count(bind: sa.engine.Connection, table: str, column: str, ids: list) -> int:
    if not ids or not _has_column(bind, table, column):
        return 0
    # Table/column names come from the module constants above, never input.
    return int(
        bind.execute(
            sa.text(
                f"SELECT count(*) FROM {table} WHERE {column} IN :ids"  # noqa: S608
            ).bindparams(sa.bindparam("ids", expanding=True)),
            {"ids": ids},
        ).scalar_one()
    )


def _refuse(reason: str) -> None:
    print(f"{_LOG_PREFIX}: {reason}; leaving all reseller rows untouched.")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if not _has_column(bind, "resellers", "is_house"):
        return
    with _bounded_locks(bind):
        _move_house_designation(bind)


def _move_house_designation(bind: sa.engine.Connection) -> None:
    mains = (
        bind.execute(
            sa.text(
                "SELECT id, is_house, is_active FROM resellers WHERE code = :code "
                "FOR UPDATE"
            ),
            {"code": _MAIN_CODE},
        )
        .mappings()
        .all()
    )
    if len(mains) != 1:
        _refuse(f"expected exactly one reseller coded {_MAIN_CODE}, found {len(mains)}")
        return
    main = mains[0]
    if main["is_house"]:
        _refuse(f"{_MAIN_CODE} is already the house reseller")
        return
    if not main["is_active"]:
        _refuse(f"{_MAIN_CODE} is inactive; an inactive row cannot be the company")
        return

    houses = (
        bind.execute(
            sa.text(
                "SELECT id, is_house, is_active FROM resellers "
                "WHERE is_house = true AND id <> :main_id FOR UPDATE"
            ),
            {"main_id": main["id"]},
        )
        .mappings()
        .all()
    )
    if len(houses) != 1:
        _refuse(
            "expected exactly one other reseller with is_house = true, "
            f"found {len(houses)}"
        )
        return
    house = houses[0]

    subscriber_count = _count(bind, "subscribers", "reseller_id", [house["id"]])
    if subscriber_count:
        _refuse(
            f"House reseller {house['id']} still owns {subscriber_count} subscriber(s)"
        )
        return

    billing_accounts = (
        bind.execute(
            sa.text("SELECT id, balance FROM billing_accounts WHERE reseller_id = :id"),
            {"id": house["id"]},
        )
        .mappings()
        .all()
        if _has_column(bind, "billing_accounts", "reseller_id")
        else []
    )
    nonzero = [row["id"] for row in billing_accounts if (row["balance"] or 0) != 0]
    if nonzero:
        _refuse(f"House billing account(s) carry a non-zero balance: {nonzero}")
        return
    account_ids = [row["id"] for row in billing_accounts]
    activity = {
        f"{table}.{column}": _count(bind, table, column, account_ids)
        for table, column in _BILLING_ACCOUNT_ACTIVITY
    }
    activity.update(
        {
            f"{table}.{column}": _count(bind, table, column, [house["id"]])
            for table, column in _RESELLER_BILLING_ACTIVITY
        }
    )
    active_activity = {name: count for name, count in activity.items() if count}
    if active_activity:
        _refuse(f"House reseller {house['id']} has billing activity {active_activity}")
        return

    reseller_user_ids = (
        [
            str(row_id)
            for row_id in bind.execute(
                sa.text(
                    "SELECT id FROM reseller_users "
                    "WHERE reseller_id = :id AND is_active = true "
                    "ORDER BY id FOR UPDATE"
                ),
                {"id": house["id"]},
            ).scalars()
        ]
        if _has_column(bind, "reseller_users", "reseller_id")
        else []
    )

    if _has_column(bind, "audit_events", "metadata"):
        bind.execute(
            sa.text(
                "INSERT INTO audit_events ("
                "id, occurred_at, actor_type, actor_id, actor_label, action, "
                "entity_type, entity_id, status_code, is_success, is_active, "
                "metadata, created_at"
                ") VALUES ("
                ":id, now(), 'system', :actor_id, :actor_label, :action, "
                ":entity_type, :entity_id, 200, true, true, "
                "CAST(:metadata AS JSONB), now()"
                ")"
            ),
            {
                "id": str(uuid.uuid4()),
                "actor_id": _ACTOR_ID,
                "actor_label": "alembic migration 663",
                "action": _ACTION,
                "entity_type": _ENTITY_TYPE,
                "entity_id": str(main["id"]),
                "metadata": json.dumps(
                    {
                        "reason": _REASON,
                        "main_reseller_code": _MAIN_CODE,
                        "main_reseller_id": str(main["id"]),
                        "main_is_house_before": bool(main["is_house"]),
                        "main_is_active_before": bool(main["is_active"]),
                        "main_is_house_after": True,
                        "house_reseller_id": str(house["id"]),
                        "house_is_house_before": bool(house["is_house"]),
                        "house_is_active_before": bool(house["is_active"]),
                        "house_is_house_after": False,
                        "house_is_active_after": False,
                        "house_billing_account_ids": [str(i) for i in account_ids],
                        "deactivated_reseller_user_ids": reseller_user_ids,
                    },
                    sort_keys=True,
                ),
            },
        )

    # Index-safe order: the old house row must stop being house before Main
    # can become it.
    bind.execute(
        sa.text(
            "UPDATE resellers SET is_house = false, is_active = false, "
            "updated_at = now() WHERE id = :id"
        ),
        {"id": house["id"]},
    )
    if reseller_user_ids:
        bind.execute(
            sa.text(
                "UPDATE reseller_users SET is_active = false, updated_at = now() "
                "WHERE id IN :ids"
            ).bindparams(sa.bindparam("ids", expanding=True)),
            {"ids": reseller_user_ids},
        )
    bind.execute(
        sa.text(
            "UPDATE resellers SET is_house = true, updated_at = now() WHERE id = :id"
        ),
        {"id": main["id"]},
    )
    print(
        f"{_LOG_PREFIX}: moved the house designation from reseller {house['id']} "
        f"to {_MAIN_CODE} ({main['id']}); deactivated House and "
        f"{len(reseller_user_ids)} reseller_users row(s)."
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if not (
        _has_column(bind, "audit_events", "metadata")
        and _has_column(bind, "resellers", "is_house")
    ):
        return

    audit_row = (
        bind.execute(
            sa.text(
                "SELECT metadata FROM audit_events "
                "WHERE action = :action AND entity_type = :entity_type "
                "  AND actor_id = :actor_id "
                "ORDER BY occurred_at DESC LIMIT 1"
            ),
            {"action": _ACTION, "entity_type": _ENTITY_TYPE, "actor_id": _ACTOR_ID},
        )
        .mappings()
        .first()
    )
    if audit_row is None:
        # upgrade() was a no-op; there is nothing to reverse.
        return
    evidence = audit_row["metadata"]
    if isinstance(evidence, str):
        evidence = json.loads(evidence)

    with _bounded_locks(bind):
        main_is_house = bind.execute(
            sa.text("SELECT is_house FROM resellers WHERE id = :id FOR UPDATE"),
            {"id": evidence["main_reseller_id"]},
        ).scalar_one_or_none()
        if main_is_house is not True:
            print(
                f"{_LOG_PREFIX} downgrade: reseller {evidence['main_reseller_id']} "
                "is no longer the house row; the designation has moved since "
                "upgrade, so nothing is restored."
            )
            return

        # Reverse index-safe order: Main gives up the designation first.
        bind.execute(
            sa.text(
                "UPDATE resellers SET is_house = :is_house, is_active = :is_active, "
                "updated_at = now() WHERE id = :id"
            ),
            {
                "id": evidence["main_reseller_id"],
                "is_house": bool(evidence["main_is_house_before"]),
                "is_active": bool(evidence["main_is_active_before"]),
            },
        )
        bind.execute(
            sa.text(
                "UPDATE resellers SET is_house = :is_house, is_active = :is_active, "
                "updated_at = now() WHERE id = :id"
            ),
            {
                "id": evidence["house_reseller_id"],
                "is_house": bool(evidence["house_is_house_before"]),
                "is_active": bool(evidence["house_is_active_before"]),
            },
        )
        reseller_user_ids = list(evidence.get("deactivated_reseller_user_ids") or [])
        if reseller_user_ids and _has_column(bind, "reseller_users", "id"):
            bind.execute(
                sa.text(
                    "UPDATE reseller_users SET is_active = true, updated_at = now() "
                    "WHERE id IN :ids"
                ).bindparams(sa.bindparam("ids", expanding=True)),
                {"ids": reseller_user_ids},
            )
