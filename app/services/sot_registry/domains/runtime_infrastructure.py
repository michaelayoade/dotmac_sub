"""Canonical SOT declarations for the runtime_infrastructure domain."""

from __future__ import annotations

from app.services.automation_contracts import (
    AutomationCatalogItem,
    AutomationCatalogState,
    AutomationDomainCapabilities,
)
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
)
from app.services.sot_registry.model import DomainSOT

DOMAIN = DomainSOT(
    domain="runtime_infrastructure",
    services=(
        SOTService(
            name="runtime.billing_legacy_archive_fence",
            module="app.services.billing.legacy_archive_fence",
            implementation_modules=(
                "app.services.billing.legacy_archive_fence_contracts",
                "app.services.billing.legacy_retained_columns_contracts",
                "app.services.billing.legacy_retained_columns_fence",
                "app.services.billing.legacy_privilege_closure",
                "app.services.billing.legacy_handoff_preparation",
                "app.services.billing.legacy_handoff_preparation_contracts",
            ),
            owns=(
                "private legacy monetary archive fencing",
                "locked legacy handoff preparation",
            ),
            depends_on=("events.owner_outputs", "financial.invoices.opening_export"),
            notes="Private participants only. Locked preparation composes actual census and fence effects inside the same final-owner root, without completing it. Future runtime.billing_ledger_handoff remains unregistered; no production entrypoint, command, migration, startup or activation. Does not prove helper/view/FK/privileged closure, complete economic watermark or global handoff.",
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="locked legacy handoff preparation",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "locked archive catalog",
                            "closed archive policy",
                            "typed census lock inventory",
                            "locked opening population",
                        ),
                        canonical_writer="runtime.billing_legacy_archive_fence",
                    ),
                    ConcernContract(
                        name="private legacy monetary archive fencing",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=("locked archive catalog", "closed archive policy"),
                        canonical_writer="runtime.billing_legacy_archive_fence",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="typed census lock inventory",
                        owner="financial.invoices.opening_export",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="Public billing_opening_lock_inventory derives the explicit V3 collector's relation targets from its existing model inventory; names alone are not held-lock or catalog evidence.",
                    ),
                    AuthorityInput(
                        name="locked opening population",
                        owner="financial.invoices.opening_export",
                        kind=AuthorityKind.OBSERVATION,
                        source="Same-root fresh V3 capture before and after both real fence participants; semantic population, migration heads and cutoff inputs must match, excluding observation clock and snapshot labels. This bounded census is not the complete economic watermark or opening qualification.",
                    ),
                    AuthorityInput(
                        name="locked archive catalog",
                        owner="external:postgresql_catalog",
                        kind=AuthorityKind.EXTERNAL_OBSERVATION,
                        source="Actual owner, namespace, role reachability, exact ordinary relations, grants and fixed function/trigger catalog under the component's complete ACCESS EXCLUSIVE lock set.",
                    ),
                    AuthorityInput(
                        name="closed archive policy",
                        owner="runtime.billing_legacy_archive_fence",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="ArchiveFenceRevision.V1 (44 archives) and RetainedColumnsRevision.V1 (billing_accounts.balance, subscribers.deposit); no caller role names or exemptions.",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.PARTICIPANT,
                    boundary="Only within the valid registered future handoff coordinator's clean same-connection root READ WRITE READ COMMITTED transaction. Whole owner forbids manual SQL savepoints. Flush only; caller alone commits or rolls back.",
                    locking="Components protect 48 distinct archive, retained and relay relations. Locked preparation derives their union with the explicit V3 census inventory, currently 54 relations, and acquires one sorted ACCESS EXCLUSIVE lock statement before capture or fence effects. It rechecks actual public ordinary noninherited relation OIDs, routing, root and role identity. Positive bounded lock and statement timeouts.",
                    idempotency="Exact installed function/trigger identities, body, arguments, events, ALWAYS status and the component-specific ACL policy (no archive DML or retained TRUNCATE for nonowners) permit read-only replay without a second staged event. Collisions/partial installs/drift refuse; no repair or unfreeze.",
                    retries="Caller retries the entire fresh owner transaction after rollback; this participant neither catches catalog failures nor completes transactions.",
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "billing_archive_fence.refused",
                        "billing_retained_columns.refused",
                        "billing_privilege_closure.refused",
                        "billing_locked_handoff_preparation.refused",
                    ),
                    mapping_owner="Future maintenance adapter; raw database failures also abort its whole owner transaction.",
                    fail_closed_on=(
                        "unregistered owner or ambiguous transaction",
                        "role/object/catalog/ACL drift",
                    ),
                ),
                events=EventContract(
                    event_types=(
                        "billing.legacy_archive_fence.staged",
                        "billing.legacy_retained_columns.staged",
                    ),
                    schema_version=1,
                    delivery_owner="events.owner_outputs",
                    compatibility="Typed revision, closed component table count, catalog digest and existing operation identity. Component audit only; contingent on same-root commit, never a seal/receipt/activation event.",
                    replay="Stage via existing transactional owner-output protocol only for a new installation. Exact catalog replay stages no duplicate event; aborted installation and event roll back together.",
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.NATIVE,
                    new_owner="runtime.billing_legacy_archive_fence",
                    verification="Authored real Alembic PostgreSQL component canaries use isolated ephemeral actual owner and app_user logins. Hosted execution pending; no production role proof.",
                ),
                steward="runtime operations",
                design_refs=(
                    "docs/designs/BILLING_LEDGER_HANDOFF.md",
                    "docs/designs/BILLING_LEGACY_FENCE_INVENTORY.md",
                ),
                test_refs=(
                    "tests/integration/test_billing_legacy_archive_fence_postgres.py",
                    "tests/architecture/test_billing_legacy_archive_fence_boundary.py",
                    "tests/architecture/test_billing_legacy_retained_columns_boundary.py",
                    "tests/integration/test_billing_legacy_retained_columns_postgres.py",
                    "tests/integration/test_billing_legacy_privilege_closure_postgres.py",
                    "tests/integration/test_billing_locked_handoff_preparation_postgres.py",
                    "tests/architecture/test_billing_locked_handoff_preparation_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="runtime.billing_runtime_login",
            module="app.commercial_module_prereqs",
            owns=("actual Billing runtime login diagnostics",),
            notes="Diagnostic supplied-session boundary only; no complete Billing preflip, helper/ACL closure, root-transaction proof or activation authority.",
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="actual Billing runtime login diagnostics",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "actual PostgreSQL session",
                            "runtime login policy",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="actual PostgreSQL session",
                        owner="external:postgresql_catalog",
                        kind=AuthorityKind.EXTERNAL_OBSERVATION,
                        source="Supplied psycopg connection startup login, transaction status, PostgreSQL identity/OIDs, role posture, effective membership/USAGE/SET and application-object ownership. No password catalog is read.",
                    ),
                    AuthorityInput(
                        name="runtime login policy",
                        owner="runtime.billing_runtime_login",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="Frozen BillingRuntimeLoginQuery fixes authenticated app_user on PostgreSQL 16+, active READ COMMITTED and no privileged posture, other reachable role or relation/schema/function/database ownership, including catalog and temporary namespaces without exemptions.",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary="Read catalog through the caller's active connection and transaction; never connect, commit, roll back, switch role or create a transaction. Caller owns the root transaction; INTRANS cannot detect arbitrary SAVEPOINTs.",
                    locking="No business locks or catalog mutation. Final admission owner must re-observe under its own locks; returned diagnostics are not proof tokens.",
                    idempotency="Repeated calls observe current READ COMMITTED state without persisting evidence or activating Billing.",
                    retries="No retries or transaction repair. Unsupported server, stale isolation, impersonation, unknown role paths and catalog failures refuse.",
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "billing_runtime_login.inactive_transaction",
                        "billing_runtime_login.unsupported_postgres",
                        "billing_runtime_login.isolation",
                        "billing_runtime_login.identity",
                        "billing_runtime_login.privileged_role",
                        "billing_runtime_login.role_path",
                        "billing_runtime_login.ownership",
                        "billing_runtime_login.catalog_unavailable",
                    ),
                    mapping_owner="Calling maintenance adapter maps BillingRuntimeLoginRefused; no runtime enforcement is installed by this slice.",
                    fail_closed_on=(
                        "unavailable actual session evidence",
                        "privileged identity, owner or membership drift",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.NATIVE,
                    new_owner="runtime.billing_runtime_login",
                    verification="Authored migrated hosted PostgreSQL canaries require genuine app_user authentication and reject role/session impersonation. Runtime execution remains pending.",
                ),
                steward="runtime operations",
                design_refs=("docs/designs/BILLING_LEDGER_HANDOFF.md",),
                test_refs=("tests/integration/test_billing_runtime_login_postgres.py",),
            ),
        ),
        SOTService(
            name="runtime.writer_drain",
            module="app.services.deployment_writer_drain",
            implementation_modules=(
                "app.services.deployment_writer_drain_transport",
                "app.celery_queue_contract",
            ),
            owns=(
                "incumbent writer drain observation",
                "stable empty writer drain policy",
            ),
            notes="Count-only operational evidence. Deployment owns producer shutdown, captured container identity and migration admission; this query never switches ledger authority.",
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="incumbent writer drain observation",
                        role=OwnerRole.RESOLVER,
                        input_names=("broker and worker observation", "drain contract"),
                    ),
                    ConcernContract(
                        name="stable empty writer drain policy",
                        role=OwnerRole.POLICY,
                        input_names=("broker and worker observation", "drain contract"),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="broker and worker observation",
                        owner="external:celery_redis",
                        kind=AuthorityKind.EXTERNAL_OBSERVATION,
                        source="Pinned Celery/Kombu/Redis count observations for every expected node's active, reserved and scheduled tasks, every declared priority queue, and both unacked hash and index; no customer task payload crosses the adapter.",
                    ),
                    AuthorityInput(
                        name="drain contract",
                        owner="runtime.writer_drain",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="Frozen DrainRequest fixes exact queue inventory, node coverage, mode, deadline and two-zero stability interval. Broker-only mode is final evidence after deployment separately proves all writers stopped.",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.NOT_APPLICABLE,
                    boundary="No database Session or business writes. Redis observation reads queue/unacked counts; Celery inspection sends necessary transient control requests and receives replies only.",
                    locking="No task or financial locks, purges, revokes, restores or visibility changes; deployment must stop producers independently.",
                    idempotency="Repeated observations return current counts; no activation, admission, receipt or ledger state is persisted.",
                    retries="Bounded observations until two complete zero observations separated by the stability interval. Missing, malformed, duplicate, unsupported or timed-out evidence refuses; no zero inference from arbitrary transport errors.",
                ),
                errors=ErrorContract(
                    domain_codes=(),
                    mapping_owner="observe_writer_drain CLI emits closed DrainRefusal codes and safe JSON/exit status; unexpected exceptions cannot authorize migration.",
                    fail_closed_on=(
                        "missing or duplicate worker coverage",
                        "unknown broker configuration or runtime dependency",
                        "malformed observation or exhausted deadline",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.NATIVE,
                    new_owner="runtime.writer_drain",
                ),
                steward="runtime operations",
                design_refs=("docs/designs/DEPLOYMENT_WRITER_DRAIN_EVIDENCE.md",),
                test_refs=(
                    "tests/test_deployment_writer_drain.py",
                    "tests/integration/writer_drain_acceptance.py",
                ),
            ),
        ),
        SOTService(
            name="runtime.payments_intent_schema_floor",
            module="app.services.payments_intent_schema_floor",
            owns=(
                "Payments invoice intent installed schema observation",
                "normal Payments deployment support classification",
                "legacy image recovery admission after Payments intent schema installation",
            ),
            notes="Operational catalog proof only; complete predecessor and complete installed shapes admit normal forward deployment, while legacy recovery requires predecessor only. No intent, settlement or ledger authority.",
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="Payments invoice intent installed schema observation",
                        role=OwnerRole.RESOLVER,
                        input_names=("actual runtime catalog",),
                    ),
                    ConcernContract(
                        name="normal Payments deployment support classification",
                        role=OwnerRole.POLICY,
                        input_names=(
                            "actual runtime catalog",
                            "installed schema observation",
                        ),
                    ),
                    ConcernContract(
                        name="legacy image recovery admission after Payments intent schema installation",
                        role=OwnerRole.POLICY,
                        input_names=(
                            "installed schema observation",
                            "legacy recovery policy",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="actual runtime catalog",
                        owner="external:postgresql",
                        kind=AuthorityKind.EXTERNAL_OBSERVATION,
                        source="Caller connection session/current login, non-superuser/non-bypass role flags, and exact pg_catalog column, constraint, trigger and function shape; no tenant-filtered business rows.",
                    ),
                    AuthorityInput(
                        name="installed schema observation",
                        owner="runtime.payments_intent_schema_floor",
                        kind=AuthorityKind.OBSERVATION,
                        source="Frozen PaymentsIntentSchemaObservation returned by read_payments_intent_schema_floor on the caller Session; never cached or inferred from intent counts.",
                    ),
                    AuthorityInput(
                        name="Payments schema support classification",
                        owner="runtime.payments_intent_schema_floor",
                        kind=AuthorityKind.OBSERVATION,
                        source="Closed classification of catalog facts: predecessor requires complete base and zero installation objects; installed requires complete base and exact installed object counts; all other shapes are unknown.",
                    ),
                    AuthorityInput(
                        name="legacy recovery policy",
                        owner="runtime.payments_intent_schema_floor",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="Only a complete predecessor with zero correlation-installation evidence admits a legacy image; installed, partial, malformed or unknown evidence refuses recovery.",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary="Caller Session; SELECT catalog/identity facts only, with no session creation, writes or transaction completion.",
                    locking="No business locks; migration and deployment owners must establish quiescence before installing the permanent floor.",
                    idempotency="The same catalog and runtime identity produce the same frozen observation and admission decision; no activation flag is stored.",
                    retries="No hidden retries or repair; unknown evidence fails closed and a caller may retry a complete read.",
                ),
                errors=ErrorContract(
                    domain_codes=("payments_intent_schema_floor.unknown",),
                    mapping_owner="Calling deployment adapter; production integration remains held until its guard wiring is reviewed.",
                    fail_closed_on=(
                        "unproved runtime login or catalog",
                        "installed or partial correlation schema",
                        "malformed observation",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.NATIVE,
                    new_owner="runtime.payments_intent_schema_floor",
                ),
                steward="runtime operations",
                design_refs=("docs/designs/PAYMENTS_INVOICE_INTENT_SCHEMA_FLOOR.md",),
                test_refs=(
                    "tests/test_payments_intent_schema_floor.py",
                    "tests/integration/test_payments_intent_correlation_pg.py",
                ),
            ),
        ),
        SOTService(
            name="runtime.bandwidth_poller_health",
            module="app.services.poller_health",
            implementation_modules=(
                "app.poller.mikrotik_poller",
                "app.services.platform_health_observations",
            ),
            owns=(
                "bounded bandwidth poller loop liveness query",
                "bounded Platform Health poller observation projection",
            ),
            notes="Local component observation and optional Platform Health document serialization only; no transport, persistence, deployment authorization or domain writes.",
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="bounded bandwidth poller loop liveness query",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "component runtime observation",
                            "bounded probe freshness policy",
                        ),
                    ),
                    ConcernContract(
                        name="bounded Platform Health poller observation projection",
                        role=OwnerRole.TRANSPORT,
                        input_names=(
                            "component runtime observation",
                            "authenticated Platform Health source identity",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="component runtime observation",
                        owner="runtime.bandwidth_poller_health",
                        kind=AuthorityKind.OBSERVATION,
                        source="Incumbent POLLER_HEALTH_KEY JSON published by the polling loop after successful or caught-failed cycles; Redis is transport, not collection-success authority.",
                    ),
                    AuthorityInput(
                        name="bounded probe freshness policy",
                        owner="runtime.bandwidth_poller_health",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="Typed query budgets and fail-closed malformed/missing/future/stale/unavailable handling described in PROCESS_HEALTH_PROBES.md.",
                    ),
                    AuthorityInput(
                        name="authenticated Platform Health source identity",
                        owner="external:dotmac_integrator",
                        kind=AuthorityKind.EXTERNAL_OBSERVATION,
                        source="Caller-supplied source_ref that the future authenticated Integrator transport must bind to the deployment identity; the local CLI does not authenticate it or persist/transport the serialized observation.",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.NOT_APPLICABLE,
                    boundary="No database transaction or business side effect; local observation and bounded transport only.",
                    locking="Syslog health endpoint uses a private same-UID directory and exclusive lifetime lock; poller reads incumbent Redis projection.",
                    idempotency="Read queries are side-effect-free for business state; each syslog request has a fresh challenge.",
                    retries="No hidden retries; operators retry unavailable observations within declared budgets.",
                ),
                errors=ErrorContract(
                    domain_codes=(),
                    mapping_owner="Component health CLI maps typed unavailable/nonhealthy outcomes to exit 1 and invalid arguments to exit 2.",
                    fail_closed_on=(
                        "missing or malformed evidence",
                        "stale or future evidence",
                        "transport unavailable",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.NATIVE,
                    new_owner="runtime.bandwidth_poller_health",
                ),
                steward="runtime operations",
                design_refs=("docs/runbooks/PROCESS_HEALTH_PROBES.md",),
                test_refs=(
                    "tests/test_process_health_probes.py",
                    "tests/architecture/test_process_health_probe_ownership.py",
                ),
            ),
        ),
        SOTService(
            name="runtime.syslog_receiver_health",
            module="app.syslog.listener",
            implementation_modules=(
                "app.syslog.health",
                "app.services.platform_health_observations",
            ),
            owns=(
                "listener-owned UDP receiver readiness query",
                "bounded Platform Health syslog observation projection",
            ),
            notes="Local component observation and optional Platform Health document serialization only; no transport, persistence, deployment authorization or domain writes.",
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="listener-owned UDP receiver readiness query",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "component runtime observation",
                            "bounded probe freshness policy",
                        ),
                    ),
                    ConcernContract(
                        name="bounded Platform Health syslog observation projection",
                        role=OwnerRole.TRANSPORT,
                        input_names=(
                            "component runtime observation",
                            "authenticated Platform Health source identity",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="component runtime observation",
                        owner="runtime.syslog_receiver_health",
                        kind=AuthorityKind.OBSERVATION,
                        source="Actual UDP bind/nonclosing transport and monotonic progress of the same listener event loop; random generation and request challenge prevent cached prior-process responses.",
                    ),
                    AuthorityInput(
                        name="bounded probe freshness policy",
                        owner="runtime.syslog_receiver_health",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="Typed query budgets and fail-closed malformed/missing/future/stale/unavailable handling described in PROCESS_HEALTH_PROBES.md.",
                    ),
                    AuthorityInput(
                        name="authenticated Platform Health source identity",
                        owner="external:dotmac_integrator",
                        kind=AuthorityKind.EXTERNAL_OBSERVATION,
                        source="Caller-supplied source_ref that the future authenticated Integrator transport must bind to the deployment identity; the local CLI does not authenticate it or persist/transport the serialized observation.",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.NOT_APPLICABLE,
                    boundary="No database transaction or business side effect; local observation and bounded transport only.",
                    locking="Syslog health endpoint uses a private same-UID directory and exclusive lifetime lock; poller reads incumbent Redis projection.",
                    idempotency="Read queries are side-effect-free for business state; each syslog request has a fresh challenge.",
                    retries="No hidden retries; operators retry unavailable observations within declared budgets.",
                ),
                errors=ErrorContract(
                    domain_codes=(),
                    mapping_owner="Component health CLI maps typed unavailable/nonhealthy outcomes to exit 1 and invalid arguments to exit 2.",
                    fail_closed_on=(
                        "missing or malformed evidence",
                        "stale or future evidence",
                        "transport unavailable",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.NATIVE,
                    new_owner="runtime.syslog_receiver_health",
                ),
                steward="runtime operations",
                design_refs=("docs/runbooks/PROCESS_HEALTH_PROBES.md",),
                test_refs=(
                    "tests/test_process_health_probes.py",
                    "tests/architecture/test_process_health_probe_ownership.py",
                ),
            ),
        ),
        SOTService(
            name="runtime.realtime_projection",
            module="app.services.realtime_platform",
            owns=(
                "versioned real-time event envelope",
                "Redis topic naming and best-effort publication",
                "shared WebSocket and SSE delivery semantics",
                "reconnect and no-replay refresh contract",
            ),
            depends_on=("auth.permission_gate",),
            notes=(
                "Real-time events are non-durable projections after an "
                "owning domain commits state. Redis pub/sub is at-most-once; "
                "clients refetch canonical read models after reconnect or "
                "reset. Client-selected topics are authorized by "
                "app.services.realtime_subscriptions; workqueue topics are "
                "derived by its scope owner. WebSocket and SSE modules are "
                "transport adapters only."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="versioned real-time event envelope",
                        role=OwnerRole.POLICY,
                        input_names=("real-time schema contract",),
                    ),
                    ConcernContract(
                        name="Redis topic naming and best-effort publication",
                        role=OwnerRole.TRANSPORT,
                        input_names=(
                            "real-time schema contract",
                            "committed projection request",
                            "Redis delivery availability observation",
                        ),
                    ),
                    ConcernContract(
                        name="shared WebSocket and SSE delivery semantics",
                        role=OwnerRole.TRANSPORT,
                        input_names=(
                            "real-time schema contract",
                            "authorized subscription topics",
                            "Redis delivery availability observation",
                        ),
                    ),
                    ConcernContract(
                        name="reconnect and no-replay refresh contract",
                        role=OwnerRole.POLICY,
                        input_names=(
                            "real-time schema contract",
                            "Redis delivery availability observation",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="real-time schema contract",
                        owner="runtime.realtime_projection",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "RealtimeEvent schema version, event/topic validation, "
                            "and refresh_required semantics"
                        ),
                    ),
                    AuthorityInput(
                        name="committed projection request",
                        owner="runtime.realtime_projection",
                        kind=AuthorityKind.OBSERVATION,
                        source=(
                            "best-effort invalidation request accepted only after the "
                            "calling domain owner commits durable state"
                        ),
                    ),
                    AuthorityInput(
                        name="authorized subscription topics",
                        owner="auth.permission_gate",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "object-level conversation or operation topic decision "
                            "from app.services.realtime_subscriptions"
                        ),
                    ),
                    AuthorityInput(
                        name="Redis delivery availability observation",
                        owner="external:redis",
                        kind=AuthorityKind.EXTERNAL_OBSERVATION,
                        source=(
                            "Redis publish and pub/sub connection outcome; never a "
                            "durable delivery receipt"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.NOT_APPLICABLE,
                    boundary=(
                        "The platform never opens or completes a domain database "
                        "transaction; callers publish only after durable state commits."
                    ),
                    locking=(
                        "Immutable envelopes and topic strings require no database "
                        "lock; Redis pub/sub provides no durable ordering lock."
                    ),
                    idempotency=(
                        "Event identifiers distinguish repeated invalidations, while "
                        "clients converge by refetching the canonical read model."
                    ),
                    retries=(
                        "Publication failure returns false and is not retried inside "
                        "the domain transaction; reconnecting clients refetch state."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(),
                    mapping_owner="WebSocket and SSE transport adapters",
                    fail_closed_on=(
                        "invalid event envelope",
                        "unauthorized client-selected topic",
                        "channel and envelope topic mismatch",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "app.websocket.realtime and transport-specific Redis "
                        "publication helpers"
                    ),
                    new_owner="runtime.realtime_projection",
                    verification=(
                        "Shared envelope, broker, subscription authorization, and "
                        "transport-boundary tests."
                    ),
                    cutover_gate=(
                        "All application publishers use realtime_platform and no "
                        "domain service imports app.websocket transports."
                    ),
                    fallback_retirement=(
                        "The legacy inbox_ws broker prefix and app.websocket.realtime "
                        "publication module are removed."
                    ),
                ),
                steward="platform runtime",
                design_refs=(
                    "docs/REALTIME_PLATFORM.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
                ),
                test_refs=(
                    "tests/test_realtime_platform.py",
                    "tests/test_realtime_subscriptions.py",
                    "tests/architecture/test_realtime_platform_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="runtime.db_sessions",
            module="app.services.db_session_adapter",
            owns=(
                "background DB session lifecycle",
                "read/write task session boundaries",
                "Postgres advisory lock ownership",
            ),
        ),
        SOTService(
            name="runtime.task_idempotency",
            module="app.services.task_idempotency",
            owns=("task idempotency keys", "duplicate task suppression"),
            depends_on=("runtime.db_sessions",),
        ),
        SOTService(
            name="runtime.task_heartbeat",
            module="app.services.task_heartbeat",
            owns=("task success heartbeat", "single-flight skip streaks"),
            depends_on=("observability.recording",),
        ),
        SOTService(
            name="runtime.infrastructure_polling",
            module="app.services.infrastructure_polling",
            owns=(
                "shared native reachability poll observations",
                "generic network-device pollable predicate",
                "poll heartbeat result counters",
            ),
            depends_on=("runtime.db_sessions",),
            notes=(
                "Polling and topology warming use reserved monitoring-queue "
                "capacity. Bulk ingestion, including independently bounded "
                "per-OLT MAC harvests, does not share that worker; queue "
                "placement does not change observation ownership."
            ),
        ),
        SOTService(
            name="runtime.web_worker_readiness",
            module="app.services.web_worker_readiness",
            owns=("web-worker route-table readiness",),
            depends_on=("observability.metrics",),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="web-worker route-table readiness",
                        role=OwnerRole.POLICY,
                        input_names=(
                            "mounted supported route table",
                            "essential startup preflight",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="mounted supported route table",
                        owner="runtime.web_worker_readiness",
                        kind=AuthorityKind.OBSERVATION,
                        source="FastAPI routes mounted before lifespan yields, including subscriber sync",
                    ),
                    AuthorityInput(
                        name="essential startup preflight",
                        owner="runtime.web_worker_readiness",
                        kind=AuthorityKind.OBSERVATION,
                        source="required schema, secret, encryption, and OIDC checks",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.NOT_APPLICABLE,
                    boundary="Startup readiness only changes process-local application state and metrics.",
                    locking="No application or database lock is acquired.",
                    idempotency="Repeated lifespan cycles recheck the complete route table before readiness.",
                    retries="Optional work runs after readiness and never retries route registration.",
                ),
                errors=ErrorContract(
                    domain_codes=(),
                    mapping_owner="health and server readiness adapters",
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.NATIVE,
                    new_owner="runtime.web_worker_readiness",
                    verification="Cold-start route and readiness regression tests.",
                ),
                steward="platform runtime",
                design_refs=("docs/runbooks/WEB_WORKER_READINESS.md",),
                test_refs=("tests/architecture/test_startup_router_readiness.py",),
            ),
        ),
        SOTService(
            name="runtime.infrastructure_health",
            module="app.services.infrastructure_health",
            owns=(
                "dependency health checks",
                "Postgres/Redis/VM/Celery infrastructure status",
                "scheduled bounded dependency health snapshot",
            ),
            depends_on=("runtime.db_sessions", "control.settings_spec"),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="dependency health checks",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "dependency probe observations",
                            "health probe configuration",
                        ),
                    ),
                    ConcernContract(
                        name="Postgres/Redis/VM/Celery infrastructure status",
                        role=OwnerRole.RESOLVER,
                        input_names=("dependency probe observations",),
                    ),
                    ConcernContract(
                        name="scheduled bounded dependency health snapshot",
                        role=OwnerRole.TRANSPORT,
                        input_names=(
                            "resolved dependency health status",
                            "scheduled probe cadence",
                            "Redis projection availability",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="dependency probe observations",
                        owner="external:runtime_dependencies",
                        kind=AuthorityKind.EXTERNAL_OBSERVATION,
                        source=(
                            "Bounded PostgreSQL, Redis, VictoriaMetrics, GenieACS, "
                            "RADIUS, MinIO, Celery, and Nominatim probe responses."
                        ),
                    ),
                    AuthorityInput(
                        name="health probe configuration",
                        owner="runtime.infrastructure_health",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "Checked-in probe set, timeouts, queue expectations, "
                            "and validated disabled-check configuration."
                        ),
                    ),
                    AuthorityInput(
                        name="resolved dependency health status",
                        owner="runtime.infrastructure_health",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "Typed ServiceStatus results produced by the complete "
                            "bounded scheduled probe run."
                        ),
                    ),
                    AuthorityInput(
                        name="scheduled probe cadence",
                        owner="control.settings_spec",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "stale_infrastructure_check_enabled and the validated "
                            "stale_infrastructure_check_interval_seconds setting."
                        ),
                    ),
                    AuthorityInput(
                        name="Redis projection availability",
                        owner="external:redis",
                        kind=AuthorityKind.EXTERNAL_OBSERVATION,
                        source=(
                            "Shared application-cache write/read outcome; Redis is a "
                            "projection transport and never health authority."
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary=(
                        "The task adapter owns a bounded read session for the "
                        "PostgreSQL probe and closes it before publishing the shared "
                        "cache projection; dashboard requests only read the snapshot."
                    ),
                    locking=(
                        "No business rows are mutated or locked; the latest cache key "
                        "is atomically replaced by one complete scheduled result."
                    ),
                    idempotency=(
                        "Repeating a probe run replaces the latest projection with the "
                        "same typed service cohort and a new observed timestamp."
                    ),
                    retries=(
                        "The scheduler retries on its next bounded cadence; individual "
                        "dependency failures become status facts and never trigger an "
                        "unbounded in-task retry."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(),
                    mapping_owner=(
                        "monitoring task and admin dashboard transport adapters"
                    ),
                    fail_closed_on=(
                        "missing snapshot",
                        "malformed snapshot",
                        "unsupported snapshot schema",
                    ),
                ),
                projections=(
                    ProjectionContract(
                        name="scheduled bounded dependency health snapshot",
                        input_names=(
                            "resolved dependency health status",
                            "scheduled probe cadence",
                            "Redis projection availability",
                        ),
                        writer="runtime.infrastructure_health",
                        freshness=(
                            "Observed at completion of the scheduled probe; fresh for "
                            "ten minutes against the default five-minute cadence."
                        ),
                        stale_behavior=(
                            "Dashboard labels last-known values stale after ten minutes; "
                            "missing or malformed data renders unavailable and never "
                            "runs probes in the request."
                        ),
                        drift_signal=(
                            "Snapshot age, cache publication failure, malformed schema, "
                            "or a missing scheduled-task heartbeat."
                        ),
                        rebuild_operation=(
                            "Run check_stale_infrastructure to execute the bounded probe "
                            "cohort and atomically republish the latest snapshot."
                        ),
                        repair_owner="runtime.infrastructure_health",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "dashboard request-time check_all_services calls and each "
                        "web worker's short-lived process cache"
                    ),
                    new_owner="runtime.infrastructure_health",
                    verification=(
                        "Snapshot contract, task publication, dashboard no-probe, "
                        "freshness UI, and architecture manifest tests."
                    ),
                    cutover_gate=(
                        "Dashboard infrastructure routes load only the scheduled "
                        "shared snapshot."
                    ),
                    fallback_retirement=(
                        "Request-time dependency probes and the per-process dashboard "
                        "infrastructure cache are removed."
                    ),
                ),
                steward="platform runtime",
                design_refs=(
                    "docs/designs/OPERATIONS_MEASUREMENT_STRATEGY.md",
                    "docs/UI_INFORMATION_AND_ACTION_STANDARD.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_infrastructure_health.py",
                    "tests/test_database_pressure_metrics.py",
                    "tests/test_web_admin_dashboard_infrastructure.py",
                    "tests/architecture/test_dashboard_infrastructure_snapshot_boundary.py",
                ),
            ),
        ),
    ),
    entrypoints=(
        "app.tasks.*",
        "app.main",
        "app.websocket.*",
        "app.api.workqueue",
        "app.services.scheduler_config",
        "app.web.admin.system",
    ),
    rule="Real-time delivery projects already-committed state and never "
    "becomes a decision owner or durable event log. Infrastructure "
    "tasks use shared DB/session/lock and heartbeat helpers; polling "
    "writes observations while network/device resolvers interpret state.",
    automation=AutomationDomainCapabilities(
        catalog_items=(
            AutomationCatalogItem(
                key="network.infrastructure_polling",
                label="Infrastructure polling",
                group="Network and monitoring",
                state=AutomationCatalogState.unavailable,
                explanation="Health snapshots are collected by the existing infrastructure scheduler; Center schedule controls are not available.",
            ),
        ),
    ),
)
