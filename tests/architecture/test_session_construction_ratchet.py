"""Runtime-readiness inventory: a two-directional ratchet on local
engine/session construction outside `app.db`'s canonical `SessionLocal`.

This is characterization, not a cutover guard: Sub still owns its own
database/session authority (`app/db.py`), and nothing here adopts
`dotmac_kernel.session_runtime.DatabaseRuntime` or changes a pinned version.
Its only job is to make sure the inventory this ratchet backs
(`docs/sub-runtime-readiness-inventory.md`) cannot silently drift — a new
construction site the Kernel runtime-composition seam would need to account
for must be a reviewed, deliberate baseline edit, not an unnoticed diff.

Mirrors the existing `test_adapter_keyword_service_calls.py` two-directional
shape: grow-or-appear fails immediately; shrink-without-lowering-the-baseline
also fails, so the debt figure stays trustworthy in both directions.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

from tests.architecture.session_construction_inventory import (
    TEST_ROOTS,
    construction_sites,
    counts_by_file,
    historical_residue_paths,
    production_counts_by_file,
    production_residue_counts_by_file,
    raw_production_counts_by_file,
    test_fixture_total_count,
)

BASELINE = Path("tests/architecture/session_construction_baseline.txt")

#: Aggregate, informational count of construction sites under `tests/`.
#: Deliberately NOT a per-file baseline: test fixtures legitimately construct
#: one ad hoc engine per test, and per-file churn there says nothing about
#: runtime readiness. This total is still two-directional so a wholesale
#: change in how tests touch the database (e.g. every fixture switching to a
#: new async pattern) cannot pass unnoticed either.
#: +2 from tests/integration/test_integration_inbox_concurrency.py: two new
#: two-session Postgres concurrency tests for the payment-inbox lease
#: (reclaim-vs-reclaim and reclaim-vs-completion), each with its own
#: `sessionmaker`, mirroring the file's existing test fixture pattern.
#: +2 from tests/integration/test_auth_session_refresh_concurrency.py: the
#: setup/verification factory and two concurrent owner-command sessions.
#: +1 from tests/integration/test_tr069_active_cpe_identity_concurrency.py:
#: a concurrent-write proof for migration 595_active_cpe_identity's new
#: partial unique index (two sessions racing to activate the same
#: cpe_device_id), reviewed test-fixture authorship for the CPE identity
#: cardinality program.
#: +1 from tests/integration/test_prepaid_funding_trigger_execution_rollback.py:
#: a real-Postgres two-connection proof that a forced failure in the
#: funding-consequence owner's own transaction rolls back only that
#: transaction, following the established two-real-connection pattern.
#: +2 from tests/integration/test_prepaid_renewal_nightly_isolation.py: two
#: real-Postgres tests (ambiguous-account isolation, unclassified-failure
#: abort) each with their own `sessionmaker`, driving the single-owner
#: funding-consequence fix through the real nightly entry point.
#: +1 from tests/integration/test_prepaid_account_lock_compatibility.py: a
#: deterministic two-session PostgreSQL proof that account-writer
#: serialization remains compatible with the prepaid review item's subscriber
#: foreign key.
#: +1 from tests/integration/test_prepaid_renewal_nightly_isolation.py: the
#: teardown session that relinquishes the test-created global authority-cutover
#: marker after independently committed nightly-owner transactions.
#: +2 from tests/test_eg8145v5_wifi_capability_migration.py: isolated in-memory
#: migration rehearsals for repairing and seeding the model capability each own
#: the disposable engine that Alembic Operations executes against.
#: +1 from tests/test_erp_staff_talk_mapping_permission_migration.py: one
#: isolated in-memory rehearsal proves the permission migration is idempotent
#: and reversible without sharing state with the application test database.
#: +2 from tests/integration/test_team_inbox_contact_link_concurrency.py: two
#: PostgreSQL concurrency proofs for reviewed endpoint links and first-inbound
#: identity claims, each with its own `sessionmaker`.
#: +1 from tests/integration/test_invoice_accounting_sync_keyset_postgres.py: a
#: real dual-session PostgreSQL proof that the accounting-sync feed's keyset
#: cursor does not skip a stationary row under a genuinely concurrent,
#: independently-committing writer, with its own `sessionmaker`.
#: +4 from tests/test_chat_widget_media_transaction.py: two isolated SQLite
#: transaction-boundary proofs each construct one disposable Engine and Session.
#: +8 from ADR 0017 enforcement evidence proofs: the out-of-band writer tests
#: (tests/test_enforcement_application_writer.py) each bind a disposable SQLite
#: Engine/sessionmaker in place of db_session_adapter.create_session, and the
#: PostgreSQL durability proofs
#: (tests/integration/test_enforcement_application_evidence_durability.py) use
#: their own sessionmaker to observe the evidence from a fresh connection after
#: the caller rolls back.
#: +2 from tests/test_enforcement_evidence_shadow_report.py: a private,
#: per-test SQLite engine/sessionmaker for the ADR 0017 shadow-report query,
#: isolated like the writer tests (EnforcementApplication carries no FKs).
#: +1 from test_communication_intent_coverage_pg.py and +9 from
#: test_payment_email_composition_pg.py: real-role, isolation and concurrency
#: fixtures. The production construction baseline remains unchanged.
#: +3 from the Kernel a97 machine alignment proof: one disposable SQLite
#: engine and Session in tests/test_machine_kernel_alignment.py, plus one
#: Connection-bound Session in tests/integration/test_machine_kernel_alignment_pg.py
#: for a real app_user/RLS read. Exact AST count: 2 + 1.
#: +1 from tests/integration/machine_cli_probe.py: a standalone, explicitly
#: marked disposable-cluster probe constructs its oracle engine before it
#: launches a child process that uses the actual app.db.SessionLocal.
#: +3 from tests/integration/test_notification_template_seed_concurrency.py:
#: one migrated PostgreSQL engine plus two independently committing sessions
#: prove concurrent startup seeders safely converge on one template per key.
#: +2 from tests/integration/test_period_purchase_concurrency_pg.py: a private,
#: migrated database engine and a sessionmaker for independently committing
#: checkout owners. These prove serialization without contaminating shared
#: fixture state. No production construction allowance is added.
#: +2 from tests/integration/test_test_connection_finance.py: independently
#: committing sessions prove same-customer creation serialization and rollback
#: of a request and its outbox event. They use the shared, migrated PostgreSQL
#: engine fixture; no production engine/session construction is introduced.
#: +5 from tests/integration/test_subscription_test_connection.py: two
#: disposable migrated engines and three real sessions prove additive RBAC
#: migration and serialized subscription activation. Production remains unchanged.
#: +1 from test_support_ticket_comment_idempotency_concurrency.py: an independent
#: worker session proves ticket-scoped duplicate submissions serialize correctly.
#: +12 from tests/integration/test_vendor_work_order_execution.py: disposable
#: migrated engines and independent sessions prove assignment, revocation and
#: actor lock-order concurrency; no production construction allowance is added.
#: +1 from test_prepaid_paid_invoice_period_repair_concurrency.py: independent
#: worker sessions prove concurrent four-eyes period-repair approvals serialize.
#: +2 from test_prepaid_renewal_origin_correction_concurrency.py and
#: test_legacy_over_allocation_correction_concurrency.py: independent worker
#: sessions prove concurrent reviewed corrections serialize and converge.
#: +1 from tests/integration/test_password_auth_credential_race.py: independent
#: worker sessions prove the credential-version gate, MFA completion and
#: recovery-code spend serialize against a concurrent reset.
#: +2 from tests/test_credential_administration.py: independent read/write
#: sessions prove retained ORM objects are refreshed by locked owner admission.
#: Production preparation uses app.db.SessionLocal, adding no construction debt.
#: +1 from tests/integration/test_email_verification_intents_pg.py: independent
#: migrated PostgreSQL sessions prove exact Subscriber locking, request replay,
#: and the existing dedupe unique constraint. No production site is added.
#: +1 from tests/customer_profile_owner_support.py: canonical profile tests use
#: a fresh Session within fixture isolation, preserving the caller Session.
#: This is fast-unit evidence only; no production construction site is added.
#: +8 from authentication-denial proofs: tests/test_authentication_denial_audit.py
#: adds one disposable file-SQLite Engine and six explicit Session sites for
#: independent lifecycle, observation, cleanup-failure, lock preservation and
#: pending/flushed request-write isolation. tests/authentication_denial_unit.py
#: adds one explicitly logic-only savepoint Session factory. PostgreSQL denial
#: proofs reuse the existing migrated factory. No runtime baseline grows.
#: +6 from tests/integration/test_identity_lock_order_concurrency.py: one
#: disposable migrated test-engine factory, one setup Session and four explicit
#: independent sessionmaker sites prove implicit FK blocking, incumbent lock
#: inversion, tuple arbitration and stale principal-reference refusal. These
#: are reviewed test fixtures; the production construction baseline is unchanged.
#: +10 from tests/integration/test_oidc_atomic_session_concurrency.py: one
#: migrated test-engine factory and nine explicitly isolated Session sites
#: pin backend identities while testing admission/reset serialization, binding
#: drift, ceremony replay and device supersession. No runtime allowance grows.
#: +3 from the OIDC commit-capable unit lane: tests/oidc_atomic_unit.py owns
#: one disposable Engine and one engine-bound Session per test; the external
#: root refusal regression adds one Connection-bound Session. These fixtures
#: exercise real root completion without bypassing the production entry guard.
#: +2 from tests/test_tx_trace.py: explicit helper Session counterexamples
#: reject independent state/audit root attribution.
#: +10 from atomic session-revocation proofs: eight independent PostgreSQL
#: Session sites force exact lock interleavings; two SQLite Session sites prove
#: caller-flushed-write preservation and independent committed observation.
#: Test-only additions; production construction allowance is unchanged.
#: Reseller standing PostgreSQL cases add two isolated Session constructors.
#: These test fixtures do not expand the production construction allowance.
# Six explicit fixture constructions in test_anonymous_recovery_adapter.py
# exercise fresh lifecycle rollback and independent file-SQLite connections.
# The production constructor inventory remains unchanged.
#: +2 from tests/test_credential_security_inventory.py:52,99: disposable SQLite
#: engines prove bounded SQL aggregates and shared UTC age-band cutoffs. These
#: are fast unit fixtures, not PostgreSQL acceptance or production construction.
#: +1 from tests/test_argon2id_benchmark.py: a disposable SQLite engine proves
#: the benchmark report path without adding a production construction site.
#: +2 from tests/test_auth_flow_helpers.py and
#: tests/test_admin_system_route_guards.py: fresh owner-command Sessions prove
#: logout and administrative session revocation commit independently of the
#: adapter's request Session. These are test-only transaction-boundary fixtures.
#: Recovery readiness adds one synthetic engine and one isolated PostgreSQL
#: Session fixture. These two test sites do not expand production allowances.
#: Five isolated PostgreSQL Session constructions in
#: test_api_key_revocation_concurrency.py exercise administrative key revocation
#: locking and rollback. Production construction allowances are unchanged.
#: Subscriber-backed reseller MFA API composition adds one Connection-bound
#: owner Session in a fast unit fixture. Production allowances are unchanged.
#: Customer MFA reset races add one migrated-PostgreSQL sessionmaker fixture.
#: Subscriber enrollment adds one file-backed SQLite engine and four unit
#: Sessions to prove independent denial evidence survives owner rollback.
#: JSON API-key issuance adds five migrated-PostgreSQL Session sites for
#: independent atomicity and uniqueness observers; production is unchanged.
#: JSON API-key PATCH adds five migrated-PostgreSQL Session sites for exact-key
#: locking, null/constraint rollback and independent observers; production is unchanged.
#: Web API-key rotation/rollback tests add three migrated-PostgreSQL Session
#: sites (worker, lock blocker and audit rollback); production is unchanged.
#: Accepted router-health test helpers add a private SQLite engine and
#: sessionmaker for out-of-band evidence rollback tests (ADR 0018).
#: +18 from four disposable, real-migration PostgreSQL acceptance harnesses:
#: identity locking +4; customer-rating concurrency/transfer +9; fiber owner
#: isolation/replay +2; vendor attending-person concurrency +3. These private
#: cloned-database engines and independent sessions establish lock ordering,
#: atomicity and stale-scope denial; no production allowance is added.
#: +18 from test_project_engineering_plan_acceptance.py: one migrated private
#: engine and seventeen independent setup/worker/verification Session sites
#: exercise approval provenance, atomicity, revocation and concurrency.
#: These are disposable test fixtures, not production construction allowances.
#: +19 from tests/integration/test_project_work_order_issuance_acceptance.py:
#: independently committed sessions for duplicate issuance, rollback, immutable
#: receipt, revocation/supersession and Project-before-task lock races.
#: +31 from migrated completion-review acceptance: independently committed
#: sessions prove duplicate intent, retention/deletion races and current access.
#: +2 fast completion-review unit sessions exercise stale authorization refresh.
#: +6 fast engineering adapter/read/participant sessions exercise typed read
#: permissions and rendering; these are test-only, not production allowances.
#: +55 net from the final universal-completion/support slice: issuance PG
#: 19->16 (-3), completion PG 31->51 (+20), completion participant +1,
#: completion adapter +7, engineering-plan unit +1, issuance unit +2 and
#: support PG +27. All construct disposable test sessions; the 17 production
#: sites and their exact baseline are unchanged. Case counts are not site counts.
#: +113 from reviewed recovery and accountable follow-up fixtures: native
#: replacement PG +34, engineering replacement PG +47, follow-up PG +17,
#: incident correlation PG +14 and replacement adapter +1. Independently
#: committing disposable sessions exercise provenance and lock races; no
#: production allowance changes.
#: +94 from shared-outage/project completion acceptance: shared PG +34,
#: shared committed-session helpers +9, project PG +49, project unit +2.
#: These exercise SQL guards and races against migrated disposable schemas.
#: Production construction allowance remains unchanged.
#: +18 from savepoint-safe completion evidence: four truthful terminal-history
#: sessions, five project outer-transaction regression sessions, and nine
#: dedicated full-XID/savepoint proof sessions. No production allowance grows.
#: +9 completes the savepoint proof: three independently committing replacement
#: lineage sessions and six enrollment/submission/review outer-transaction sites.
#: +10 from canonical fiber/radio project entry acceptance: independent setup,
#: issue/review and verification sessions. No production allowance grows.
#: +5 from the default-evidence radio regression: independent photo/signoff
#: gate and recorded evidence checks; production remains unchanged.
#: Reconciliation with current main preserves its recovery-readiness and API-key
#: revocation fixtures alongside the lifecycle acceptance fixtures above. The
#: canonical AST inventory is 657 sites across 143 test files: latest main adds
#: seven subscriber-MFA fixture sites to the lifecycle inventory of 650. Production
#: remains 17 sites; its exact per-file baseline is unchanged.
#: +5 from engineering-plan acceptance: independent Sessions prove native
#: held/blocked/done tasks refuse issuance after approval. The canonical AST
#: inventory is now 662 sites across 143 test files; production remains 17.
#: Reconciliation with main a287 preserves fifteen additional fixture sites:
#: JSON issuance +5, JSON PATCH +5, web key administration +3, and accepted
#: router-health evidence +2. The canonical AST inventory is 677 sites across
#: 147 test files; production remains 17 with its exact baseline unchanged.
#: Staff deletion adds fourteen migrated-PostgreSQL Session sites for pinned
#: workers, FK/lock blockers and independent rollback observers; production is unchanged.
#: API-key authentication maintenance adds ten migrated-PG Session sites
#: and four unit-only construction sites for fresh-owner and bounded-pool tests.
#: Customer hard-delete characterization adds one migrated-clone engine and
#: one Session; production construction allowances remain unchanged.
#: Customer atomic deletion adds ten migrated-PostgreSQL Session sites for
#: physical lock/confirmation interleavings, six network participant fixtures,
#: and two additional characterization Session sites for fresh owner isolation.
#: These eighteen test-only sites do not expand production construction allowances.
#: Reconciliation with main 81c4b4ccd preserves these 48 additional fixture
#: sites alongside the lifecycle acceptance fixtures: the canonical AST scanner
#: measures 725 sites across 153 test files. Production remains 17 sites across
#: 15 files, with its exact per-file baseline unchanged.
TEST_FIXTURE_BASELINE_TOTAL = 725


def _baseline() -> dict[str, int]:
    allowed: dict[str, int] = {}
    for line in BASELINE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        path, _, count = line.rpartition(" ")
        allowed[path] = int(count)
    return allowed


def test_no_new_local_session_construction_in_production_surfaces() -> None:
    allowed = _baseline()
    current = production_counts_by_file()

    new_files = sorted(set(current) - set(allowed))
    assert not new_files, (
        "These files construct a SQLAlchemy Engine or Session outside "
        "app.db's canonical SessionLocal factory, and are not in the "
        "runtime-readiness inventory baseline. Either route through "
        "app.db.SessionLocal, or add the site to "
        "tests/architecture/session_construction_baseline.txt and describe "
        "it in docs/sub-runtime-readiness-inventory.md — a shared Kernel "
        f"DatabaseRuntime would need to account for it: {new_files}"
    )

    grew = sorted(
        f"{path}: {count} > {allowed[path]} allowed"
        for path, count in current.items()
        if count > allowed[path]
    )
    assert not grew, (
        "New local engine/session construction was added to a file already "
        "carrying this debt. Update the baseline and the inventory doc "
        f"deliberately if the new site is intended: {grew}"
    )


def test_session_construction_baseline_has_no_stale_entries() -> None:
    """A file that no longer constructs its own engine/session must leave the
    baseline, so a retired site cannot silently reappear under cover of an
    old allowance."""

    current = production_counts_by_file()
    stale = sorted(
        f"{path} (recorded {count}, now {current.get(path, 0)})"
        for path, count in _baseline().items()
        if current.get(path, 0) < count
    )
    assert not stale, (
        "These baseline counts are higher than reality — lower or remove "
        f"them so the ratchet keeps its grip: {stale}"
    )


def test_test_fixture_engine_family_is_swept() -> None:
    """`tests/` is a real entry-point family (fixtures, migration rehearsals,
    playwright harnesses) and must stay in the sweep, even though it is not
    held to the same per-file baseline as production code."""

    total = test_fixture_total_count()
    assert total <= TEST_FIXTURE_BASELINE_TOTAL, (
        f"tests/ now constructs {total} local engines/sessions, more than the "
        f"recorded {TEST_FIXTURE_BASELINE_TOTAL}. Raise "
        "TEST_FIXTURE_BASELINE_TOTAL deliberately if this growth is expected "
        "test-fixture authorship, not an unreviewed new pattern."
    )
    assert total >= TEST_FIXTURE_BASELINE_TOTAL, (
        f"tests/ now constructs only {total} local engines/sessions, fewer "
        f"than the recorded {TEST_FIXTURE_BASELINE_TOTAL}. Lower "
        "TEST_FIXTURE_BASELINE_TOTAL so the count stays trustworthy, or "
        "confirm no fixtures were silently dropped."
    )
    # `tests/` is exactly what TEST_ROOTS names — this pins the family this
    # test sweeps, so a future rename doesn't quietly stop scanning it.
    assert TEST_ROOTS == ("tests",)


def test_historical_residue_is_excluded_from_the_live_ratchet_but_still_swept() -> None:
    """Sensitivity proof (plant + near-miss, on the real repository): the
    LIVE production baseline excludes historical residue from a fully
    decommissioned integration, but the exclusion is not vacuous — the raw,
    unfiltered sweep still finds every one of those files, and a genuinely
    live file with real construction sites is never swept up by mistake.

    This is the residue-exclusion mechanism's own sensitivity proof: if the
    ledger it defers to changed shape and the exclusion silently stopped
    matching anything, `residue` below would be empty and this test would
    catch it immediately, rather than letting retirement residue quietly
    reappear as an unaccounted "new file" in the live ratchet.
    """

    residue = historical_residue_paths()
    raw = raw_production_counts_by_file()
    live = production_counts_by_file()
    residue_counts = production_residue_counts_by_file()

    # Plant: at least one production construction site is real residue, so
    # the exclusion has something to do — it is not exercising an empty set.
    residue_sites_found = {path for path in raw if path in residue}
    assert residue_sites_found, (
        "No production construction site currently matches the decommissioned "
        "integration's frozen surface. If that surface has genuinely shrunk "
        "to zero for production code, this assertion should be updated "
        "deliberately — but an empty set here means the exclusion mechanism "
        "cannot be proven to do anything."
    )

    # The residue set must be excluded from the LIVE view...
    assert not (residue_sites_found & set(live)), (
        f"These historical-residue files leaked into the LIVE production "
        f"baseline view: {sorted(residue_sites_found & set(live))}"
    )
    # ...but still accounted for, separately, in the residue view.
    assert residue_sites_found <= set(residue_counts)

    # Near-miss: a real, currently-live file with its own construction site
    # (unrelated to the decommissioned integration) must NOT be swept into
    # the residue exclusion by accident.
    assert "app/db.py" in live
    assert "app/db.py" not in residue_counts
    assert "app/db.py" not in residue

    # raw == live ∪ residue, with no overlap — the partition is exhaustive.
    assert set(raw) == set(live) | set(residue_counts)
    assert not (set(live) & set(residue_counts))
    assert sum(raw.values()) == sum(live.values()) + sum(residue_counts.values())


def _sites_in(source: str) -> list[tuple[int, str]]:
    tree = ast.parse(textwrap.dedent(source))
    return construction_sites(tree)


def test_scanner_detects_a_planted_direct_engine_construction() -> None:
    """Sensitivity proof (plant): a real, unauthorized call site is named."""

    planted = """
        from sqlalchemy import create_engine

        def get_rogue_engine():
            return create_engine("postgresql://example/rogue")
    """
    sites = _sites_in(planted)
    assert sites == [(5, "create_engine")], (
        "The scanner must name the planted create_engine(...) call site by "
        f"line and canonical constructor name; got {sites}"
    )


def test_scanner_detects_a_planted_bound_session_construction() -> None:
    """Sensitivity proof (plant): a bound ORM Session(...) call is named,
    the same shape as app/services/db_session_adapter.py's advisory-lock
    session."""

    planted = """
        from sqlalchemy.orm import Session

        def rogue_session(conn):
            return Session(bind=conn, autoflush=False)
    """
    sites = _sites_in(planted)
    assert sites == [(5, "Session")]


def test_scanner_ignores_comment_and_docstring_mentions() -> None:
    """Sensitivity proof (near-miss): prose that names these symbols, with no
    actual call, must not be flagged."""

    near_miss = '''
        """This module used to call sessionmaker() directly; see app.db for
        the real create_engine() call and Session construction now."""

        # TODO: stop hand-rolling a create_engine call here.
        SESSIONMAKER_DOC = "sessionmaker"
    '''
    assert _sites_in(near_miss) == []


def test_scanner_ignores_an_unrelated_class_named_session() -> None:
    """Sensitivity proof (near-miss): a domain model class named `Session`
    that has nothing to do with SQLAlchemy session construction — the exact
    real-repository shape at app/models/auth.py's `class Session(Base):`."""

    near_miss = """
        from app.db import Base

        class Session(Base):
            __tablename__ = "sessions"
    """
    assert _sites_in(near_miss) == []


def test_scanner_ignores_bare_session_call_without_a_sqlalchemy_import() -> None:
    """Sensitivity proof (near-miss): a bare `Session(...)` call where
    `Session` was never imported from a `sqlalchemy*` module is not this
    module's construction primitive — it must not be assumed to be one."""

    near_miss = """
        from app.models.auth import Session

        def touch(existing_row_id):
            return Session(id=existing_row_id)
    """
    assert _sites_in(near_miss) == []


def test_real_repository_near_miss_stays_unflagged() -> None:
    """The actual `class Session(Base):` in app/models/auth.py must not be
    counted — proof against the real file, not only a synthetic stand-in."""

    counts = counts_by_file(("app/models",))
    assert "app/models/auth.py" not in counts, (
        "app/models/auth.py should only ever appear in this inventory if it "
        "gains a REAL SQLAlchemy engine/session construction call, not "
        "because of its unrelated `class Session(Base):` domain model."
    )
