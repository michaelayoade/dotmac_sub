import importlib.util
from pathlib import Path

import pytest

from app.models.billing import CollectionAccount, CollectionAccountType


def _migration():
    path = (
        Path(__file__).resolve().parents[2]
        / "alembic/versions/384_collection_account_payment_details.py"
    )
    spec = importlib.util.spec_from_file_location("migration_373_postgres", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_postgres_migration_enriches_one_unambiguous_identity(db_session) -> None:
    migration = _migration()
    account = CollectionAccount(
        name="Migration PG identity",
        account_type=CollectionAccountType.bank,
        bank_name="ZENITH BANK",
        account_last4="7319",
        currency="NGN",
        is_active=True,
    )
    db_session.add(account)
    db_session.flush()
    accounts = migration._legacy_accounts(
        {
            "direct_bank_transfer_bank_name": "Zenith Bank",
            "direct_bank_transfer_account_name": "Dotmac Technologies Ltd",
            "direct_bank_transfer_account_number": "0000007319",
        }
    )

    ids = migration._migrate_accounts(db_session.connection(), accounts)
    db_session.expire_all()
    migrated = db_session.get(CollectionAccount, account.id)

    assert ids == [str(account.id)]
    assert migrated is not None
    assert migrated.account_number == "0000007319"
    assert migrated.account_last4 == "7319"
    assert migrated.presentment_priority == 1000


def test_postgres_migration_rejects_ambiguous_last4(db_session) -> None:
    migration = _migration()
    db_session.add_all(
        [
            CollectionAccount(
                name="Migration PG ambiguous A",
                account_type=CollectionAccountType.bank,
                bank_name="ZENITH BANK",
                account_last4="8421",
                currency="NGN",
                is_active=True,
            ),
            CollectionAccount(
                name="Migration PG ambiguous B",
                account_type=CollectionAccountType.bank,
                bank_name="ZENITH BANK",
                account_last4="8421",
                currency="NGN",
                is_active=True,
            ),
        ]
    )
    db_session.flush()
    accounts = migration._legacy_accounts(
        {
            "direct_bank_transfer_bank_name": "Zenith Bank",
            "direct_bank_transfer_account_name": "Dotmac Technologies Ltd",
            "direct_bank_transfer_account_number": "0000008421",
        }
    )

    with pytest.raises(RuntimeError, match="same bank share the same last four"):
        migration._migrate_accounts(db_session.connection(), accounts)
