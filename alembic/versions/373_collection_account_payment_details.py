"""Make collection accounts the owner of customer-presented bank destinations.

The migration moves the active direct-transfer settings into the owner in the
same transaction as the schema change. Existing Splynx-backed identities are
enriched by last-four match; missing identities are inserted deterministically;
ambiguous or incomplete legacy facts fail the deployment closed.

``accounting_code`` is an external mapping only. Sub does not own a chart of
accounts, journals, or balances for the accounting system.

Revision ID: 373_collection_account_payment_details
Revises: 372_vendor_payment_projection
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

import sqlalchemy as sa

from alembic import op

revision = "373_collection_account_payment_details"
down_revision = "372_vendor_payment_projection"
branch_labels = None
depends_on = None

_ACCOUNT_SETTING_KEYS = (
    "direct_bank_transfer_bank_name",
    "direct_bank_transfer_account_name",
    "direct_bank_transfer_account_number",
    "direct_bank_transfer_sort_code",
    "direct_bank_transfer_accounts",
)


def _setting_values(bind) -> dict[str, str]:
    rows = bind.execute(
        sa.text(
            "SELECT key, value_text FROM domain_settings "
            "WHERE domain = 'billing' AND key IN :keys AND is_active = true"
        ).bindparams(sa.bindparam("keys", expanding=True)),
        {"keys": _ACCOUNT_SETTING_KEYS},
    ).fetchall()
    return {str(key): str(value or "").strip() for key, value in rows}


def _clean_account(raw: object, *, position: int) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise RuntimeError("Direct-transfer account settings must contain objects")
    bank_name = str(raw.get("bank_name") or "").strip().upper()
    account_name = str(raw.get("account_name") or "").strip()
    account_number = "".join(str(raw.get("account_number") or "").split())
    if not (bank_name and account_name and account_number):
        raise RuntimeError(
            "Direct-transfer account settings are incomplete; repair before cutover"
        )
    raw_id = str(raw.get("id") or "").strip()
    try:
        account_id = UUID(raw_id) if raw_id else None
    except ValueError:
        account_id = None
    return {
        "id": account_id
        or uuid5(
            NAMESPACE_URL,
            f"dotmac:collection:{bank_name.casefold()}:{account_number}:NGN",
        ),
        "bank_name": bank_name,
        "account_name": account_name,
        "account_number": account_number,
        "sort_code": str(raw.get("sort_code") or "").strip() or None,
        "is_active": str(raw.get("enabled", "true")).lower()
        in {"1", "true", "yes", "on"},
        # Preserve the settings-list order explicitly. New accounts default to
        # zero, so they cannot become the invoice default merely by name.
        "presentment_priority": 1000 - position,
    }


def _legacy_accounts(values: dict[str, str]) -> list[dict[str, object]]:
    raw_json = values.get("direct_bank_transfer_accounts", "")
    if raw_json:
        try:
            parsed = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "direct_bank_transfer_accounts is not valid JSON"
            ) from exc
        if not isinstance(parsed, list):
            raise RuntimeError("direct_bank_transfer_accounts must be a list")
        accounts = [
            _clean_account(item, position=index) for index, item in enumerate(parsed)
        ]
        identities = [
            (
                str(account["bank_name"]).casefold(),
                str(account["account_number"]),
                "NGN",
            )
            for account in accounts
        ]
        if len(identities) != len(set(identities)):
            raise RuntimeError(
                "Duplicate direct-transfer bank account identity in settings"
            )
        if accounts:
            return accounts

    singular_values = {
        "bank_name": values.get("direct_bank_transfer_bank_name"),
        "account_name": values.get("direct_bank_transfer_account_name"),
        "account_number": values.get("direct_bank_transfer_account_number"),
        "sort_code": values.get("direct_bank_transfer_sort_code"),
    }
    if any(singular_values.values()):
        return [_clean_account(singular_values, position=0)]
    return []


def _unique_name(bind, bank_name: str, last4: str, account_id: UUID) -> str:
    base = f"{bank_name} •••• {last4}"
    existing = bind.execute(
        sa.text("SELECT id FROM collection_accounts WHERE name = :name"),
        {"name": base},
    ).scalar()
    if existing is None or str(existing) == str(account_id):
        return base
    return f"{base} {str(account_id)[:8]}"


def _migrate_accounts(bind, accounts: list[dict[str, object]]) -> list[str]:
    now = datetime.now(UTC)
    migrated_ids: list[str] = []
    for account in accounts:
        number = str(account["account_number"])
        exact_matches = (
            bind.execute(
                sa.text(
                    "SELECT id FROM collection_accounts "
                    "WHERE upper(bank_name) = :bank_name "
                    "AND account_number = :account_number AND currency = 'NGN' "
                    "ORDER BY id"
                ),
                {
                    "bank_name": account["bank_name"],
                    "account_number": number,
                },
            )
            .scalars()
            .all()
        )
        if len(exact_matches) > 1:
            raise RuntimeError(
                "Multiple collection accounts represent the same bank destination"
            )
        existing = exact_matches[0] if exact_matches else None
        if existing is None:
            last4_matches = (
                bind.execute(
                    sa.text(
                        "SELECT id FROM collection_accounts "
                        "WHERE account_type = 'bank' AND account_number IS NULL "
                        "AND upper(bank_name) = :bank_name "
                        "AND account_last4 = :last4 AND currency = 'NGN' ORDER BY id"
                    ),
                    {
                        "bank_name": account["bank_name"],
                        "last4": number[-4:],
                    },
                )
                .scalars()
                .all()
            )
            if len(last4_matches) > 1:
                raise RuntimeError(
                    "Multiple collection accounts at the same bank share the same "
                    "last four digits; resolve identity before cutover"
                )
            existing = last4_matches[0] if last4_matches else None

        account_id = UUID(str(existing or account["id"]))
        values = {
            **account,
            "id": str(account_id),
            "last4": number[-4:],
            "now": now,
        }
        if existing is not None:
            bind.execute(
                sa.text(
                    "UPDATE collection_accounts SET bank_name=:bank_name, "
                    "account_name=:account_name, account_number=:account_number, "
                    "account_last4=:last4, sort_code=:sort_code, "
                    "presentment_priority=:presentment_priority, "
                    "is_active=:is_active, updated_at=:now WHERE id=:id"
                ),
                values,
            )
        else:
            values["name"] = _unique_name(
                bind, str(account["bank_name"]), number[-4:], account_id
            )
            bind.execute(
                sa.text(
                    "INSERT INTO collection_accounts "
                    "(id, name, account_type, bank_name, account_name, "
                    "account_number, account_last4, sort_code, accounting_code, "
                    "presentment_priority, currency, is_active, notes, "
                    "created_at, updated_at) VALUES "
                    "(:id, :name, 'bank', :bank_name, :account_name, "
                    ":account_number, :last4, :sort_code, NULL, "
                    ":presentment_priority, 'NGN', :is_active, NULL, :now, :now)"
                ),
                values,
            )
        migrated_ids.append(str(account_id))
    return migrated_ids


def upgrade() -> None:
    op.add_column(
        "collection_accounts", sa.Column("account_number", sa.String(64), nullable=True)
    )
    op.add_column(
        "collection_accounts", sa.Column("account_name", sa.String(200), nullable=True)
    )
    op.add_column(
        "collection_accounts", sa.Column("sort_code", sa.String(32), nullable=True)
    )
    op.add_column(
        "collection_accounts",
        sa.Column("accounting_code", sa.String(64), nullable=True),
    )
    op.add_column(
        "collection_accounts",
        sa.Column(
            "presentment_priority",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "payment_channels", sa.Column("accounting_code", sa.String(64), nullable=True)
    )

    bind = op.get_bind()
    values = _setting_values(bind)
    accounts = _legacy_accounts(values)
    has_legacy_bank_fact = any(values.get(key) for key in _ACCOUNT_SETTING_KEYS)
    if has_legacy_bank_fact and not accounts:
        raise RuntimeError(
            "Legacy bank-account settings are incomplete; repair before cutover"
        )
    migrated_ids = _migrate_accounts(bind, accounts)
    if len(migrated_ids) != len(accounts):
        raise RuntimeError("Not every legacy bank account reached collection_accounts")

    op.create_index(
        "uq_collection_accounts_bank_number_currency",
        "collection_accounts",
        ["bank_name", "account_number", "currency"],
        unique=True,
        postgresql_where=sa.text(
            "bank_name IS NOT NULL AND account_number IS NOT NULL"
        ),
        sqlite_where=sa.text("bank_name IS NOT NULL AND account_number IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_collection_accounts_bank_number_currency",
        table_name="collection_accounts",
    )
    op.drop_column("payment_channels", "accounting_code")
    op.drop_column("collection_accounts", "presentment_priority")
    op.drop_column("collection_accounts", "accounting_code")
    op.drop_column("collection_accounts", "sort_code")
    op.drop_column("collection_accounts", "account_name")
    op.drop_column("collection_accounts", "account_number")
