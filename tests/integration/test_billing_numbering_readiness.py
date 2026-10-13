"""Hosted migrated PostgreSQL proof: snapshot-only census cannot seed or allocate."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date
from uuid import uuid4

import pytest
from dotmac_kernel.settings_cache import active_settings_cache, install_settings_cache
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from app import db as app_db
from app.models.billing import Invoice, InvoiceStatus
from app.models.subscriber import Subscriber
from app.services.billing_numbering_readiness import (
    NumberingReadinessQuery,
    SeriesKind,
    billing_numbering_readiness_report,
)
from app.services.operator_tenant import OPERATOR_TENANT_ID
from app.services.subscriber import _default_reseller_id
from scripts.migration import billing_numbering_readiness

pytestmark = pytest.mark.integration
QUERY = NumberingReadinessQuery(reference_date=date(2026, 10, 13))


@pytest.fixture()
def snapshot_factory(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> sessionmaker[Session]:
    if engine.dialect.name != "postgresql":
        pytest.fail("readiness proof requires real migrated PostgreSQL")
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(app_db, "SessionLocal", factory)
    return factory


@pytest.fixture()
def fresh_settings() -> Iterator[None]:
    previous = active_settings_cache()
    install_settings_cache(None)
    try:
        yield
    finally:
        install_settings_cache(previous)


def state(db: Session) -> tuple[int, ...]:
    tables = (
        "public.document_sequences",
        "mod_numbering.number_series",
        "mod_numbering.series_counters",
        "mod_numbering.allocation_receipts",
        "mod_numbering.series_repairs",
        "public.idempotency_records",
    )
    return tuple(
        db.execute(
            text(f"SELECT count(*) FROM {table}")  # noqa: S608 - fixed table catalogue only
        ).scalar_one()
        for table in tables
    )


def test_cli_uses_migrated_snapshot_without_creating_state(
    snapshot_factory: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    with app_db.read_only_snapshot_session() as db:
        before = state(db)
    previous_store = active_settings_cache()
    status = billing_numbering_readiness.main(
        ["--reference-date", QUERY.reference_date.isoformat()]
    )
    assert active_settings_cache() is previous_store
    payload = json.loads(capsys.readouterr().out)
    assert status == (0 if payload["admissible"] else 1 if payload["complete"] else 2)
    assert payload["tenant_id"] == str(OPERATOR_TENANT_ID)
    for plan in payload["plans"]:
        counts = plan["counts"]
        assert (
            counts["database_total"]
            == counts["scanned"]
            == sum(counts[key] for key in ("null", "empty", "matching", "other"))
        )
    with app_db.read_only_snapshot_session() as db:
        assert state(db) == before


def test_snapshot_remains_stable_across_concurrent_committed_document_change(
    snapshot_factory: sessionmaker[Session], fresh_settings: None
) -> None:
    with snapshot_factory() as writer:
        marker = uuid4().hex
        subscriber = Subscriber(
            first_name="Numbering",
            last_name="Census",
            email=f"numbering-{marker}@example.invalid",
            reseller_id=_default_reseller_id(writer),
        )
        writer.add(subscriber)
        writer.flush()
        # Non-current documents must remain in the census, including inactive
        # drafts and void history. Existing number labels are never repaired.
        invoices = [
            Invoice(
                account_id=subscriber.id,
                invoice_number=f"READINESS-{marker}-{index}",
                status=status,
                is_active=False,
            )
            for index, status in enumerate((InvoiceStatus.draft, InvoiceStatus.void))
        ]
        writer.add_all(invoices)
        writer.commit()
        changed_id = invoices[0].id
    with app_db.read_only_snapshot_session() as db:
        assert (
            db.execute(text("SHOW transaction_isolation")).scalar_one()
            == "repeatable read"
        )
        assert db.execute(text("SHOW transaction_read_only")).scalar_one() == "on"
        assert db.execute(
            text("SELECT current_setting('app.current_tenant', true)")
        ).scalar_one() == str(OPERATOR_TENANT_ID)
        first = billing_numbering_readiness_report(db, query=QUERY)
        before = state(db)
        with snapshot_factory() as writer:
            invoice = writer.get(Invoice, changed_id)
            assert invoice is not None
            invoice.invoice_number = f"CHANGED-{marker}"
            writer.commit()
        second = billing_numbering_readiness_report(db, query=QUERY)
        assert first.evidence_digest == second.evidence_digest
        assert state(db) == before
        invoice_plan = next(
            plan for plan in first.plans if plan.kind is SeriesKind.INVOICE
        )
        assert invoice_plan.counts.other >= 2
    with app_db.read_only_snapshot_session() as db:
        fresh = billing_numbering_readiness_report(db, query=QUERY)
        assert fresh.evidence_digest != first.evidence_digest


def test_read_only_snapshot_rejects_incumbent_and_module_writes(
    snapshot_factory: sessionmaker[Session],
) -> None:
    statements = (
        "UPDATE public.document_sequences SET next_value=next_value+1",
        "UPDATE mod_numbering.series_counters SET next_value=next_value+1",
        "INSERT INTO mod_numbering.number_series (id,tenant_id,series_code,prefix,suffix,separator,min_digits,include_year,year_digits,include_month,reset_policy,start_value) VALUES (gen_random_uuid(),:tenant,'readiness-forbidden','','','',1,0,4,0,'never',1)",
    )
    for statement in statements:
        with app_db.read_only_snapshot_session() as db:
            with pytest.raises(DBAPIError) as excinfo:
                db.execute(text(statement), {"tenant": OPERATOR_TENANT_ID})
            assert "read-only" in str(excinfo.value).lower()


class ForbiddenCacheAccess:
    """Any cache transport use invalidates snapshot-bound evidence."""

    def get(self, key: str) -> object | None:
        raise AssertionError("readiness must not consume a stale cache entry")

    def set(self, key: str, value: object, *, ttl_seconds: int | None = None) -> None:
        raise AssertionError("readiness must not write to the cache transport")

    def delete_prefix(self, prefix: str) -> int:
        raise AssertionError("readiness must not invalidate shared cache state")


def test_cli_reads_changed_database_prefix_without_touching_installed_cache(
    snapshot_factory: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    from sqlalchemy import select

    from app.models.domain_settings import DomainSetting, SettingDomain
    from app.models.subscription_engine import SettingValueType

    prefix = f"FRESH-{uuid4().hex[:12]}-"
    with snapshot_factory() as writer:
        setting = writer.scalar(
            select(DomainSetting).where(
                DomainSetting.domain == SettingDomain.billing,
                DomainSetting.key == "invoice_number_prefix",
                DomainSetting.scope_kind == "tenant",
                DomainSetting.tenant_id == OPERATOR_TENANT_ID,
                DomainSetting.scope_id.is_(None),
            )
        )
        if setting is None:
            setting = DomainSetting(
                domain=SettingDomain.billing,
                key="invoice_number_prefix",
                scope_kind="tenant",
                scope_id=None,
                tenant_id=OPERATOR_TENANT_ID,
                value_type=SettingValueType.string,
                value_text=prefix,
                is_active=True,
            )
            writer.add(setting)
        else:
            setting.value_text = prefix
            setting.value_json = None
            setting.is_active = True
        writer.commit()
    previous = active_settings_cache()
    forbidden = ForbiddenCacheAccess()
    install_settings_cache(forbidden)
    try:
        status = billing_numbering_readiness.main(
            ["--reference-date", QUERY.reference_date.isoformat()]
        )
        assert active_settings_cache() is forbidden
    finally:
        install_settings_cache(previous)
    payload = json.loads(capsys.readouterr().out)
    invoice_plan = next(
        plan for plan in payload["plans"] if plan["kind"] == SeriesKind.INVOICE.value
    )
    assert invoice_plan["configuration"]["prefix"] == prefix
    assert status in (0, 1)
