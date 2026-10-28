"""Hosted-only: Gap B verifier through Billing's real provision -> import -> seal.

Design section 8 steps 8-12 on a fully migrated PostgreSQL (Sub + Starter
billing schemas): real census from seeded Sub rows, signed statements, a real
restricted window principal, Billing's own re-checks, the mutate-then-seal
refusal (the writer fence) and the production-with-marker refusal.

Authored against the pinned a8 contracts; it needs TEST_DATABASE_URL and runs
in hosted CI only.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic.config import Config
from dotmac_billing import models as billing_models
from dotmac_billing import opening_queries as cash_queries
from dotmac_billing import service as billing
from dotmac_billing.errors import BillingRuleViolation
from dotmac_billing.opening_window_contracts import (
    OpeningWindowV1,
    ProvisionOpeningWindowV1,
    SealOpeningWindowV1,
)
from dotmac_billing.opening_window_queries import (
    get_opening_window_admissions,
    get_opening_window_seal,
)
from dotmac_kernel.cache import TenantScope
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, sessionmaker

from alembic import command as alembic
from app import db as app_db
from app.models.billing import Invoice, InvoiceDueDateBasis, InvoiceStatus
from app.models.subscriber import Subscriber
from app.services.billing import opening_verifier as ov
from app.services.billing.opening_census_wire import render_opening_census_wire
from app.services.billing.opening_export import preview_billing_opening_export
from app.services.billing.opening_export_contracts import (
    PreviewBillingOpeningExportQuery,
)
from app.services.operator_tenant import OPERATOR_TENANT_ID
from app.services.subscriber import _default_reseller_id
from tests import opening_verifier_support as s
from tests.integration.test_kernel_lineage_rehearsal import (  # noqa: F401
    isolated_database,
)

pytestmark = pytest.mark.integration
SCOPE = TenantScope(OPERATOR_TENANT_ID)
INSTALLATION = UUID(int=0x32)


@dataclass
class Env:
    engine: sa.Engine
    world: s.World
    verifiers: ov.OpeningVerifiers
    principal: tuple[str, int]
    account_id: UUID
    sub_invoice_id: UUID


def _session(engine: sa.Engine, role: str | None = None) -> Session:
    @sa.event.listens_for(engine, "connect")
    def init(connection, _record):  # noqa: ANN001
        with connection.cursor() as cursor:
            if role:
                cursor.execute(f'SET ROLE "{role}"')
            cursor.execute(
                "SELECT set_config('app.current_tenant', %s, false)",
                (str(OPERATOR_TENANT_ID),),
            )
        connection.commit()

    return Session(engine)


def _grant_principal(engine: sa.Engine) -> tuple[str, int]:
    """Real NOLOGIN window principal with exact named DML, as in Starter's fixture."""
    role = f"gapb_window_{uuid4().hex[:12]}"
    writes = (
        "documents posting_groups posting_effects accounting_facts "
        "receivable_position_facts receivable_position_facts_v2 "
        "receivable_exposure_facts_v2 opening_import_receipts opening_cash_lots "
        "opening_cash_effects opening_receivable_bases "
        "opening_historical_discharge_claims cash_source_claims "
        "receivable_source_claims opening_window_admissions opening_window_seals"
    ).split()
    with engine.begin() as c:
        c.execute(
            sa.text(
                f'CREATE ROLE "{role}" NOLOGIN NOSUPERUSER NOBYPASSRLS '
                "NOCREATEROLE NOCREATEDB NOREPLICATION"
            )
        )
        c.execute(sa.text(f'GRANT USAGE ON SCHEMA public, mod_billing TO "{role}"'))
        for name in billing_models.TENANT_TABLES:
            privileges = "SELECT, INSERT" if name in writes else "SELECT"
            c.execute(sa.text(f'GRANT {privileges} ON mod_billing.{name} TO "{role}"'))
        c.execute(
            sa.text(
                f'GRANT UPDATE (generation) ON mod_billing.opening_windows TO "{role}"'
            )
        )
        c.execute(
            sa.text(f'GRANT UPDATE (id) ON mod_billing.billing_accounts TO "{role}"')
        )
        for name in ("idempotency_records", "outbox_events"):
            c.execute(sa.text(f'GRANT SELECT, INSERT ON public.{name} TO "{role}"'))
        oid = c.scalar(
            sa.text("SELECT oid FROM pg_roles WHERE rolname = :r"), {"r": role}
        )
    return role, int(oid)


