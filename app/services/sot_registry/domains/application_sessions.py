"""Canonical SOT declarations for the application_sessions domain."""

from __future__ import annotations

from app.services.sot_manifest import (
    AuthorityInput,
    AuthorityKind,
    AuthorityMigrationState,
    ConcernContract,
    ErrorContract,
    EventContract,
    MigrationContract,
    OwnerRole,
    ProjectionContract,
    ServiceContract,
    SOTService,
    TransactionContract,
    TransactionMode,
    owner_command_boundary_error_codes,
)
from app.services.sot_registry.model import DomainSOT

DOMAIN = DomainSOT(
    domain="application_sessions",
    services=(
        SOTService(
            name="app_sessions.store",
            module="app.services.session_store",
            owns=(
                "Redis-backed session storage",
                "session principal indexes",
                "session revocation epochs",
            ),
        ),
        SOTService(
            name="app_sessions.reseller_standing",
            module="app.services.reseller_session_standing",
            owns=(
                "ordinary reseller cookie database standing",
                "ordinary reseller cookie refresh deadline",
            ),
            depends_on=(
                "app_sessions.auth",
                "app_sessions.store",
                "auth.reseller_onboarding",
                "control.settings_spec",
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="ordinary reseller cookie database standing",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "database authentication session state",
                            "current reseller ownership",
                            "bound ordinary cookie identity",
                        ),
                    ),
                    ConcernContract(
                        name="ordinary reseller cookie refresh deadline",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "database authentication session state",
                            "current reseller ownership",
                            "bound ordinary cookie identity",
                            "portal lifetime policy",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="database authentication session state",
                        owner="app_sessions.auth",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="AuthSession exclusive principal/status/revoked_at/expiry",
                    ),
                    AuthorityInput(
                        name="current reseller ownership",
                        owner="auth.reseller_onboarding",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "Reseller active standing; active ResellerUser membership "
                            "for first-class principals; active Subscriber plus "
                            "ResellerUser membership for legacy subscriber principals. "
                            "A first-class user's deprecated Subscriber link is "
                            "historical provenance only."
                        ),
                    ),
                    AuthorityInput(
                        name="bound ordinary cookie identity",
                        owner="app_sessions.store",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source="Serialized exact auth_session_id, principal kind/id, exclusive principal identifiers and reseller_id; never authentication authority",
                    ),
                    AuthorityInput(
                        name="portal lifetime policy",
                        owner="control.settings_spec",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="Configured sliding and absolute TTLs supplied as typed query values",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary="Reads in the caller transaction with no_autoflush; never commit or roll back caller work.",
                    locking="Fresh scalar-column queries with populate_existing execution semantics under caller isolation, no ORM refresh or locks; next request rechecks committed revocation.",
                    idempotency="No mutation or external effects; deterministic for the observed database state and decision clock.",
                    retries="Storage failure propagates for adapter 503; never authorize from cached standing.",
                ),
                errors=ErrorContract(
                    domain_codes=(),
                    mapping_owner="reseller HTTP adapters",
                    fail_closed_on=(
                        "missing or malformed ordinary binding",
                        "principal or reseller ownership mismatch",
                        "inactive, revoked or expired database standing",
                        "database unavailable",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner="Redis-only ordinary reseller cookie standing",
                    new_owner="app_sessions.reseller_standing",
                    verification="Ordinary principal, revocation, stale identity-map, deadline and source-boundary tests.",
                    cutover_gate="Ordinary get_context and refresh require exact current database standing.",
                    fallback_retirement="Unbound ordinary cookies require re-login; explicit legacy impersonation actor/binding remains excluded and unresolved.",
                ),
                steward="platform security",
                design_refs=("docs/designs/RESELLER_COOKIE_DATABASE_STANDING.md",),
                test_refs=(
                    "tests/test_reseller_session_standing.py",
                    "tests/integration/test_reseller_session_standing_pg.py",
                    "tests/architecture/test_reseller_session_standing_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="app_sessions.customer_portal",
            module="app.services.customer_portal_session",
            owns=(
                "customer portal Redis session delivery",
                "customer portal Redis projection refresh/revoke",
                "customer portal database session standing",
                "impersonation/read-only portal session policy",
            ),
            depends_on=(
                "app_sessions.store",
                "customer.identity_scope",
                "auth.password_authentication",
                "app_sessions.auth",
                "control.settings_spec",
            ),
            notes=(
                "Redis transports customer identity only after committed database "
                "standing; password_authentication owns the AuthSession decision, "
                "insert, audit and compensating revoke. No impersonation flag bypass."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="customer portal Redis session delivery",
                        role=OwnerRole.TRANSPORT,
                        input_names=(
                            "committed authentication session",
                            "typed customer session identity",
                            "portal lifetime policy",
                        ),
                    ),
                    ConcernContract(
                        name="customer portal Redis projection refresh/revoke",
                        role=OwnerRole.TRANSPORT,
                        input_names=(
                            "committed authentication session",
                            "serialized Redis projection",
                            "portal lifetime policy",
                        ),
                    ),
                    ConcernContract(
                        name="customer portal database session standing",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "committed authentication session",
                            "typed customer session identity",
                        ),
                    ),
                    ConcernContract(
                        name="impersonation/read-only portal session policy",
                        role=OwnerRole.POLICY,
                        input_names=(
                            "typed customer session identity",
                            "committed authentication session",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="committed authentication session",
                        owner="auth.password_authentication",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="AuthSession id, subscriber, active status, revoked_at and expires_at; issuance and revocation are owned database transitions",
                    ),
                    AuthorityInput(
                        name="typed customer session identity",
                        owner="customer.identity_scope",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="CreateCustomerPortalSession and CustomerSessionStandingQuery bind UUID subscriber/account and optional subscription; existing caller owns impersonation authorization and actor audit",
                    ),
                    AuthorityInput(
                        name="serialized Redis projection",
                        owner="app_sessions.store",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source="Redis or explicitly enabled test memory transport: auth_session_id, subscriber/account, expiry and revocation epoch; never independent authentication proof",
                    ),
                    AuthorityInput(
                        name="portal lifetime policy",
                        owner="control.settings_spec",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="customer session/remember TTL and absolute TTL settings; database standing expires at issuance TTL even if Redis slides",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary="This transport/query owns no database writes or commits. AuthSession issuance, audit and compensation execute in password_authentication before Redis delivery. The standing query returns an immutable result.",
                    locking="Standing is one fresh PK read with populate_existing. Password owner locks principal before source and stages session/audit atomically; Redis operations follow commit.",
                    idempotency="Each delivery generates a new opaque token; repeating delivery never grants standing absent the same active database row. Invalid projections are deleted.",
                    retries="Redis delivery failure yields 503 and best-effort owner compensation; lock_timeout preserves Retry-After 5. No stale-success fallback.",
                ),
                errors=ErrorContract(
                    domain_codes=(),
                    mapping_owner="app.services.web_customer_auth and existing authorized impersonation adapters",
                    fail_closed_on=(
                        "missing/malformed/inactive/revoked/expired/wrong-subscriber AuthSession",
                        "legacy unbound Redis payload including forged impersonation flag",
                        "Redis delivery failure",
                        "subscription does not belong to the bound subscriber",
                    ),
                ),
                projections=(
                    ProjectionContract(
                        name="customer portal Redis session",
                        input_names=(
                            "committed authentication session",
                            "typed customer session identity",
                            "portal lifetime policy",
                        ),
                        writer="app_sessions.store",
                        freshness="Written after database commit; database standing reloaded every authenticated request and refresh.",
                        stale_behavior="Deny and invalidate missing/revoked/expired/unbound projections; impersonation has no exception.",
                        drift_signal="Redis payload absent/malformed or auth_session_id standing mismatch; failed postcommit delivery is reported with no successful cookie.",
                        rebuild_operation="Require fresh authorized password/MFA or impersonation issuance; never reconstruct standing from Redis or username.",
                        repair_owner="app_sessions.customer_portal",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner="Redis-only customer session authentication and adapter-owned password/MFA success",
                    new_owner="app_sessions.customer_portal",
                    verification="Typed creation/standing tests, lifecycle refresh tests, impersonation regressions and PostgreSQL T16/T17/T18 credential/source races.",
                    cutover_gate="Every production customer session creation is AuthSession backed; get_current_customer and refresh require database standing.",
                    fallback_retirement="Legacy ordinary/impersonation Redis sessions require re-login. The compatibility facade only converts cache identifiers to typed commands/queries; there is no flag or username fallback.",
                ),
                steward="platform security",
                design_refs=(
                    "docs/designs/CUSTOMER_PORTAL_CREDENTIAL_STANDING.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_web_customer_auth.py",
                    "tests/test_portal_session_lifecycle.py",
                    "tests/architecture/test_password_authentication_boundary.py",
                    "tests/integration/test_password_auth_credential_race.py",
                ),
            ),
        ),
        SOTService(
            name="app_sessions.auth",
            module="app.services.session_manager",
            owns=(
                "database auth-session listing",
                "database auth-session revocation",
                "exact revoked-session cache projection and repair",
            ),
            depends_on=("app_sessions.store", "events.store", "events.dispatcher"),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="database auth-session listing",
                        role=OwnerRole.RESOLVER,
                        input_names=("database session standing",),
                    ),
                    ConcernContract(
                        name="database auth-session revocation",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "database session standing",
                            "authenticated revocation authority",
                        ),
                        canonical_writer="app_sessions.auth",
                    ),
                    ConcernContract(
                        name="exact revoked-session cache projection and repair",
                        role=OwnerRole.TRANSPORT,
                        input_names=("database session standing",),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="database session standing",
                        owner="app_sessions.auth",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="sessions exact principal foreign keys, status, revocation and refresh-token binding",
                    ),
                    AuthorityInput(
                        name="authenticated revocation authority",
                        owner="external:auth_client",
                        kind=AuthorityKind.EXTERNAL_OBSERVATION,
                        source="current refresh bearer, authenticated user session, administrative actor or vendor admission compensation",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary="revoke_authentication_sessions executes once on a fresh adapter session; canonical audit INSERT, revocation and exact-target outbox commit atomically",
                    locking="principal FOR UPDATE before stable UUID ordered session FOR UPDATE; refreshed exact binding; bulk locks exclude concurrent issuance",
                    idempotency="Revoked rows retain their original timestamp; missing or wrong current session cannot revoke others; logout matches current token hash only",
                    retries="55P03 is HTTP 503 Retry-After 5; durable exact-ID cache cleanup retries independently",
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "app_sessions.auth.not_found",
                        "app_sessions.auth.invalid_principal",
                        "app_sessions.auth.invalid_actor",
                        "app_sessions.auth.invalid_current_session",
                        *owner_command_boundary_error_codes("app_sessions.auth"),
                    ),
                    mapping_owner="authentication API and web adapters",
                    retryable_codes=(),
                    fail_closed_on=(
                        "ambiguous principal binding",
                        "missing authenticated current session",
                        "cross-principal target",
                        "audit or commit failure",
                    ),
                ),
                events=EventContract(
                    event_types=("authentication_sessions.revoked",),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility="Version 1 contains exact UUID targets and no bearer, token hash or cache key",
                    replay="SessionRevocationProjectionHandler rechecks revoked database standing and strictly deletes only listed cache session contexts; failures remain retryable",
                ),
                projections=(
                    ProjectionContract(
                        name="exact revoked authentication session cache",
                        input_names=("database session standing",),
                        writer="app_sessions.auth",
                        freshness="SessionRevocationProjectionHandler consumes the committed exact UUID outbox intent asynchronously; auth requests always recheck database standing",
                        stale_behavior="Database revocation denies access; Redis outage propagates and never restores active standing",
                        drift_signal="Failed SessionRevocationProjectionHandler delivery remains visible in the durable dispatcher handler attempts",
                        rebuild_operation="Replay authentication_sessions.revoked after rechecking exact principal binding and revoked standing for each listed UUID; never invalidate a broad principal epoch",
                        repair_owner="app_sessions.auth",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.CUT_OVER,
                    old_owner="AuthFlow.logout, Sessions.delete and session_manager direct commits",
                    new_owner="app_sessions.auth",
                    verification="focused transaction, actor, exact-binding and projection tests",
                    cutover_gate="ordinary bearer logout, self single/others, administrative DELETE and vendor compensation use one atomic owner command",
                    fallback_retirement="Portal logout, portal signout-others and impersonation remain explicitly outside this slice",
                ),
                steward="platform security",
                design_refs=("docs/designs/AUTH_SESSION_REVOCATION.md",),
                test_refs=(
                    "tests/test_atomic_session_revocation.py",
                    "tests/integration/test_atomic_session_revocation_concurrency.py",
                    "tests/architecture/test_session_revocation_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="app_sessions.refresh",
            module="app.services.auth_session_refresh",
            owns=("concurrency-safe database authentication session renewal",),
            depends_on=(
                "app_sessions.auth",
                "events.dispatcher",
                "events.store",
                "party.staff_session_projection",
                "control.settings_spec",
                "secrets.reference_store",
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name=(
                            "concurrency-safe database authentication session renewal"
                        ),
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "database authentication session state",
                            "presented refresh credential and client binding",
                            "canonical staff session principal projection",
                            "access-token signing policy",
                            "held JWT signing secret",
                        ),
                        canonical_writer="app_sessions.refresh",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="database authentication session state",
                        owner="app_sessions.refresh",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "sessions token hashes, rotation timestamp, status, "
                            "expiry and recorded client binding"
                        ),
                    ),
                    AuthorityInput(
                        name="presented refresh credential and client binding",
                        owner="external:auth_client",
                        kind=AuthorityKind.EXTERNAL_OBSERVATION,
                        source=(
                            "opaque refresh credential plus normalized device, "
                            "user-agent and effective client address observations"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical staff session principal projection",
                        owner="party.staff_session_projection",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "Party-bound staff session principal resolved before "
                            "a staff token rotates"
                        ),
                    ),
                    AuthorityInput(
                        name="access-token signing policy",
                        owner="control.settings_spec",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "effective JWT algorithm and 15-minute default access "
                            "lifetime resolved while the renewal transaction is open"
                        ),
                    ),
                    AuthorityInput(
                        name="held JWT signing secret",
                        owner="secrets.reference_store",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "process-held jwt_secret material loaded at boot; token "
                            "values and signing material are never persisted in events"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "renew_authentication_session enters execute_owner_command "
                        "once and commits rotation, revocation and event evidence "
                        "atomically"
                    ),
                    locking=(
                        "SELECT FOR UPDATE locks the active session matching the "
                        "current or immediately previous refresh-token hash; "
                        "the default decision clock is sampled after acquiring "
                        "the lock so waiting neither revokes a fresh duplicate "
                        "nor extends the five-second replay window"
                    ),
                    idempotency=(
                        "The current token rotates once; the immediately previous "
                        "token replays for the same client for five seconds without "
                        "rotating again"
                    ),
                    retries=(
                        "Concurrent callers wait on the session row. Database "
                        "deadlock or serialization failures remain retryable by the "
                        "adapter; security refusals are not retried"
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "app_sessions.refresh.invalid_token",
                        "app_sessions.refresh.invalid_principal",
                        "app_sessions.refresh.signing_unavailable",
                        "app_sessions.refresh.staff_projection_refused",
                        *owner_command_boundary_error_codes("app_sessions.refresh"),
                    ),
                    mapping_owner="authentication HTTP and web adapters",
                    retryable_codes=(),
                    fail_closed_on=(
                        "unknown refresh credential",
                        "expired session",
                        "previous-token reuse outside the five-second overlap",
                        "previous-token reuse from a different client binding",
                        "ambiguous or unavailable staff principal projection",
                        "missing process-held JWT signing material",
                    ),
                ),
                events=EventContract(
                    event_types=(
                        "authentication_session.rotated",
                        "authentication_session.refresh_refused",
                    ),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility="Version 1 is additive and contains no token material.",
                    replay=(
                        "Events are immutable security evidence; current session "
                        "state is rebuilt from the sessions record, not event replay"
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner="app.services.auth_flow.AuthFlow.refresh",
                    new_owner="app_sessions.refresh",
                    verification=(
                        "Unit overlap/refusal tests, PostgreSQL concurrent rotation "
                        "test and browser coordinator tests"
                    ),
                    cutover_gate=(
                        "Every refresh adapter delegates to the typed owner and no "
                        "adapter rotates or revokes the session row"
                    ),
                    fallback_retirement=(
                        "The legacy AuthFlow refresh mutation is removed; AuthFlow "
                        "remains only a transport adapter"
                    ),
                ),
                steward="identity and platform operations",
                design_refs=(
                    "docs/designs/AUTH_SESSION_REFRESH_CONCURRENCY.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_auth_session_refresh.py",
                    "tests/integration/test_auth_session_refresh_concurrency.py",
                    "tests/js/session_refresh.test.js",
                ),
            ),
        ),
    ),
    entrypoints=(
        "app.web.customer.auth",
        "app.web.customer.routes",
        "app.api.auth",
        "app.web.admin.auth",
    ),
    rule="Routes authenticate and authorize; session services own storage, "
    "refresh, listing, revocation, and impersonation session policy. Generic "
    "administration cannot create or rewrite authentication sessions or MFA "
    "credentials; authentication owns issuance and proof-bound enrollment.",
)
