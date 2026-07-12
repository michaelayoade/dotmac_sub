"""System-wide single-source-of-truth relationship registry.

This registry names the service boundaries that should own domain decisions.
It is intentionally declarative: routes, APIs, Celery tasks, and event handlers
can use it as an architectural map while each domain is migrated incrementally.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SOTService:
    name: str
    module: str
    owns: tuple[str, ...]
    depends_on: tuple[str, ...] = ()
    notes: str | None = None


@dataclass(frozen=True)
class DomainSOT:
    domain: str
    services: tuple[SOTService, ...]
    entrypoints: tuple[str, ...]
    rule: str


DOMAIN_SOT_RELATIONSHIPS: tuple[DomainSOT, ...] = (
    DomainSOT(
        domain="customer_context",
        services=(
            SOTService(
                name="customer.identity_scope",
                module="app.services.customer_context",
                owns=(
                    "portal/customer principal resolution",
                    "allowed account/subscriber scope",
                    "customer ownership checks",
                ),
            ),
            SOTService(
                name="customer.network_context",
                module="app.services.customer_network_context",
                owns=(
                    "customer network footprint",
                    "ONT/CPE/IP/session summary",
                ),
                depends_on=("customer.identity_scope", "network.access_path"),
            ),
            SOTService(
                name="customer.financial_position",
                module="app.services.customer_financial_position",
                owns=(
                    "customer balance summaries",
                    "customer-visible financial position",
                ),
                depends_on=("financial.ledger",),
            ),
        ),
        entrypoints=(
            "app.web.customer",
            "app.api.me",
            "app.services.customer_portal_*",
            "app.services.crm_api",
        ),
        rule=(
            "Customer-facing surfaces resolve scope once through customer context "
            "and compose network/financial summaries through services."
        ),
    ),
    DomainSOT(
        domain="financial_access",
        services=(
            SOTService(
                name="financial.ledger",
                module="app.services.billing.ledger",
                owns=(
                    "posted money movement",
                    "ledger-derived balances",
                    "financial transaction history",
                ),
            ),
            SOTService(
                name="financial.billing_accounts",
                module="app.services.billing.billing_accounts",
                owns=(
                    "billing account read/write operations",
                    "account-level balance materialization",
                ),
                depends_on=("financial.ledger",),
            ),
            SOTService(
                name="financial.billing_profile",
                module="app.services.billing_profile",
                owns=(
                    "prepaid/postpaid profile resolution",
                    "billing-mode transition policy",
                ),
            ),
            SOTService(
                name="financial.access_resolution",
                module="app.services.access_resolution",
                owns=(
                    "billable service classification",
                    "RADIUS access decision",
                    "postpaid/prepaid enforcement cohorts",
                ),
                depends_on=("financial.billing_profile",),
            ),
            SOTService(
                name="financial.dunning",
                module="app.services.collections._core",
                owns=(
                    "postpaid collection lifecycle",
                    "dunning action execution",
                ),
                depends_on=("financial.access_resolution", "financial.ledger"),
            ),
            SOTService(
                name="financial.billing_scheduled",
                module="app.services.billing.scheduled",
                owns=(
                    "scheduled invoice and overdue execution",
                    "billing health and audit execution",
                    "scheduled billing notification execution",
                ),
                depends_on=(
                    "financial.ledger",
                    "financial.access_resolution",
                ),
            ),
            SOTService(
                name="financial.collections_scheduled",
                module="app.services.collections.scheduled",
                owns=(
                    "scheduled billing enforcement execution",
                    "scheduled prepaid balance enforcement execution",
                    "scheduled bundle-state reconciliation execution",
                ),
                depends_on=(
                    "financial.dunning",
                    "financial.access_resolution",
                ),
            ),
            SOTService(
                name="financial.payment_reconciliation",
                module="app.services.payment_reconciliation",
                owns=(
                    "stranded top-up reconciliation",
                    "scheduled top-up reconciliation execution",
                ),
                depends_on=("financial.ledger",),
            ),
        ),
        entrypoints=(
            "app.services.billing_automation",
            "app.services.collections.*",
            "app.web.admin.billing_*",
            "app.api.billing",
            "app.tasks.billing",
            "app.tasks.collections",
            "app.tasks.enforcement",
            "app.tasks.payment_reconciliation",
        ),
        rule=(
            "No caller infers access or balances from draft invoices, imported "
            "legacy fields, or ad hoc sums when ledger/access resolvers exist."
        ),
    ),
    DomainSOT(
        domain="network",
        services=(
            SOTService(
                name="network.identity",
                module="app.services.network.identity",
                owns=("cross-model network links", "device/entity identity"),
            ),
            SOTService(
                name="network.monitoring_inventory",
                module="app.services.network_monitoring",
                owns=(
                    "monitoring inventory mutations",
                    "monitoring metric records",
                    "alert rule and alert state mutations",
                ),
                depends_on=("network.identity",),
            ),
            SOTService(
                name="network.access_path",
                module="app.services.network.access_path",
                owns=("subscription access path", "last-mile path summary"),
                depends_on=("network.identity",),
            ),
            SOTService(
                name="network.radius_sessions",
                module="app.services.network.radius_sessions",
                owns=("online-now session state", "primary NAS session"),
                depends_on=("network.identity",),
            ),
            SOTService(
                name="network.device_state",
                module="app.services.network.device_state",
                owns=("live infrastructure state", "pollability interpretation"),
                depends_on=("network.identity",),
            ),
            SOTService(
                name="network.outage_impact",
                module="app.services.network.outage_impact",
                owns=("affected-customer impact", "outage scope impact"),
                depends_on=("network.access_path", "network.device_state"),
            ),
            SOTService(
                name="network.device_groups",
                module="app.services.network.device_groups",
                owns=(
                    "network device group mutations",
                    "device group membership",
                    "device group bulk action queueing",
                ),
                depends_on=("network.identity",),
            ),
            SOTService(
                name="network.events",
                module="app.services.network.events",
                owns=("network event decisions",),
                depends_on=(
                    "network.device_state",
                    "network.outage_impact",
                    "network.radius_sessions",
                    "network.device_groups",
                ),
            ),
        ),
        entrypoints=(
            "app.services.topology.*",
            "app.services.infrastructure_*",
            "app.tasks.network_*",
            "app.web.admin.network_*",
        ),
        rule=(
            "Pollers write observations; network resolvers decide state; event "
            "services decide consequences."
        ),
    ),
    DomainSOT(
        domain="subscriber_sessions",
        services=(
            SOTService(
                name="sessions.radius_live_view",
                module="app.services.radius_active_sessions",
                owns=(
                    "RADIUS active-session mirror",
                    "accounting start/interim/stop session rows",
                ),
                depends_on=("network.identity",),
            ),
            SOTService(
                name="sessions.radius_reconciliation",
                module="app.services.radius_session_reconcile",
                owns=(
                    "external radacct open-session discovery",
                    "live-session mirror pruning",
                ),
                depends_on=("sessions.radius_live_view",),
            ),
            SOTService(
                name="sessions.radius_resolution",
                module="app.services.network.radius_sessions",
                owns=("customer online-now resolution", "primary NAS session"),
                depends_on=("sessions.radius_live_view", "network.identity"),
            ),
            SOTService(
                name="sessions.enforcement",
                module="app.services.enforcement",
                owns=(
                    "CoA/disconnect execution",
                    "session refresh after access-state changes",
                ),
                depends_on=(
                    "financial.access_resolution",
                    "sessions.radius_resolution",
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
        rule=(
            "RADIUS accounting imports write session facts; session resolvers "
            "answer online state; enforcement applies disconnect/CoA outcomes."
        ),
    ),
    DomainSOT(
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
                name="app_sessions.customer_portal",
                module="app.services.customer_portal_session",
                owns=(
                    "customer portal session creation",
                    "customer portal session refresh/revoke",
                    "impersonation/read-only portal session policy",
                ),
                depends_on=("app_sessions.store", "customer.identity_scope"),
            ),
            SOTService(
                name="app_sessions.auth",
                module="app.services.session_manager",
                owns=(
                    "database auth-session listing",
                    "database auth-session revocation",
                ),
                depends_on=("app_sessions.store",),
            ),
        ),
        entrypoints=(
            "app.web.customer.auth",
            "app.web.customer.routes",
            "app.api.auth",
            "app.web.admin.auth",
        ),
        rule=(
            "Routes authenticate and authorize; session services own storage, "
            "refresh, listing, revocation, and impersonation session policy."
        ),
    ),
    DomainSOT(
        domain="secrets_credentials",
        services=(
            SOTService(
                name="secrets.reference_store",
                module="app.services.secrets",
                owns=(
                    "secret reference parsing and resolution",
                    "OpenBao read/write boundary",
                    "bounded secret cache lifecycle",
                ),
            ),
            SOTService(
                name="secrets.settings_policy",
                module="app.services.domain_settings",
                owns=(
                    "secret setting classification",
                    "secret setting reference persistence",
                ),
                depends_on=("secrets.reference_store",),
            ),
            SOTService(
                name="secrets.credential_crypto",
                module="app.services.credential_crypto",
                owns=(
                    "database credential encryption",
                    "credential field inventory",
                    "current and previous decryption key resolution",
                ),
                depends_on=("secrets.reference_store",),
            ),
            SOTService(
                name="secrets.rotation",
                module="app.services.credential_rotation_schedule",
                owns=(
                    "scheduled credential key lifecycle",
                    "rotation grace period",
                    "credential re-encryption convergence",
                ),
                depends_on=(
                    "secrets.reference_store",
                    "secrets.credential_crypto",
                    "runtime.db_sessions",
                ),
            ),
        ),
        entrypoints=(
            "app.tasks.security",
            "app.web.admin.system",
            "app.services.*",
        ),
        rule=(
            "Bootstrap secrets use environment or mounted files; application "
            "secrets use references; high-cardinality credentials use the "
            "declared encrypted-field inventory. Callers never choose storage."
        ),
    ),
    DomainSOT(
        domain="notifications_communications",
        services=(
            SOTService(
                name="communications.channel_policy",
                module="app.services.notification_channel_policy",
                owns=("channel eligibility", "channel preference resolution"),
            ),
            SOTService(
                name="communications.customer_policy",
                module="app.services.customer_notification_policy",
                owns=("customer notification eligibility",),
                depends_on=("customer.identity_scope",),
            ),
            SOTService(
                name="communications.event_policy",
                module="app.services.event_notification_policy",
                owns=(
                    "event notification enablement",
                    "balance notification suppression",
                ),
                depends_on=("communications.channel_policy",),
            ),
            SOTService(
                name="communications.notification_service",
                module="app.services.notification",
                owns=("notification row lifecycle", "delivery state"),
                depends_on=(
                    "communications.channel_policy",
                    "communications.event_policy",
                ),
            ),
            SOTService(
                name="communications.staff_notifications",
                module="app.services.staff_notifications",
                owns=("admin/staff notification creation",),
                depends_on=("communications.notification_service",),
            ),
            SOTService(
                name="communications.team_inbox",
                module="app.services.team_inbox_operations",
                owns=(
                    "conversation collaboration",
                    "conversation assignment",
                    "inbox reply and contact-link workflows",
                    "inbound channel ingestion",
                ),
                depends_on=(
                    "customer.identity_scope",
                    "communications.channel_policy",
                    "communications.notification_service",
                ),
            ),
        ),
        entrypoints=(
            "app.services.events.handlers.notification",
            "app.tasks.notifications",
            "app.web.admin.notifications",
            "app.services.team_inbox_*",
        ),
        rule=(
            "Domain services request communication outcomes; channel choice and "
            "notification rows stay inside communication services."
        ),
    ),
    DomainSOT(
        domain="events_webhooks",
        services=(
            SOTService(
                name="events.dispatcher",
                module="app.services.events.dispatcher",
                owns=("event routing", "handler orchestration"),
            ),
            SOTService(
                name="events.store",
                module="app.services.event_store",
                owns=("event persistence", "handler attempt tracking"),
                depends_on=("events.dispatcher",),
            ),
            SOTService(
                name="events.webhook_deliveries",
                module="app.services.webhook_deliveries",
                owns=("webhook delivery rows", "webhook queueing"),
                depends_on=("events.dispatcher",),
            ),
        ),
        entrypoints=(
            "app.services.events.handlers.*",
            "app.tasks.webhooks",
            "app.web.admin.integrations",
        ),
        rule=(
            "Handlers orchestrate; persistence, retry, and delivery bookkeeping "
            "live in event/webhook services."
        ),
    ),
    DomainSOT(
        domain="runtime_infrastructure",
        services=(
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
                    "native infrastructure poll observations",
                    "pollable device predicate",
                    "poll heartbeat result counters",
                ),
                depends_on=("runtime.db_sessions", "network.device_state"),
            ),
            SOTService(
                name="runtime.infrastructure_health",
                module="app.services.infrastructure_health",
                owns=(
                    "dependency health checks",
                    "Postgres/Redis/VM/Celery infrastructure status",
                ),
                depends_on=("runtime.db_sessions",),
            ),
        ),
        entrypoints=(
            "app.tasks.*",
            "app.main",
            "app.services.scheduler_config",
            "app.web.admin.system",
        ),
        rule=(
            "Infrastructure tasks use shared DB/session/lock and heartbeat "
            "helpers; polling writes observations while network/device resolvers "
            "interpret state."
        ),
    ),
    DomainSOT(
        domain="observability",
        services=(
            SOTService(
                name="observability.recording",
                module="app.services.observability",
                owns=("task/job run recording", "operational findings"),
            ),
            SOTService(
                name="observability.task_reliability",
                module="app.services.task_reliability",
                owns=("task reliability classification", "stale-run alerts"),
                depends_on=("observability.recording",),
            ),
            SOTService(
                name="observability.metrics",
                module="app.metrics",
                owns=("runtime counters", "runtime gauges"),
                depends_on=("observability.recording",),
            ),
        ),
        entrypoints=("app.tasks.*", "app.main", "app.services.*"),
        rule=(
            "Tasks and service loops record lifecycle through observability "
            "helpers instead of writing heartbeat/run state directly."
        ),
    ),
    DomainSOT(
        domain="provisioning_operations",
        services=(
            SOTService(
                name="operations.provisioning_context",
                module="app.services.provisioning_context",
                owns=("subscriber provisioning context", "ONT/CPE service link"),
                depends_on=("customer.identity_scope", "network.access_path"),
            ),
            SOTService(
                name="operations.provisioning_workflow",
                module="app.services.provisioning_managers",
                owns=("provisioning workflow execution", "provisioning step state"),
                depends_on=("operations.provisioning_context",),
            ),
            SOTService(
                name="operations.work_orders",
                module="app.services.work_order_views",
                owns=("work-order read models", "customer work-order linkage"),
                depends_on=("customer.identity_scope",),
            ),
        ),
        entrypoints=(
            "app.services.events.handlers.provisioning",
            "app.tasks.ont_provisioning",
            "app.web.admin.provisioning",
            "app.services.web_dispatch_work_orders",
        ),
        rule=(
            "Provisioning callers resolve customer/network context through the "
            "shared context layer before executing workflow steps."
        ),
    ),
    DomainSOT(
        domain="feature_control_plane",
        services=(
            SOTService(
                name="control.feature_registry",
                module="app.services.control_registry",
                owns=(
                    "module/feature/safety control resolution",
                    "legacy feature-flag alias mapping",
                    "feature-to-module composition",
                ),
                depends_on=("control.module_manager", "control.domain_settings"),
            ),
            SOTService(
                name="control.module_manager",
                module="app.services.module_manager",
                owns=("product module enablement", "module labels and feature states"),
            ),
            SOTService(
                name="control.domain_settings",
                module="app.services.domain_settings",
                owns=("domain setting persistence", "setting update validation"),
            ),
            SOTService(
                name="control.settings_spec",
                module="app.services.settings_spec",
                owns=("setting schema", "setting value coercion", "env fallback rules"),
                depends_on=("control.domain_settings",),
            ),
        ),
        entrypoints=(
            "app.services.scheduler_config",
            "app.tasks.*",
            "app.web.admin.system",
            "app.api.settings",
        ),
        rule=(
            "Callers ask the feature registry whether a capability is enabled; "
            "they should not independently compose module, env, DB, and legacy "
            "flag state."
        ),
    ),
    DomainSOT(
        domain="authorization_control_plane",
        services=(
            SOTService(
                name="auth.permission_gate",
                module="app.services.auth_dependencies",
                owns=(
                    "route permission dependencies",
                    "request principal permission checks",
                ),
                depends_on=("auth.rbac",),
            ),
            SOTService(
                name="auth.rbac",
                module="app.services.rbac",
                owns=("roles", "permissions", "role/user assignments"),
            ),
            SOTService(
                name="auth.staff_provisioning",
                module="app.services.staff_provisioning",
                owns=("staff account provisioning", "staff identity bootstrap"),
                depends_on=("auth.rbac",),
            ),
        ),
        entrypoints=("app.api.*", "app.web.admin.*", "app.web.auth.*"),
        rule=(
            "Routes declare permission requirements; RBAC services own role and "
            "permission mutation. Business services should receive an authorized "
            "principal, not perform route-level permission wiring."
        ),
    ),
    DomainSOT(
        domain="scheduler_control_plane",
        services=(
            SOTService(
                name="scheduler.registry",
                module="app.services.scheduler_config",
                owns=(
                    "effective scheduled-task registration",
                    "task toggle synchronization",
                    "Celery runtime schedule config",
                ),
                depends_on=("control.feature_registry", "runtime.db_sessions"),
            ),
            SOTService(
                name="scheduler.operations",
                module="app.services.scheduler",
                owns=("ScheduledTask CRUD", "manual task enqueue operations"),
                depends_on=("scheduler.registry",),
            ),
            SOTService(
                name="scheduler.worker_control",
                module="app.services.worker_control",
                owns=("worker restart targets", "worker control actions"),
                depends_on=("scheduler.registry",),
            ),
        ),
        entrypoints=("app.tasks.*", "app.web.admin.system", "app.main"),
        rule=(
            "Task cadence and enablement flow through scheduler config and the "
            "feature control plane; task bodies execute work and report status."
        ),
    ),
    DomainSOT(
        domain="network_access_control_plane",
        services=(
            SOTService(
                name="access.control_resolution",
                module="app.services.access_resolution",
                owns=(
                    "access-state command resolution",
                    "billable-service access eligibility",
                ),
                depends_on=("financial.billing_profile",),
            ),
            SOTService(
                name="access.event_policy",
                module="app.services.enforcement_event_policy",
                owns=(
                    "event-driven enforcement feature policy",
                    "FUP enforcement action settings",
                    "overdue suspension event policy",
                ),
                depends_on=("control.settings_spec",),
            ),
            SOTService(
                name="access.radius_state",
                module="app.services.radius_access_state",
                owns=("desired RADIUS state mapping", "RADIUS group/profile actions"),
                depends_on=("access.control_resolution", "access.event_policy"),
            ),
            SOTService(
                name="access.radius_reject",
                module="app.services.radius_reject",
                owns=("reject address allocation", "reject IP lifecycle"),
                depends_on=("access.radius_state",),
            ),
            SOTService(
                name="access.session_enforcement",
                module="app.services.enforcement",
                owns=("access-state CoA/disconnect execution",),
                depends_on=("access.radius_state", "sessions.radius_resolution"),
            ),
        ),
        entrypoints=(
            "app.services.events.handlers.enforcement",
            "app.tasks.enforcement",
            "app.services.collections.*",
            "app.services.usage",
        ),
        rule=(
            "Billing, FUP, and admin actions resolve the desired access outcome "
            "once, map it to RADIUS state once, then let enforcement apply the "
            "network-side change."
        ),
    ),
    DomainSOT(
        domain="service_intent_control_plane",
        services=(
            SOTService(
                name="service_intent.catalog_to_network",
                module="app.services.service_intent_adapter",
                owns=(
                    "catalog/subscription to network intent",
                    "network-safe subscription provisioning payloads",
                ),
                depends_on=("service_intent.catalog_policy",),
            ),
            SOTService(
                name="service_intent.catalog_policy",
                module="app.services.catalog.policies",
                owns=("catalog policy lookup", "offer policy interpretation"),
            ),
            SOTService(
                name="service_intent.catalog_validation",
                module="app.services.catalog.validation",
                owns=("catalog mutation validation", "offer/profile consistency"),
                depends_on=("service_intent.catalog_policy",),
            ),
            SOTService(
                name="service_intent.ont",
                module="app.services.network.ont_service_intent",
                owns=("ONT service intent projection",),
                depends_on=("service_intent.catalog_to_network", "network.access_path"),
            ),
        ),
        entrypoints=(
            "app.services.provisioning_*",
            "app.tasks.tr069.*",
            "app.web.admin.catalog",
            "app.web.admin.provisioning",
        ),
        rule=(
            "Catalog defines the commercial service; service-intent adapters "
            "translate it into network/provisioning payloads. Network code should "
            "not infer plan meaning directly from catalog models."
        ),
    ),
    DomainSOT(
        domain="integration_control_plane",
        services=(
            SOTService(
                name="integration.registry",
                module="app.services.integrations.registry",
                owns=("integration connector registry", "connector capabilities"),
            ),
            SOTService(
                name="integration.jobs",
                module="app.services.integration",
                owns=("integration targets", "integration jobs", "integration runs"),
                depends_on=("integration.registry",),
            ),
            SOTService(
                name="integration.sync",
                module="app.services.integration_sync",
                owns=("integration sync orchestration", "sync run lifecycle"),
                depends_on=("integration.jobs",),
            ),
            SOTService(
                name="integration.hooks",
                module="app.services.integration_hooks",
                owns=("integration hook dispatch", "hook subscriptions"),
                depends_on=("events.dispatcher", "integration.registry"),
            ),
        ),
        entrypoints=(
            "app.web.admin.integrations",
            "app.api.*_webhooks",
            "app.tasks.integrations",
            "app.services.events.handlers.integration_hook",
        ),
        rule=(
            "Integration routes and webhooks validate and enqueue; registry, job, "
            "sync, and hook services own connector behavior and delivery flow."
        ),
    ),
)


def domain_order() -> list[str]:
    return [domain.domain for domain in DOMAIN_SOT_RELATIONSHIPS]


def domain_relationship(domain_name: str) -> DomainSOT:
    for domain in DOMAIN_SOT_RELATIONSHIPS:
        if domain.domain == domain_name:
            return domain
    raise KeyError(domain_name)


def services_for_domain(domain_name: str) -> tuple[SOTService, ...]:
    return domain_relationship(domain_name).services


def service_names_for_domain(domain_name: str) -> tuple[str, ...]:
    return tuple(service.name for service in services_for_domain(domain_name))


def dependencies_for(service_name: str) -> tuple[str, ...]:
    for domain in DOMAIN_SOT_RELATIONSHIPS:
        for service in domain.services:
            if service.name == service_name:
                return service.depends_on
    raise KeyError(service_name)


def owning_service_for(concern: str) -> SOTService | None:
    needle = concern.strip().lower()
    if not needle:
        return None
    for domain in DOMAIN_SOT_RELATIONSHIPS:
        for service in domain.services:
            if any(needle in owned.lower() for owned in service.owns):
                return service
    return None
