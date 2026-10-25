"""Canonical SOT declarations for the subscriber_sessions domain."""

from __future__ import annotations

from app.services.sot_manifest import (
    AuthorityInput,
    AuthorityKind,
    AuthorityMigrationState,
    ConcernContract,
    ErrorContract,
    MigrationContract,
    OwnerRole,
    ServiceContract,
    SOTService,
    TransactionContract,
    TransactionMode,
)
from app.services.sot_registry.model import DomainSOT

DOMAIN = DomainSOT(
    domain="subscriber_sessions",
    setting_domains=("usage",),
    services=(
        SOTService(
            name="sessions.radius_reconciliation",
            module="app.services.radius_session_reconcile",
            owns=(
                "external radacct open-session discovery",
                "RADIUS active-session mirror writes",
                "live-session mirror pruning",
            ),
            depends_on=("network.identity",),
        ),
        SOTService(
            name="sessions.radius_accounting_health",
            module="app.services.radius_accounting_health",
            owns=(
                "RADIUS accounting source freshness policy",
                "accounting source health classification",
            ),
            depends_on=("control.domain_settings", "runtime.db_sessions"),
        ),
        SOTService(
            name="sessions.radius_resolution",
            module="app.services.network.radius_sessions",
            owns=(
                "customer online-now resolution",
                "primary NAS session resolution",
                "historical subscription monitoring coverage",
            ),
            depends_on=("sessions.radius_reconciliation", "network.identity"),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="customer online-now resolution",
                        role=OwnerRole.RESOLVER,
                        input_names=("active RADIUS session projection",),
                    ),
                    ConcernContract(
                        name="primary NAS session resolution",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "active RADIUS session projection",
                            "network identity registry",
                        ),
                    ),
                    ConcernContract(
                        name="historical subscription monitoring coverage",
                        role=OwnerRole.RESOLVER,
                        input_names=("subscription-bound accounting observations",),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="active RADIUS session projection",
                        owner="sessions.radius_reconciliation",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source="radius_active_sessions",
                    ),
                    AuthorityInput(
                        name="subscription-bound accounting observations",
                        owner="sessions.radius_reconciliation",
                        kind=AuthorityKind.OBSERVATION,
                        source=(
                            "radius_accounting_sessions exact subscription binding, "
                            "session_start, session_end, and last_update_at"
                        ),
                    ),
                    AuthorityInput(
                        name="network identity registry",
                        owner="network.identity",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="NetworkDevice and NAS identity mappings",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary=(
                        "Caller creates and closes the session; resolver "
                        "performs no writes or transaction completion."
                    ),
                    locking=(
                        "No row lock; the result reflects database visibility "
                        "at query time."
                    ),
                    idempotency=(
                        "The same subscriber, limit, and visible input snapshot "
                        "produce the same ordered resolution."
                    ),
                    retries=(
                        "Adapters may retry transient read failures; the resolver "
                        "has no side effects."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(),
                    mapping_owner="web, API, task, and service adapters",
                    fail_closed_on=("invalid subscriber identifier",),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.NATIVE,
                    new_owner="sessions.radius_resolution",
                ),
                steward="network operations",
                design_refs=(
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/DASHBOARD_OVERVIEW_PAGE_CONTRACT.md",
                ),
                test_refs=(
                    "tests/test_network_sot_services.py",
                    "tests/test_customer_service_level.py",
                    "tests/test_sot_relationships.py",
                ),
            ),
        ),
        SOTService(
            name="sessions.enforcement",
            module="app.services.enforcement",
            owns=(
                "NAS-evidenced accounting-session closure",
                "single-flight access-control recovery execution",
                "subscription cancel/suspend/restore/FUP-lift access-cleanup "
                "coordination",
            ),
            depends_on=(
                "financial.access_resolution",
                "sessions.radius_resolution",
            ),
            notes=(
                "Migration debt (legacy manifest baseline). The closure and "
                "recovery concerns are implemented in the Celery task "
                "app/tasks/radius.py: NAS-evidenced closure writes "
                "FreeRADIUS radacct on its own connection on an RFC 5176 "
                "session-not-found reply, and the single-flight recovery loop "
                "runs under an advisory lock. Owed: extract recovery into a "
                "coordinator-managed owner command; and migrate radacct closure "
                "to the decided single path (Michael, 2026-09-26): this service "
                "is Sub's only synthetic closure writer and sets acctstoptime "
                "only on positive NAS evidence of session absence; FreeRADIUS "
                "remains the source of real Stop and Accounting-On/Off; the "
                "age-based reaper in app/services/radius_reconciliation.py "
                "becomes identify-and-report (stale rows are unknown until "
                "verified) and loses its direct update only through a measured "
                "cutover. The access-cleanup coordination concern is the "
                "cancel/suspend/restore and FUP-lift orchestration in "
                "app.services.enforcement (cleanup_subscription_on_cancel, "
                "cleanup_subscription_on_suspend, "
                "restore_subscription_connectivity, lift_fup_enforcement), which "
                "has no owner command; its only unowned writes are "
                "RadiusUser.is_active. Its other writes are PARALLEL WRITERS of "
                "state other services own, recorded as debt against them, not "
                "owned here: AccessCredential.radius_profile_id via "
                "apply_radius_profile_to_subscription (owner "
                "access.credential_binding; raises "
                "access.session_enforcement.credentials_ambiguous / "
                "no_subscription_credentials); AccessCredential.is_active (owner "
                "access.pppoe_credentials); Subscription.ipv4_address / "
                "ipv6_address cleared on cancel (owner "
                "network.ip_assignment_lifecycle, tracked in "
                "tests/architecture/served_ipv4_writer_baseline.txt and "
                "served_ipv6_writer_baseline.txt); and "
                "project_credentials_to_radius, a request to "
                "access.radius_projection rather than a projection of its own. "
                "FUP state itself is cleared through access.fup_runtime_state. "
                "Each needs its owner command or cutover. "
                "CoA/disconnect execution "
                "and session refresh are contracted under "
                "access.session_enforcement."
            ),
        ),
    ),
    entrypoints=(
        "app.tasks.radius",
        "app.tasks.enforcement",
        "app.services.events.handlers.enforcement",
        "app.web.admin.network_radius",
        "app.services.web_customer_details",
    ),
    rule="RADIUS accounting imports write session facts; session resolvers "
    "answer online state; enforcement applies disconnect/CoA outcomes.",
)
