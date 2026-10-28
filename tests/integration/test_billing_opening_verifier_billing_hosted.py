"""Hosted-only: Gap B verifier through Billing's real provision -> import -> seal.

Design section 8 steps 8-12 on the migrated PostgreSQL test database (Sub plus
Starter billing schemas). A real census cannot be eligible on a bare test
database (it needs authority cutovers, ledger-backed AR lanes and invoice
lines), so the census/assembly/originals are the synthetic, signed evidence;
Billing, the window principal, the pg_roles and rehearsal-marker observations
are real. The census re-render against real Sub rows is covered by
test_billing_opening_verifier_hosted.py. Each test uses a fresh tenant because
an opening window is permanent per tenant.

No engines or sessions are constructed here (the session-construction ratchet
counts constructions); sessions come from the shared ``census_factory``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
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
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.services.billing import opening_verifier as ov
from tests import opening_verifier_support as s
from tests.integration.test_billing_opening_export import census_factory  # noqa: F401

pytestmark = pytest.mark.integration


class HybridObserver(ov.SqlSourceObservationPort):
    """Real marker and pg_roles observation; the census re-render is controllable."""

    def __init__(self, wire: bytes) -> None:
        self.wire = wire

    def census_wire(
        self, query: object, snapshot_id: str, captured_at: datetime
    ) -> bytes:
        return self.wire


@dataclass
class Env:
    factory: sessionmaker[Session]
    scope: TenantScope
    world: s.World
    observer: HybridObserver
    verifiers: ov.OpeningVerifiers
    principal: tuple[str, int]
    role: list[str | None]


WRITES = (
    "documents posting_groups posting_effects accounting_facts "
    "receivable_position_facts receivable_position_facts_v2 "
    "receivable_exposure_facts_v2 opening_import_receipts opening_cash_lots "
    "opening_cash_effects opening_receivable_bases "
    "opening_historical_discharge_claims cash_source_claims "
    "receivable_source_claims opening_window_admissions opening_window_seals"
).split()


def _principal(factory: sessionmaker[Session]) -> tuple[str, int]:
    """Real NOLOGIN window principal with exact named DML (Starter fixture shape)."""
    role = f"gapb_window_{uuid4().hex[:12]}"
    with factory() as db:
        db.execute(
            sa.text(
                f'CREATE ROLE "{role}" NOLOGIN NOSUPERUSER NOBYPASSRLS '
                "NOCREATEROLE NOCREATEDB NOREPLICATION"
            )
        )
        db.execute(sa.text(f'GRANT USAGE ON SCHEMA public, mod_billing TO "{role}"'))
        for name in billing_models.TENANT_TABLES:
            privileges = "SELECT, INSERT" if name in WRITES else "SELECT"
            db.execute(sa.text(f'GRANT {privileges} ON mod_billing.{name} TO "{role}"'))
        db.execute(
            sa.text(
                f'GRANT UPDATE (generation) ON mod_billing.opening_windows TO "{role}"'
            )
        )
        db.execute(
            sa.text(f'GRANT UPDATE (id) ON mod_billing.billing_accounts TO "{role}"')
        )
        for name in ("idempotency_records", "outbox_events"):
            db.execute(sa.text(f'GRANT SELECT, INSERT ON public.{name} TO "{role}"'))
        oid = db.scalar(
            sa.text("SELECT oid FROM pg_roles WHERE rolname = :r"), {"r": role}
        )
        db.commit()
    return role, int(oid)


@pytest.fixture()
def env(
    census_factory: sessionmaker[Session],  # noqa: F811
    engine: Engine,
    tmp_path: Path,
) -> Iterator[Env]:
    tenant = uuid4()
    scope = TenantScope(tenant)
    held: list[str | None] = [None]  # SET LOCAL ROLE for every transaction

    def begin(
        session: Session, _transaction: object, connection: sa.Connection
    ) -> None:
        if held[0]:
            connection.execute(sa.text(f'SET LOCAL ROLE "{held[0]}"'))
        connection.execute(
            sa.text("SELECT set_config('app.current_tenant', :t, true)"),
            {"t": str(tenant)},
        )

    sa.event.listen(census_factory, "after_begin", begin)
    with census_factory() as db:
        db.execute(
            sa.text(
                "INSERT INTO public.tenants (id, slug, name, is_active) "
                "VALUES (:id, :slug, 'gapb', true)"
            ),
            {"id": tenant, "slug": f"gapb-{tenant.hex[:12]}"},
        )
        db.commit()
    principal = _principal(census_factory)
    accounts = []
    with census_factory() as db:
        for _ in range(2):
            account = billing.create_billing_account(
                db,
                scope=scope,
                external_account_ref=str(uuid4()),
                currency="NGN",
                minor_units=2,
            )
            db.commit()
            accounts.append(
                (
                    account.id,
                    cash_queries.get_opening_cash_target(
                        db, scope=scope, billing_account_id=account.id
                    ),
                )
            )
    assembly = s.assembly()
    assembly["scope"] = {"kind": "tenant", "tenant_id": str(tenant)}
    for row, (account_id, target) in zip(assembly["partitions"], accounts, strict=True):
        row["account_id"] = str(account_id)
        row["expected_position_fingerprint"] = target.expected_position_fingerprint
        row["posting_watermark"] = target.posting_watermark
    with census_factory() as db:
        db.execute(
            sa.text(
                "CREATE TABLE IF NOT EXISTS public.rehearsal_marker "
                "(restore_id text PRIMARY KEY, note text NOT NULL)"
            )
        )
        db.execute(
            sa.text("INSERT INTO public.rehearsal_marker VALUES (:r, 'hosted')"),
            {"r": s.RESTORE_ID},
        )
        db.commit()
    world = s.World(
        tmp_path,
        kind="credit_note",
        assembly_doc=assembly,
        principal_name=principal[0],
    )
    observer = HybridObserver(world.census_bytes)
    world.observer = observer
    try:
        yield Env(
            census_factory, scope, world, observer, world.build(), principal, held
        )
    finally:
        sa.event.remove(census_factory, "after_begin", begin)
        with census_factory() as db:
            db.execute(sa.text("DROP TABLE IF EXISTS public.rehearsal_marker"))
            db.execute(sa.text(f'DROP OWNED BY "{principal[0]}"'))
            db.execute(sa.text(f'DROP ROLE "{principal[0]}"'))
            db.commit()


@contextmanager
def _as_principal(env: Env) -> Iterator[Session]:
    env.role[0] = env.principal[0]
    try:
        with env.factory() as session:
            yield session
    finally:
        env.role[0] = None


def _provision(env: Env) -> OpeningWindowV1:
    w = env.world
    window = OpeningWindowV1(
        env.scope,
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
    with env.factory() as migrator:
        billing.provision_opening_window(
            migrator,
            scope=env.scope,
            command=ProvisionOpeningWindowV1(window),
            verifier=env.verifiers.provision,
        )
        migrator.commit()
    return window


def _import_all(env: Env, session: Session) -> list[object]:
    receipts = [
        billing.import_opening_account_v2(
            session, scope=env.scope, command=c, verifier=env.verifiers.import_v2
        )
        for c in env.world.ev.expected.values()
    ]
    session.commit()
    return receipts


def _seal_command(
    env: Env, session: Session, window: OpeningWindowV1
) -> SealOpeningWindowV1:
    body = env.world.seal_body
    return SealOpeningWindowV1(
        env.scope,
        window.window_id,
        window.epoch,
        window.manifest_fingerprint,
        get_opening_window_admissions(session, scope=env.scope),
        body["coupled_watermark"],
        body["command_key"],
        body["authority_ref"],
        s.NOW,
    )


def test_real_provision_import_replay_and_seal(env: Env) -> None:
    window = _provision(env)
    with _as_principal(env) as session:
        receipts = _import_all(env, session)
        assert len(receipts) == 2 and "gapb1:rehearsal:" in repr(receipts[0])
        assert _import_all(env, session) == receipts  # exact replay, same receipts
        seal = billing.seal_opening_window(
            session,
            scope=env.scope,
            command=_seal_command(env, session, window),
            verifier=env.verifiers.seal,
        )
        session.commit()
        assert get_opening_window_seal(session, scope=env.scope) == seal


def test_writer_movement_after_provision_refuses_the_seal(env: Env) -> None:
    window = _provision(env)
    with _as_principal(env) as session:
        _import_all(env, session)
        command = _seal_command(env, session, window)
        # The legacy writer moves: the census re-render no longer equals the pinned bytes.
        env.observer.wire = env.world.census_bytes.replace(b"100.00", b"101.00", 1)
        with pytest.raises(BillingRuleViolation):
            billing.seal_opening_window(
                session, scope=env.scope, command=command, verifier=env.verifiers.seal
            )
        session.rollback()
        assert get_opening_window_seal(session, scope=env.scope) is None


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