@pytest.fixture()
def env(
    isolated_database: URL,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Env]:
    config = Config("alembic.ini")
    config.set_main_option("script_location", "alembic")
    config.set_main_option(
        "sqlalchemy.url", isolated_database.render_as_string(hide_password=False)
    )
    alembic.upgrade(config, "heads")
    engine = sa.create_engine(isolated_database)
    factory = sessionmaker(bind=engine, autoflush=True, autocommit=False)
    monkeypatch.setattr(app_db, "SessionLocal", factory)
    with factory() as writer:
        account = Subscriber(
            first_name="Gapb",
            last_name="Verifier",
            email=f"gapb-{uuid4().hex}@example.invalid",
            reseller_id=_default_reseller_id(writer),
        )
        writer.add(account)
        writer.flush()
        invoices = [
            Invoice(
                account_id=account.id,
                invoice_number=f"GAPB-{uuid4().hex[:8]}-{i}",
                status=InvoiceStatus.issued,
                currency="NGN",
                subtotal=Decimal("100"),
                total=Decimal("100"),
                balance_due=Decimal("100"),
                issued_at=s.ISSUED,
                due_at=s.ISSUED + timedelta(days=30),
                due_date_basis=InvoiceDueDateBasis.unknown_unverified,
                created_at=s.ISSUED,
                updated_at=s.ISSUED,
            )
            for i in range(2)
        ]
        writer.add_all(invoices)
        writer.commit()
        source_id = account.id
        invoice_rows = [(i.id, i.invoice_number) for i in invoices]
    with _session(sa.create_engine(isolated_database)) as migrator:
        billing_account = billing.create_billing_account(
            migrator,
            scope=SCOPE,
            external_account_ref=str(uuid4()),
            currency="NGN",
            minor_units=2,
        )
        migrator.commit()
        target = cash_queries.get_opening_cash_target(
            migrator, scope=SCOPE, billing_account_id=billing_account.id
        )
    query = PreviewBillingOpeningExportQuery(
        "a" * 40,
        INSTALLATION,
        datetime.now(UTC),
        (("NGN", 2),),
        "sub-billing-opening-census-v3",
    )
    with app_db.read_only_snapshot_session() as db:
        wire = render_opening_census_wire(
            preview_billing_opening_export(db, query=query)
        )
    census_doc = json.loads(wire)
    assembly = s.assembly()
    assembly["scope"] = {"kind": "tenant", "tenant_id": str(OPERATOR_TENANT_ID)}
    assembly["partitions"] = [
        {
            "source_account_id": str(source_id),
            "source_account_key": "source-account:hosted",
            "account_id": str(billing_account.id),
            "currency": "NGN",
            "precision": 2,
            "idempotency_key": "opening-rehearsal:hosted",
            "expected_position_fingerprint": target.expected_position_fingerprint,
            "posting_watermark": target.posting_watermark,
        }
    ]
    assembly["invoice_mappings"] = [
        {
            "source_invoice_id": str(inv_id),
            "source_identity": {"kind": "invoice", "key": f"invoice:{n}"},
            "source_invoice_key": f"invoice:{n}",
        }
        for n, (inv_id, _) in enumerate(invoice_rows)
    ]
    assembly["discharge_mappings"] = []
    principal = _grant_principal(engine)
    with engine.begin() as c:
        c.execute(
            sa.text(
                "CREATE TABLE public.rehearsal_marker "
                "(restore_id text PRIMARY KEY, note text NOT NULL)"
            )
        )
        c.execute(
            sa.text("INSERT INTO public.rehearsal_marker VALUES (:r, 'hosted')"),
            {"r": s.RESTORE_ID},
        )
    world = s.World(
        tmp_path,
        census_doc=census_doc,
        assembly_doc=assembly,
        originals_rows=[(str(i), n, 10000) for i, n in invoice_rows],
        principal_name=principal[0],
    )
    world.observer = ov.SqlSourceObservationPort()
    try:
        yield Env(
            engine,
            world,
            world.build(),
            principal,
            billing_account.id,
            invoice_rows[0][0],
        )
    finally:
        engine.dispose()


