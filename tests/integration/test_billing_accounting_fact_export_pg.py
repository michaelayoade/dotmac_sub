"""Migrated PostgreSQL owns race, rollback and additive enum acceptance proof."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest
import sqlalchemy as sa
from alembic.config import Config
from dotmac_billing import AccountingEffectV1, AccountingFactV1
from dotmac_billing.models import (
    AccountingFact,
    BillingAccount,
    PostingGroup,
    ReceivablePositionFact,
    ReceivablePositionFactV2,
)
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.money import Currency, Money
from psycopg import sql
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.models.erp_billing_export import (
    ErpBillingExport,
    ErpBillingFlow,
    ErpExportStatus,
)
from app.services.dotmac_erp.billing_accounting_fact import map_billing_accounting_fact
from app.services.dotmac_erp.billing_adapter import (
    ErpBillingAdapterError,
    StageCommittedBillingFactCommand,
    StagedBillingFact,
    pending_exports,
    record_delivery_outcome,
    stage_committed_billing_fact,
)
from app.services.operator_tenant import OPERATOR_TENANT_ID
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from scripts.ci.immutable_predecessor import (
    ERP_ACCOUNTING_FACT_PREDECESSOR,
    verify_predecessor,
)
from scripts.ci.migrated_test_database import parse_test_database_target

INSTANT = datetime(2026, 10, 13, 12, tzinfo=UTC)
HOST = OwnerCommandDefinition(
    owner="billing.contracts",
    concern="versioned billing contract terms",
    name="record_billing_contract_version",
)


def _context() -> CommandContext:
    return CommandContext.system(
        actor="pytest:accounting-fact",
        scope="accounting-fact-test",
        reason="Synthetic transport acceptance",
        idempotency_key=f"test:{uuid4()}",
    )


def _fact() -> AccountingFactV1:
    return AccountingFactV1(
        scope=TenantScope(OPERATOR_TENANT_ID),
        source_system="billing",
        fact_id=uuid4(),
        fact_version=1,
        billing_account_id=uuid4(),
        posting_group_id=uuid4(),
        source_ref="opaque",
        source_authority="internal",
        effect_kind="invoice_issued",
        occurred_at=INSTANT,
        committed_at=INSTANT,
        effects=(AccountingEffectV1("receivable", Money.of("100", Currency("NGN"))),),
    )


def _seed_billing_fact(db: Session, fact: AccountingFactV1) -> None:
    """Synthetic migrated Billing source rows, never a runtime consumer bridge."""
    envelope = map_billing_accounting_fact(fact)
    db.add(
        BillingAccount(
            id=fact.billing_account_id,
            tenant_id=OPERATOR_TENANT_ID,
            external_account_ref=f"transport-test:{fact.billing_account_id}",
            currency="NGN",
            minor_units=2,
        )
    )
    db.flush()
    db.add(
        PostingGroup(
            id=fact.posting_group_id,
            tenant_id=OPERATOR_TENANT_ID,
            billing_account_id=fact.billing_account_id,
            group_kind="invoice_issued",
            currency="NGN",
            minor_units=2,
            source_ref=fact.source_ref,
            source_version=1,
            occurred_at=INSTANT,
        )
    )
    db.flush()
    # Billing a8 fences posting-group writers at commit: every group must carry
    # exact fresh V1 and V2 position snapshots. This synthetic group has no
    # posting effects, so its exact snapshots are the zero position, shaped as
    # the billing writer emits them (service._post_group).
    snapshot = {
        "tenant_id": OPERATOR_TENANT_ID,
        "billing_account_id": fact.billing_account_id,
        "source_owner": "dotmac-billing",
        "source_version": 1,
        "posting_group_watermark": fact.posting_group_id,
        "currency": "NGN",
        "minor_units": 2,
        "collectible_receivable": Decimal("0"),
        "available_credit": Decimal("0"),
        "prepaid_funding": Decimal("0"),
        "state_fingerprint": "0" * 64,
        "rebuild_fingerprint": "0" * 64,
        "source_authority": "internal",
        "projection_mode": "authoritative",
        "derived_from": "posting_groups",
        "completeness": "complete",
        "completeness_reason_code": None,
        "financial_state": "resolved",
        "observed_at": INSTANT,
    }
    db.add(ReceivablePositionFact(**snapshot))
    db.add(ReceivablePositionFactV2(**snapshot, available_non_cash_credit=Decimal("0")))
    db.flush()
    db.add(
        AccountingFact(
            id=fact.fact_id,
            tenant_id=OPERATOR_TENANT_ID,
            posting_group_id=fact.posting_group_id,
            fact_version=1,
            source_system=fact.source_system,
            source_authority=fact.source_authority,
            effect_kind=fact.effect_kind,
            fact_digest=envelope.fact_digest,
            fact_payload=envelope.to_payload()["fact"],
            occurred_at=INSTANT,
        )
    )
    db.flush()


def _stage(db: Session, fact: AccountingFactV1) -> StagedBillingFact:
    return stage_committed_billing_fact(
        db,
        StageCommittedBillingFactCommand(fact),
        context=_context(),
    )


@pytest.mark.parametrize("conflicting", (False, True))
def test_concurrent_first_insert_replays_or_refuses_changed_fact(
    cloned_database: Callable[[str], URL],
    conflicting: bool,
) -> None:
    engine = sa.create_engine(cloned_database("heads"))
    factory = sessionmaker(engine, expire_on_commit=False)
    fact = _fact()
    try:
        with factory() as db:
            execute_owner_command(
                db,
                definition=HOST,
                context=_context(),
                operation=lambda: _seed_billing_fact(db, fact),
            )
        barrier = Barrier(2)
        other = (
            replace(
                fact,
                effects=(
                    AccountingEffectV1("receivable", Money.of("101", Currency("NGN"))),
                ),
            )
            if conflicting
            else fact
        )

        def concurrent_stage(value: AccountingFactV1) -> StagedBillingFact | str:
            with factory() as db:
                barrier.wait(timeout=30)
                try:
                    return execute_owner_command(
                        db,
                        definition=HOST,
                        context=_context(),
                        operation=lambda: _stage(db, value),
                    )
                except ErpBillingAdapterError as exc:
                    return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(concurrent_stage, value) for value in (fact, other)]
            outcomes = [future.result(timeout=60) for future in futures]
        with factory() as db:
            rows = list(db.scalars(sa.select(ErpBillingExport)))
            assert len(rows) == 1
            assert rows[0].source_id == fact.fact_id
            assert db.get(AccountingFact, fact.fact_id) is not None
        successes = [
            value for value in outcomes if isinstance(value, StagedBillingFact)
        ]
        if conflicting:
            assert len(successes) == 1
            assert (
                sum(
                    isinstance(value, str)
                    and value.endswith("accounting_fact_identity_conflict")
                    for value in outcomes
                )
                == 1
            )
        else:
            assert len(successes) == 2
            assert {value.export_id for value in successes} == {rows[0].id}
            assert sorted(value.replayed for value in successes) == [False, True]
    finally:
        engine.dispose()


def test_owner_rollback_removes_fact_and_export_and_delivery_failure_keeps_fact(
    cloned_database: Callable[[str], URL],
) -> None:
    engine = sa.create_engine(cloned_database("heads"))
    factory = sessionmaker(engine, expire_on_commit=False)
    fact = _fact()
    try:
        with factory() as db:

            def failing_owner() -> None:
                _seed_billing_fact(db, fact)
                _stage(db, fact)
                raise RuntimeError("synthetic caller rollback")

            with pytest.raises(RuntimeError, match="synthetic caller rollback"):
                execute_owner_command(
                    db, definition=HOST, context=_context(), operation=failing_owner
                )
        with factory() as db:
            assert db.get(AccountingFact, fact.fact_id) is None
            assert list(db.scalars(sa.select(ErpBillingExport))) == []
        with factory() as db:

            def succeeding_owner() -> StagedBillingFact:
                _seed_billing_fact(db, fact)
                return _stage(db, fact)

            staged = execute_owner_command(
                db, definition=HOST, context=_context(), operation=succeeding_owner
            )
        with factory() as db:
            assert pending_exports(db)[0].id == staged.export_id
        with factory() as db:
            record_delivery_outcome(
                db,
                export_id=staged.export_id,
                outcome=ErpExportStatus.rejected,
                context=_context(),
                occurred_at=INSTANT,
                error="ERP mapping unavailable",
            )
        with factory() as db:
            assert db.get(AccountingFact, fact.fact_id) is not None
        with factory() as db:
            replay = execute_owner_command(
                db,
                definition=HOST,
                context=_context(),
                operation=lambda: _stage(db, fact),
            )
            assert replay.export_id == staged.export_id and replay.replayed
    finally:
        engine.dispose()


@pytest.fixture
def historical_predecessor_database(template_base_url: URL) -> Iterator[URL]:
    """Build real old source, including its old enum, without Git ref mutations."""
    parse_test_database_target(template_base_url.render_as_string(hide_password=False))
    repository = Path(__file__).resolve().parents[2]
    predecessor = verify_predecessor(
        repository=repository, source=ERP_ACCOUNTING_FACT_PREDECESSOR
    )
    name = f"dotmac_test_erp_fact_predecessor_{uuid4().hex}"
    maintenance = template_base_url.set(database="postgres", drivername="postgresql")
    url = template_base_url.set(database=name)
    with psycopg.connect(
        maintenance.render_as_string(hide_password=False), autocommit=True
    ) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        with TemporaryDirectory(prefix="erp-fact-historical-source-") as directory:
            root = Path(directory)
            archive = subprocess.run(
                [
                    "git",
                    "archive",
                    "--format=tar",
                    predecessor.source.commit,
                ],
                cwd=repository,
                check=True,
                capture_output=True,
                timeout=30,
            )
            subprocess.run(
                ["tar", "-xf", "-", "-C", str(root)],
                input=archive.stdout,
                check=True,
                timeout=30,
            )
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(root)
            environment["DATABASE_URL"] = url.render_as_string(hide_password=False)
            environment["TEST_DATABASE_URL"] = url.render_as_string(hide_password=False)
            old_upgrade = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import os; from sqlalchemy.engine import make_url; "
                        "from scripts.ci.template_database import "
                        "bootstrap_database_local_prerequisites, upgrade_to; "
                        "url=make_url(os.environ['TEST_DATABASE_URL']); "
                        "bootstrap_database_local_prerequisites(url); upgrade_to(url, 'heads')"
                    ),
                ],
                cwd=root,
                env=environment,
                capture_output=True,
                timeout=300,
            )
            assert old_upgrade.returncode == 0, (
                "Immutable historical-source migration failed; acceptance is blocked."
            )
        yield url
    finally:
        with psycopg.connect(
            maintenance.render_as_string(hide_password=False), autocommit=True
        ) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s AND pid<>pg_backend_pid()",
                (name,),
            )
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


def test_real_predecessor_upgrade_preserves_existing_invoice_exports(
    historical_predecessor_database: URL,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    from app import config as app_config

    url = historical_predecessor_database
    monkeypatch.setattr(
        app_config,
        "settings",
        replace(
            app_config.settings, database_url=url.render_as_string(hide_password=False)
        ),
    )
    config = Config("alembic.ini")
    engine = sa.create_engine(url)
    export_id = uuid4()
    try:
        with engine.connect() as connection:
            old_labels = set(
                connection.scalars(
                    sa.text(
                        "SELECT enumlabel FROM pg_enum JOIN pg_type ON pg_type.oid=pg_enum.enumtypid WHERE typname='erpbillingflow'"
                    )
                )
            )
            assert "accounting_fact" not in old_labels
        with Session(engine) as db:
            db.add(
                ErpBillingExport(
                    id=export_id,
                    flow=ErpBillingFlow.invoice,
                    source_kind="invoice",
                    source_id=uuid4(),
                    payload_version=1,
                    payload={"legacy": "preserved"},
                    idempotency_key=f"test:{uuid4()}",
                    command_id=uuid4(),
                    correlation_id=uuid4(),
                )
            )
            db.commit()
        command.upgrade(config, "heads")
        command.upgrade(config, "heads")
        with Session(engine) as db:
            assert db.get(ErpBillingExport, export_id).payload == {
                "legacy": "preserved"
            }
            labels = set(
                db.scalars(
                    sa.text(
                        "SELECT enumlabel FROM pg_enum JOIN pg_type ON pg_type.oid=pg_enum.enumtypid WHERE typname='erpbillingflow'"
                    )
                )
            )
            assert labels == {
                "invoice",
                "credit_note",
                "payment",
                "refund_or_reversal",
                "tax_withholding",
                "correction",
                "accounting_fact",
            }
    finally:
        engine.dispose()
