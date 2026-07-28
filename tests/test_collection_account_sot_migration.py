import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa


def _migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/384_collection_account_payment_details.py"
    )
    spec = importlib.util.spec_from_file_location("migration_373", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _schema(bind) -> None:
    bind.execute(
        sa.text(
            "CREATE TABLE collection_accounts ("
            "id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, account_type TEXT, "
            "bank_name TEXT, account_name TEXT, account_number TEXT, "
            "account_last4 TEXT, sort_code TEXT, accounting_code TEXT, "
            "presentment_priority INTEGER NOT NULL DEFAULT 0, currency TEXT, "
            "is_active BOOLEAN, notes TEXT, created_at DATETIME, updated_at DATETIME)"
        )
    )


def test_json_accounts_preserve_order_identity_and_enabled_state() -> None:
    migration = _migration()
    values = {
        "direct_bank_transfer_accounts": """
        [
          {
            "id": "9dd6fc8380a44fc6b23f4b2fc6e7fd55",
            "enabled": "true",
            "bank_name": "Example Bank",
            "account_name": "Dotmac Technologies Ltd",
            "account_number": "0123 456 789",
            "sort_code": "12-34-56"
          },
          {
            "enabled": "false",
            "bank_name": "Second Bank",
            "account_name": "Dotmac Technologies Ltd",
            "account_number": "9876543210"
          }
        ]
        """
    }

    first = migration._legacy_accounts(values)
    second = migration._legacy_accounts(values)

    assert first == second
    assert str(first[0]["id"]) == "9dd6fc83-80a4-4fc6-b23f-4b2fc6e7fd55"
    assert first[0]["bank_name"] == "EXAMPLE BANK"
    assert first[0]["account_number"] == "0123456789"
    assert first[0]["presentment_priority"] > first[1]["presentment_priority"]
    assert first[0]["is_active"] is True
    assert first[1]["is_active"] is False


def test_missing_identity_is_inserted_and_repeat_enriches_same_row() -> None:
    migration = _migration()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as bind:
        _schema(bind)
        accounts = migration._legacy_accounts(
            {
                "direct_bank_transfer_bank_name": "Zenith Bank",
                "direct_bank_transfer_account_name": "Dotmac",
                "direct_bank_transfer_account_number": "0123456789",
            }
        )

        first_ids = migration._migrate_accounts(bind, accounts)
        second_ids = migration._migrate_accounts(bind, accounts)
        rows = bind.execute(
            sa.text(
                "SELECT id, bank_name, account_name, account_number, "
                "account_last4, presentment_priority FROM collection_accounts"
            )
        ).all()

    assert first_ids == second_ids
    assert rows == [
        (
            first_ids[0],
            "ZENITH BANK",
            "Dotmac",
            "0123456789",
            "6789",
            1000,
        )
    ]


def test_unique_last4_identity_is_enriched_without_duplication() -> None:
    migration = _migration()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as bind:
        _schema(bind)
        bind.execute(
            sa.text(
                "INSERT INTO collection_accounts "
                "(id, name, account_type, bank_name, account_last4, currency, "
                "is_active, presentment_priority) VALUES "
                "('11111111-1111-4111-8111-111111111111', "
                "'Existing Splynx identity', 'bank', 'ZENITH BANK', '6789', "
                "'NGN', true, 0)"
            )
        )
        accounts = migration._legacy_accounts(
            {
                "direct_bank_transfer_bank_name": "Zenith Bank",
                "direct_bank_transfer_account_name": "Dotmac",
                "direct_bank_transfer_account_number": "0123456789",
            }
        )

        ids = migration._migrate_accounts(bind, accounts)
        rows = bind.execute(
            sa.text(
                "SELECT id, name, account_number, presentment_priority "
                "FROM collection_accounts"
            )
        ).all()

    assert ids == ["11111111-1111-4111-8111-111111111111"]
    assert rows == [
        (
            ids[0],
            "Existing Splynx identity",
            "0123456789",
            1000,
        )
    ]


def test_ambiguous_last4_fails_closed() -> None:
    migration = _migration()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as bind:
        _schema(bind)
        bind.execute(
            sa.text(
                "INSERT INTO collection_accounts "
                "(id, name, account_type, bank_name, account_last4, currency, "
                "is_active) VALUES "
                "('11111111-1111-4111-8111-111111111111', 'First', 'bank', "
                "'ZENITH BANK', '6789', 'NGN', true), "
                "('22222222-2222-4222-8222-222222222222', 'Second', 'bank', "
                "'ZENITH BANK', '6789', 'NGN', true)"
            )
        )
        accounts = migration._legacy_accounts(
            {
                "direct_bank_transfer_bank_name": "Zenith Bank",
                "direct_bank_transfer_account_name": "Dotmac",
                "direct_bank_transfer_account_number": "0123456789",
            }
        )

        with pytest.raises(RuntimeError, match="same bank share the same last four"):
            migration._migrate_accounts(bind, accounts)


def test_same_last4_at_another_bank_is_not_enriched() -> None:
    migration = _migration()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as bind:
        _schema(bind)
        bind.execute(
            sa.text(
                "INSERT INTO collection_accounts "
                "(id, name, account_type, bank_name, account_last4, currency, "
                "is_active) VALUES "
                "('11111111-1111-4111-8111-111111111111', 'Other bank', 'bank', "
                "'UBA', '6789', 'NGN', true)"
            )
        )
        accounts = migration._legacy_accounts(
            {
                "direct_bank_transfer_bank_name": "Zenith Bank",
                "direct_bank_transfer_account_name": "Dotmac",
                "direct_bank_transfer_account_number": "0123456789",
            }
        )

        ids = migration._migrate_accounts(bind, accounts)
        rows = bind.execute(
            sa.text(
                "SELECT bank_name, account_number FROM collection_accounts "
                "ORDER BY bank_name"
            )
        ).all()

    assert ids != ["11111111-1111-4111-8111-111111111111"]
    assert rows == [("UBA", None), ("ZENITH BANK", "0123456789")]


def test_invalid_or_duplicate_legacy_json_fails_closed() -> None:
    migration = _migration()
    with pytest.raises(RuntimeError, match="not valid JSON"):
        migration._legacy_accounts({"direct_bank_transfer_accounts": "{"})
    with pytest.raises(RuntimeError, match="Duplicate"):
        migration._legacy_accounts(
            {
                "direct_bank_transfer_accounts": """
                [
                  {"bank_name":"A", "account_name":"Dotmac", "account_number":"1234"},
                  {"bank_name":"a", "account_name":"Dotmac", "account_number":"1234"}
                ]
                """
            }
        )