def _provision(env: Env) -> OpeningWindowV1:
    w = env.world
    window = OpeningWindowV1(
        SCOPE,
        UUID(s.WINDOW_ID),
        UUID(s.EPOCH),
        2,
        w.ev.fingerprint,
        w.ev.manifest,
        env.principal[0],
        env.principal[1],
        w.window["provision_authority_ref"],
        s.NOW,
    )
    with _session(sa.create_engine(env.engine.url)) as migrator:
        billing.provision_opening_window(
            migrator,
            scope=SCOPE,
            command=ProvisionOpeningWindowV1(window),
            verifier=env.verifiers.provision,
        )
        migrator.commit()
    return window


@contextmanager
def _restricted(env: Env) -> Iterator[Session]:
    engine = sa.create_engine(env.engine.url)
    with _session(engine, env.principal[0]) as session:
        yield session


def _import_all(env: Env, session: Session) -> list[object]:
    receipts = []
    for command in env.world.ev.expected.values():
        receipts.append(
            billing.import_opening_account_v2(
                session, scope=SCOPE, command=command, verifier=env.verifiers.import_v2
            )
        )
    session.commit()
    return receipts


def _seal_command(env: Env, session: Session, window: OpeningWindowV1):
    body = env.world.seal_body
    return SealOpeningWindowV1(
        SCOPE,
        window.window_id,
        window.epoch,
        window.manifest_fingerprint,
        get_opening_window_admissions(session, scope=SCOPE),
        body["coupled_watermark"],
        body["command_key"],
        body["authority_ref"],
        s.NOW,
    )


def test_real_provision_import_replay_and_seal(env: Env) -> None:
    window = _provision(env)
    with _restricted(env) as session:
        receipts = _import_all(env, session)
        assert receipts and "gapb1:rehearsal:" in repr(receipts[0])
        assert _import_all(env, session) == receipts  # exact replay, same receipt
        seal_command = _seal_command(env, session, window)
        seal = billing.seal_opening_window(
            session, scope=SCOPE, command=seal_command, verifier=env.verifiers.seal
        )
        session.commit()
        assert get_opening_window_seal(session, scope=SCOPE) == seal


def test_mutation_after_provision_refuses_the_seal(env: Env) -> None:
    window = _provision(env)
    with _restricted(env) as session:
        _import_all(env, session)
        seal_command = _seal_command(env, session, window)
        factory = sessionmaker(bind=env.engine)
        with factory() as writer:  # the legacy writer moves after provision
            invoice = writer.get(Invoice, env.sub_invoice_id)
            assert invoice is not None
            invoice.invoice_number = f"MOVED-{uuid4().hex[:8]}"
            writer.commit()
        with pytest.raises(BillingRuleViolation):
            billing.seal_opening_window(
                session, scope=SCOPE, command=seal_command, verifier=env.verifiers.seal
            )
        session.rollback()
        assert get_opening_window_seal(session, scope=SCOPE) is None


def test_production_profile_with_marker_present_is_refused(
    env: Env, tmp_path: Path
) -> None:
    (tmp_path / "prod").mkdir()
    production = s.World(tmp_path / "prod", environment="production")
    with pytest.raises(ov.OpeningVerifierConstructionError) as info:
        ov.build_opening_verifiers(
            production.profile(),
            production.evidence_paths,
            ov.OpeningStatementPaths(**production.files),
            ov.SqlSourceObservationPort(),
            production.clock,
        )
    assert info.value.code == "marker_forbidden"
