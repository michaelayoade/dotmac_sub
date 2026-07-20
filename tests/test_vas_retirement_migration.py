"""VAS retirement preserves evidence and fails closed on unresolved money."""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "300_retire_vas_runtime.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "vas_retirement_migration", _MIGRATION_PATH
)
assert _SPEC and _SPEC.loader
retirement = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(retirement)


def _connection():
    engine = sa.create_engine("sqlite://")
    connection = engine.connect()
    metadata = sa.MetaData()
    sa.Table(
        "vas_wallet_entries",
        metadata,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("wallet_id", sa.String, nullable=False),
        sa.Column("entry_type", sa.String, nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("reference", sa.String),
    )
    sa.Table(
        "vas_transactions",
        metadata,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("request_id", sa.String, nullable=False),
        sa.Column("token_encrypted", sa.Text),
    )
    sa.Table(
        "vas_refund_requests",
        metadata,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("status", sa.String, nullable=False),
    )
    sa.Table(
        "topup_intents",
        metadata,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("metadata", sa.JSON),
    )
    sa.Table(
        "domain_settings",
        metadata,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("domain", sa.String, nullable=False),
    )
    sa.Table(
        "scheduled_tasks",
        metadata,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("task_name", sa.String, nullable=False),
    )
    sa.Table(
        "permissions",
        metadata,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("key", sa.String, nullable=False),
    )
    for table_name in (
        "role_permissions",
        "subscriber_permissions",
        "system_user_permissions",
    ):
        sa.Table(
            table_name,
            metadata,
            sa.Column("id", sa.String, primary_key=True),
            sa.Column("permission_id", sa.String, nullable=False),
        )
    metadata.create_all(connection)
    return connection, metadata


def test_retirement_blocks_non_zero_customer_liability():
    connection, metadata = _connection()
    entries = metadata.tables["vas_wallet_entries"]
    connection.execute(
        entries.insert(),
        {
            "id": "credit",
            "wallet_id": "wallet-1",
            "entry_type": "credit",
            "amount": 10,
        },
    )

    with pytest.raises(RuntimeError, match="non_zero_wallets=1"):
        retirement._assert_safe_cutover(connection)


def test_retirement_blocks_pending_provider_workflow():
    connection, metadata = _connection()
    topups = metadata.tables["topup_intents"]
    connection.execute(
        topups.insert(),
        {
            "id": "topup-1",
            "status": "pending",
            "metadata": {"payment_flow": "vas_wallet_topup"},
        },
    )

    with pytest.raises(RuntimeError, match="pending_gateway_topups=1"):
        retirement._assert_safe_cutover(connection)


def test_retirement_blocks_failed_purchase_with_unrestored_debit():
    connection, metadata = _connection()
    connection.execute(
        metadata.tables["vas_wallet_entries"].insert(),
        [
            {
                "id": "credit",
                "wallet_id": "wallet-1",
                "entry_type": "credit",
                "amount": 10,
                "reference": "topup-1",
            },
            {
                "id": "debit",
                "wallet_id": "wallet-1",
                "entry_type": "debit",
                "amount": 10,
                "reference": "vas-request-1",
            },
        ],
    )
    connection.execute(
        metadata.tables["vas_transactions"].insert(),
        {
            "id": "txn-1",
            "status": "failed",
            "request_id": "request-1",
            "token_encrypted": None,
        },
    )

    with pytest.raises(RuntimeError, match="non_terminal_purchases=1"):
        retirement._assert_safe_cutover(connection)


def test_retirement_removes_runtime_controls_but_preserves_evidence(monkeypatch):
    connection, metadata = _connection()
    entries = metadata.tables["vas_wallet_entries"]
    connection.execute(
        entries.insert(),
        [
            {
                "id": "credit",
                "wallet_id": "wallet-1",
                "entry_type": "credit",
                "amount": 10,
            },
            {
                "id": "debit",
                "wallet_id": "wallet-1",
                "entry_type": "debit",
                "amount": 10,
            },
        ],
    )
    connection.execute(
        metadata.tables["vas_transactions"].insert(),
        {
            "id": "txn-1",
            "status": "delivered",
            "request_id": "request-1",
            "token_encrypted": "secret",
        },
    )
    connection.execute(
        metadata.tables["vas_refund_requests"].insert(),
        {"id": "refund-1", "status": "succeeded"},
    )
    connection.execute(
        metadata.tables["domain_settings"].insert(),
        [
            {"id": "vas", "domain": "vas"},
            {"id": "billing", "domain": "billing"},
        ],
    )
    connection.execute(
        metadata.tables["scheduled_tasks"].insert(),
        [
            {
                "id": "vas",
                "name": "vas_requery",
                "task_name": "app.tasks.vas.run",
            },
            {
                "id": "billing",
                "name": "billing",
                "task_name": "app.tasks.billing.run",
            },
        ],
    )
    connection.execute(
        metadata.tables["permissions"].insert(),
        [
            {"id": "vas-read", "key": "billing:vas:read"},
            {"id": "billing-read", "key": "billing:invoice:read"},
        ],
    )
    for table_name in (
        "role_permissions",
        "subscriber_permissions",
        "system_user_permissions",
    ):
        connection.execute(
            metadata.tables[table_name].insert(),
            [
                {"id": f"{table_name}-vas", "permission_id": "vas-read"},
                {
                    "id": f"{table_name}-billing",
                    "permission_id": "billing-read",
                },
            ],
        )
    monkeypatch.setattr(retirement.op, "get_bind", lambda: connection)

    retirement.upgrade()

    assert connection.scalar(sa.select(sa.func.count()).select_from(entries)) == 2
    transaction = connection.execute(
        sa.select(metadata.tables["vas_transactions"])
    ).one()
    assert transaction.token_encrypted is None
    assert (
        connection.scalar(
            sa.select(sa.func.count()).select_from(metadata.tables["domain_settings"])
        )
        == 1
    )
    assert (
        connection.scalar(
            sa.select(sa.func.count()).select_from(metadata.tables["scheduled_tasks"])
        )
        == 1
    )
    assert connection.execute(
        sa.select(metadata.tables["permissions"].c.key)
    ).scalars().all() == ["billing:invoice:read"]
    for table_name in (
        "role_permissions",
        "subscriber_permissions",
        "system_user_permissions",
    ):
        assert connection.execute(
            sa.select(metadata.tables[table_name].c.permission_id)
        ).scalars().all() == ["billing-read"]
