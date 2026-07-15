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
                    "bounded cohort financial projections",
                ),
                depends_on=("financial.ledger",),
            ),
            SOTService(
                name="customer.service_status",
                module="app.services.service_status",
                owns=(
                    "customer-visible service health",
                    "customer financial action hints",
                    "payment-restores-service claims",
                ),
                depends_on=(
                    "financial.access_resolution",
                    "customer.financial_position",
                ),
            ),
            SOTService(
                name="customer.usage_summary",
                module="app.services.usage_summary",
                owns=(
                    "customer usage window definitions",
                    "customer usage headline totals",
                    "customer usage total provenance",
                ),
                depends_on=("sessions.radius_reconciliation",),
                notes=(
                    "Authoritative zero is a valid total. Customer clients do "
                    "not replace server totals with loaded-session pages or "
                    "retention-limited chart series."
                ),
            ),
            SOTService(
                name="customer.branding",
                module="app.services.brand_profiles",
                owns=(
                    "platform/reseller/organization brand profiles",
                    "customer-facing brand precedence",
                    "brand primary, secondary, and semantic UI color roles",
                    "runtime web theme token generation",
                    "legacy branding convergence",
                ),
                depends_on=("customer.identity_scope", "control.domain_settings"),
            ),
        ),
        entrypoints=(
            "app.web.customer",
            "app.api.me",
            "mobile",
            "app.services.customer_portal_*",
            "app.services.crm_api",
        ),
        rule=(
            "Customer-facing surfaces resolve scope once through customer context "
            "and compose network/financial summaries through services. Clients "
            "consume service-status action hints instead of inferring restoration "
            "policy from subscription status or invoice rows, and consume usage "
            "totals with their server-owned provenance instead of reconstructing "
            "headlines from partial client data."
        ),
    ),
    DomainSOT(
        domain="financial_access",
        services=(
            SOTService(
                name="financial.ledger",
                module="app.services.billing.ledger",
                owns=(
                    "append-only ledger record lifecycle",
                    "ledger reversal invariants",
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
                name="financial.payments",
                module="app.services.billing.payments",
                owns=(
                    "payment document lifecycle",
                    "payment allocation and account credit",
                    "payment-originated ledger postings",
                ),
                depends_on=("financial.ledger", "financial.billing_accounts"),
            ),
            SOTService(
                name="financial.invoices",
                module="app.services.billing.invoices",
                owns=(
                    "invoice document lifecycle",
                    "invoice status transitions",
                    "invoice adjustment and reversal postings",
                ),
                depends_on=("financial.ledger", "financial.billing_accounts"),
            ),
            SOTService(
                name="financial.credit_notes",
                module="app.services.billing.credit_notes",
                owns=(
                    "credit-note draft, issuance, application, and void lifecycle",
                    "credit-note issuance, allocation, and reversal ledger postings",
                    "credit-note spendability and posting idempotency",
                ),
                depends_on=("financial.ledger", "financial.invoices"),
            ),
            SOTService(
                name="financial.tax_configuration",
                module="app.services.billing.tax",
                owns=(
                    "configurable tax-rate records",
                    "tax-rate activation lifecycle",
                ),
            ),
            SOTService(
                name="financial.payment_proofs",
                module="app.services.payment_proofs",
                owns=(
                    "payment-proof review lifecycle",
                    "proof-backed payment request",
                    "withholding-tax receivable source records",
                ),
                depends_on=("financial.payments",),
            ),
            SOTService(
                name="financial.tax_accounting",
                module="app.services.tax_accounting",
                owns=(
                    "tax report semantics",
                    "output-tax invoice projection",
                    "withholding-tax receivable projection",
                    "tax report period and currency aggregation",
                    "credit-note tax recognition point",
                    "withholding-tax lifecycle",
                    "withholding-tax official timeline",
                    "net output-tax liability projection",
                ),
                depends_on=(
                    "financial.invoices",
                    "financial.tax_configuration",
                    "financial.payment_proofs",
                ),
                notes=(
                    "Issued output tax less issued credit-note tax adjustments is "
                    "the source-document liability, not cash collected, and "
                    "currencies remain separate. This owner also enforces legal "
                    "pending/certified/reclaimed/written-off WHT transitions and an "
                    "immutable evidence timeline. Dotmac ERP exclusively owns tax "
                    "account mappings, balanced journals, tax transactions, and "
                    "financial statements; Sub exports line tax treatment and WHT "
                    "facts through bounded sync feeds and has no local posting path."
                ),
            ),
            SOTService(
                name="financial.vas_wallet",
                module="app.services.vas_wallet",
                owns=(
                    "VAS wallet entry lifecycle",
                    "VAS spendable balance",
                    "atomic wallet-to-billing payment bridge",
                ),
                depends_on=("financial.payments",),
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
                name="financial.prepaid_threshold",
                module="app.services.prepaid_threshold",
                owns=(
                    "prepaid enforcement threshold",
                    "unfunded prepaid renewal requirement",
                ),
                depends_on=("financial.billing_profile",),
            ),
            SOTService(
                name="financial.prepaid_enforcement",
                module="app.services.prepaid_enforcement_planner",
                owns=(
                    "prepaid enforcement candidate cohort",
                    "prepaid warn/suspend/restore planning",
                    "prepaid enforcement readiness reporting",
                ),
                depends_on=(
                    "financial.ledger",
                    "financial.billing_profile",
                    "financial.prepaid_threshold",
                ),
            ),
            SOTService(
                name="financial.prepaid_plan_change",
                module="app.services.prepaid_plan_changes",
                owns=(
                    "prepaid plan-change proration decision",
                    "prepaid plan-change wallet affordability",
                    "idempotent plan-change debit and credit staging",
                ),
                depends_on=(
                    "financial.ledger",
                    "customer.financial_position",
                ),
                notes=(
                    "Immediate changes lock the account, recompute at write time, "
                    "and commit the financial adjustment with the subscription."
                ),
            ),
            SOTService(
                name="financial.access_resolution",
                module="app.services.access_resolution",
                owns=(
                    "billable service classification",
                    "RADIUS access decision",
                    "postpaid/prepaid enforcement cohorts",
                    "financial suspension/restoration eligibility",
                ),
                depends_on=(
                    "financial.billing_profile",
                    "financial.prepaid_threshold",
                    "customer.financial_position",
                ),
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
                    "financial.prepaid_enforcement",
                ),
            ),
            SOTService(
                name="financial.payment_provider_events",
                module="app.services.billing.providers",
                owns=(
                    "payment-provider event ingestion",
                    "provider-event idempotency",
                    "incomplete provider settlement resumption",
                ),
                depends_on=("financial.payments",),
            ),
            SOTService(
                name="financial.payment_webhooks",
                module="app.services.api_billing_webhooks",
                owns=(
                    "verified payment webhook projection",
                    "inbound payment dead-letter lifecycle",
                    "payment dead-letter replay",
                ),
                depends_on=("financial.payment_provider_events",),
            ),
            SOTService(
                name="financial.payment_reconciliation",
                module="app.services.payment_reconciliation",
                owns=(
                    "stranded top-up reconciliation",
                    "scheduled top-up reconciliation execution",
                ),
                depends_on=("financial.ledger", "financial.payment_provider_events"),
            ),
            SOTService(
                name="financial.vas_operations",
                module="app.services.vas_admin_commands",
                owns=(
                    "admin VAS mutation transactions",
                    "VAS manual transaction resolution",
                ),
                depends_on=("control.domain_settings", "financial.vas_refunds"),
            ),
            SOTService(
                name="financial.vas_refunds",
                module="app.services.vas_refunds",
                owns=(
                    "VAS refund-to-source eligibility",
                    "VAS refund request lifecycle",
                    "VAS refund wallet reservation and reversal projection",
                    "VAS refund provider reconciliation",
                ),
                depends_on=("control.domain_settings",),
                notes=(
                    "The VAS wallet is a separate customer-liability ledger; "
                    "a refund request and wallet reservation commit before the "
                    "gateway call. Gateway adapters provide observations but do "
                    "not decide eligibility or lifecycle state."
                ),
            ),
        ),
        entrypoints=(
            "app.services.billing_automation",
            "app.services.collections.*",
            "app.web.admin.billing_*",
            "app.web.admin.reports",
            "app.web.admin.vas",
            "app.api.billing",
            "app.services.payment_proofs",
            "app.services.web_reports_extended",
            "app.tasks.billing",
            "app.tasks.collections",
            "app.tasks.enforcement",
            "app.tasks.payment_reconciliation",
            "app.tasks.vas",
        ),
        rule=(
            "No caller infers access or balances from draft invoices, imported "
            "legacy fields, or ad hoc sums when ledger/access resolvers exist. "
            "Tax reports consume the tax-accounting projection, never label "
            "issued tax as collected cash, and never add different currencies. "
            "Tax account mappings and double-entry consequences are written only "
            "by Dotmac ERP from Sub's bounded source-fact feeds."
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
                owns=(
                    "online-now session state",
                    "primary NAS session",
                    "bounded historical NAS evidence",
                ),
                depends_on=("network.identity",),
            ),
            SOTService(
                name="network.device_state",
                module="app.services.device_operational_status",
                owns=(
                    "NOC-facing device operational status",
                    "device operational status vocabulary",
                    "device retry-pending and alarm classification",
                ),
                depends_on=("runtime.infrastructure_polling",),
            ),
            SOTService(
                name="network.operation_ledger",
                module="app.services.network_operations",
                owns=(
                    "tracked device operation lifecycle and status vocabulary",
                    "operation terminal-transition guard",
                    "correlation-key duplicate suppression",
                    "stale-active operation reclamation",
                    "parent/child operation status rollup",
                    "device operation re-execution eligibility",
                ),
                depends_on=("network.identity",),
                notes=(
                    "Owns whether a tracked device operation may run, resume, or "
                    "be re-executed. Celery tasks are transport adapters that "
                    "report progress through this ledger; they do not decide "
                    "retry eligibility. app.services.task_reliability declares "
                    "each task's contract and is a projection of this owner, not "
                    "a parallel authority — a task whose contract claims operator "
                    "redrive requires a redrive path here first."
                ),
            ),
            SOTService(
                name="network.control_plane_intent",
                module="app.services.control_plane_intent",
                owns=(
                    "shared desired-state delivery lifecycle",
                    "control-plane target and revision identity",
                    "vendor status projections and transition guards",
                ),
                depends_on=("network.identity",),
                notes=(
                    "Vendor adapters retain native persistence models but project "
                    "through one desired-to-readback lifecycle. Verified always "
                    "requires device evidence for the current intent revision."
                ),
            ),
            SOTService(
                name="network.routeros_sot",
                module="app.services.router_management.sot_policy",
                owns=(
                    "typed RouterOS desired-state contract",
                    "managed RouterOS resource and field policy",
                    "Dotmac RouterOS resource ownership identity",
                ),
                depends_on=(
                    "network.identity",
                    "runtime.db_sessions",
                    "observability.recording",
                ),
                notes=(
                    "Vendor-specific RouterOS desired state projects through the "
                    "shared network.control_plane_intent lifecycle."
                ),
            ),
            SOTService(
                name="network.nas_inventory",
                module="app.services.nas.devices",
                owns=("NAS administrative lifecycle state", "NAS inventory reads"),
                depends_on=("network.identity",),
            ),
            SOTService(
                name="network.nas_lifecycle",
                module="app.services.nas_lifecycle",
                owns=(
                    "NAS lifecycle reconciliation plans",
                    "subscription NAS relink decisions",
                    "NAS lifecycle RADIUS projection commands",
                ),
                depends_on=(
                    "network.identity",
                    "network.access_path",
                    "network.radius_sessions",
                    "network.nas_inventory",
                    "service_intent.subscription_nas_assignment",
                    "access.radius_state",
                    "runtime.db_sessions",
                    "observability.recording",
                ),
            ),
            SOTService(
                name="network.nas_access_path_evidence",
                module="app.services.nas_access_path_evidence",
                owns=(
                    "manual NAS lifecycle evidence reports",
                    "historical access-path review recommendations",
                ),
                depends_on=(
                    "network.radius_sessions",
                    "network.nas_lifecycle",
                    "runtime.db_sessions",
                ),
            ),
            SOTService(
                name="network.outage_impact",
                module="app.services.network.outage_impact",
                owns=("affected-customer impact", "outage scope impact"),
                depends_on=("network.access_path",),
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
                name="network.outage_lifecycle",
                module="app.services.topology.outage",
                owns=(
                    "persisted outage incident status vocabulary",
                    "outage incident lifecycle",
                    "outage event emission and escalation planning",
                ),
                depends_on=(
                    "network.outage_impact",
                    "events.dispatcher",
                ),
            ),
            SOTService(
                name="network.connection_health",
                module="app.services.topology.connection_status",
                owns=(
                    "customer-safe connection health vocabulary",
                    "customer-safe last-mile and area-outage verdict",
                    "customer connection headline, message, and advice",
                ),
                depends_on=(
                    "network.access_path",
                    "network.radius_sessions",
                    "network.outage_impact",
                    "network.outage_lifecycle",
                ),
                notes=(
                    "This customer diagnostic vocabulary is separate from "
                    "network.device_state and raw RADIUS session observations."
                ),
            ),
        ),
        entrypoints=(
            "app.services.topology.*",
            "app.services.infrastructure_*",
            "app.services.router_management.*",
            "app.tasks.network_*",
            "app.tasks.router_sync",
            "app.web.admin.network_*",
            "app.web.customer.connection",
            "app.api.me",
            "app.services.reseller_portal",
            "mobile",
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
                owns=("customer online-now resolution", "primary NAS session"),
                depends_on=("sessions.radius_reconciliation", "network.identity"),
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
                name="secrets.access_credential_format",
                module="app.services.access_credential_secret",
                owns=(
                    "access credential representation classification",
                    "one-way RADIUS hash preservation policy",
                    "explicit cleartext marker normalization",
                ),
            ),
            SOTService(
                name="secrets.credential_integrity",
                module="app.services.credential_key_rotation",
                owns=(
                    "credential integrity classification",
                    "plaintext credential remediation",
                    "credential integrity observability projection",
                    "credential re-encryption convergence",
                ),
                depends_on=(
                    "secrets.access_credential_format",
                    "secrets.credential_crypto",
                    "observability.recording",
                    "runtime.db_sessions",
                ),
            ),
            SOTService(
                name="secrets.rotation",
                module="app.services.credential_rotation_schedule",
                owns=(
                    "scheduled credential key lifecycle",
                    "rotation grace period",
                ),
                depends_on=(
                    "secrets.reference_store",
                    "secrets.credential_integrity",
                    "runtime.db_sessions",
                ),
            ),
            SOTService(
                name="secrets.credential_recovery",
                module="app.services.credential_lifecycle_cleanup",
                owns=(
                    "lost-key credential recovery planning",
                    "lifecycle-safe unrecoverable credential cleanup",
                    "reviewed cleanup plan digest enforcement",
                ),
                depends_on=(
                    "secrets.credential_integrity",
                    "network.identity",
                    "network.radius_sessions",
                    "access.radius_state",
                    "runtime.db_sessions",
                    "observability.recording",
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
                name="communications.customer_read_state",
                module="app.services.customer_portal_notifications",
                owns=(
                    "customer notification read/unread state",
                    "customer notification unread counts",
                    "legacy device read-state migration boundary",
                ),
                depends_on=(
                    "customer.identity_scope",
                    "communications.customer_policy",
                    "communications.notification_service",
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
                module="app.services.team_inbox_commands",
                owns=(
                    "conversation collaboration",
                    "conversation assignment",
                    "inbox reply and contact-link workflows",
                    "inbound channel ingestion",
                    "admin inbox mutation transactions",
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
            "app.api.me",
            "app.web.customer.routes",
            "app.web.admin.notifications",
            "app.web.admin.inbox",
            "app.services.team_inbox_*",
        ),
        rule=(
            "Domain services request communication outcomes; channel choice, "
            "notification rows, and recipient read state stay inside "
            "communication services. Admin inbox mutation routes delegate to "
            "the committed team-inbox command boundary."
        ),
    ),
    DomainSOT(
        domain="events_webhooks",
        services=(
            SOTService(
                name="events.dispatcher",
                module="app.services.events.dispatcher",
                owns=("event routing", "handler orchestration"),
                depends_on=("control.relationships",),
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
                depends_on=("runtime.db_sessions",),
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
                owns=(
                    "task/job run recording",
                    "operational findings",
                    "bounded state snapshot publication",
                ),
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
                owns=(
                    "runtime counters",
                    "runtime gauges",
                    "state snapshot scrape export",
                ),
                depends_on=("observability.recording",),
            ),
        ),
        entrypoints=("app.tasks.*", "app.main", "app.services.*"),
        rule=(
            "Tasks and service loops record lifecycle through observability "
            "helpers instead of writing heartbeat/run state directly. Metrics "
            "collectors read counters or bounded snapshots; unbounded business "
            "queries run only in scheduled single-flight producers."
        ),
    ),
    DomainSOT(
        domain="support_operations",
        services=(
            SOTService(
                name="support.ticket_lifecycle",
                module="app.services.support",
                owns=(
                    "ticket status vocabulary",
                    "guarded ticket status transitions",
                    "ticket lifecycle timestamps and consequences",
                ),
            ),
            SOTService(
                name="support.ticket_configuration",
                module="app.services.support_ticket_settings",
                owns=(
                    "operator-visible ticket status subset",
                    "ticket priority and type options",
                    "ticket routing and SLA policy",
                ),
                depends_on=("support.ticket_lifecycle",),
                notes=(
                    "Configured status choices are constrained to the lifecycle "
                    "vocabulary and do not own semantic colors or tones."
                ),
            ),
            SOTService(
                name="support.ticket_bulk_commands",
                module="app.services.web_support_ticket_bulk",
                owns=(
                    "selected support-ticket bulk membership resolution",
                    "support-ticket bulk change normalization",
                    "support-ticket bulk update eligibility preview",
                    "support-ticket bulk confirmation drift detection",
                    "structured support-ticket bulk update outcomes",
                ),
                depends_on=(
                    "support.ticket_lifecycle",
                    "support.ticket_configuration",
                    "ui.bulk_action_contracts",
                ),
                notes=(
                    "Execution delegates each eligible mutation to "
                    "app.services.support.Tickets.update through Tickets.bulk_update "
                    "so SLA, automation, assignment, work-order, notification, "
                    "event, audit, and workqueue consequences have one owner."
                ),
            ),
        ),
        entrypoints=(
            "app.api.support",
            "app.api.me.support",
            "app.web.admin.support",
            "app.web.customer.support",
            "mobile",
        ),
        rule=(
            "Support adapters request ticket mutations through the ticket service. "
            "The lifecycle owner validates raw statuses; settings may expose a "
            "subset but cannot add states or define their semantic presentation."
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
                name="operations.work_order_status",
                module="app.services.field.work_order_status",
                owns=(
                    "persisted work-order status vocabulary",
                    "open, assignable, and terminal work-order status sets",
                ),
            ),
            SOTService(
                name="operations.work_orders",
                module="app.services.work_order_views",
                owns=("work-order read models", "customer work-order linkage"),
                depends_on=(
                    "customer.identity_scope",
                    "operations.work_order_status",
                ),
            ),
            SOTService(
                name="operations.field_completion",
                module="app.services.field.transitions",
                owns=(
                    "field job completion eligibility",
                    "field completion evidence requirements",
                    "field job completion transitions",
                ),
                depends_on=(
                    "operations.work_orders",
                    "operations.work_order_status",
                    "control.domain_settings",
                ),
                notes=(
                    "Authenticated field job detail projects the same completion "
                    "requirements consumed by transition validation. Field clients "
                    "do not reconstruct this policy."
                ),
            ),
            SOTService(
                name="operations.project_lifecycle",
                module="app.services.projects",
                owns=(
                    "native project field and status mutations",
                    "project SLA clock synchronization",
                    "project lifecycle event and notification requests",
                ),
                depends_on=(
                    "events.dispatcher",
                    "communications.staff_notifications",
                ),
                notes=(
                    "Customer and reseller read authority remains controlled by "
                    "projects.native_read until the CRM mirror cutover is complete."
                ),
            ),
        ),
        entrypoints=(
            "app.services.events.handlers.provisioning",
            "app.tasks.ont_provisioning",
            "app.web.admin.provisioning",
            "app.web.admin.projects",
            "app.api.projects",
            "app.api.field.*",
            "app.services.web_projects",
            "app.services.web_dispatch_work_orders",
            "field_mobile",
        ),
        rule=(
            "Provisioning callers resolve customer/network context through the "
            "shared context layer before executing workflow steps. Native project "
            "mutation adapters delegate to Projects.update for lifecycle consequences. "
            "Field clients consume completion_requirements from authenticated job "
            "detail and leave completion eligibility to the field transition service."
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
            SOTService(
                name="control.relationships",
                module="app.services.control_relationships",
                owns=(
                    "setting exclusivity and migration-chain validation",
                    "event handler stage and capability ownership",
                    "control relationship diagnostics",
                ),
                depends_on=("control.domain_settings", "control.settings_spec"),
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
                name="service_intent.catalog_billing_governance",
                module="app.services.catalog_billing_governance",
                owns=(
                    "billing-critical catalog mutation policy",
                    "live pricing and cadence immutability",
                    "billing catalog audit and operator alerting",
                ),
                depends_on=(
                    "service_intent.catalog_validation",
                    "auth.permission_gate",
                    "observability.recording",
                ),
            ),
            SOTService(
                name="service_intent.subscription_nas_assignment",
                module="app.services.catalog.subscriptions",
                owns=(
                    "subscription provisioning NAS assignment",
                    "nonterminal services grouped by NAS",
                ),
                depends_on=("service_intent.catalog_policy",),
            ),
            SOTService(
                name="service_intent.subscription_lifecycle",
                module="app.services.subscription_lifecycle",
                owns=(
                    "subscription lifecycle state projection",
                    "subscription command eligibility and preview",
                    "billing and access impact projection",
                    "subscription command and outcome contracts",
                ),
                depends_on=(
                    "service_intent.catalog_policy",
                    "financial.access_resolution",
                    "financial.prepaid_plan_change",
                    "access.radius_state",
                ),
                notes=(
                    "Execution remains with the established billing, account "
                    "lifecycle, catalog, and RADIUS owners. UI, API, scheduled, "
                    "and bulk callers consume this preview before execution."
                ),
            ),
            SOTService(
                name="service_intent.subscription_lifecycle_execution",
                module="app.services.subscription_lifecycle_commands",
                owns=(
                    "single-subscription command orchestration",
                    "subscription command locking and reviewed-head enforcement",
                    "subscription command idempotent replay",
                    "structured subscription command outcomes",
                    "independently committed subscription command batches",
                ),
                depends_on=(
                    "service_intent.subscription_lifecycle",
                    "service_intent.catalog_policy",
                    "financial.prepaid_plan_change",
                    "access.radius_state",
                ),
                notes=(
                    "Delegates mutations and side effects to the established "
                    "account lifecycle, catalog, billing, scheduler, and RADIUS "
                    "owners. Renewal execution remains billing-owned and fails "
                    "closed. Deferred status execution is owned by "
                    "service_intent.subscription_lifecycle_scheduling. Admin "
                    "single and bulk adapters delegate here instead of writing "
                    "subscription lifecycle fields directly."
                ),
            ),
            SOTService(
                name="service_intent.subscription_lifecycle_scheduling",
                module="app.services.subscription_lifecycle_schedules",
                owns=(
                    "durable deferred subscription status intent",
                    "deferred command execution leases and bounded retry",
                    "scheduled lifecycle cancellation",
                    "deferred lifecycle execution evidence",
                ),
                depends_on=(
                    "service_intent.subscription_lifecycle",
                    "service_intent.subscription_lifecycle_execution",
                    "scheduler.registry",
                ),
                notes=(
                    "Revalidates the reviewed subscription head at execution "
                    "time and delegates every mutation to the canonical command "
                    "executor. Plan scheduling remains with the catalog change "
                    "request owner."
                ),
            ),
            SOTService(
                name="service_intent.ont",
                module="app.services.network.ont_service_intent",
                owns=("ONT service intent projection",),
            ),
        ),
        entrypoints=(
            "app.services.provisioning_*",
            "app.tasks.tr069.*",
            "app.web.admin.catalog",
            "app.web.admin.provisioning",
        ),
        rule=(
            "Catalog policy and subscription services define commercial intent; "
            "network owners project configured intent without a parallel adapter."
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
    DomainSOT(
        domain="ui_list_projection",
        services=(
            SOTService(
                name="ui.list_contracts",
                module="app.services.list_query",
                owns=(
                    "list query normalization",
                    "page metadata derivation",
                    "canonical list URL serialization",
                    "list capability declarations",
                ),
            ),
            SOTService(
                name="ui.customer_list_projection",
                module="app.services.web_customer_lists",
                owns=(
                    "admin customer searchable fields",
                    "admin customer filter semantics",
                    "admin customer stable sort semantics",
                    "admin customer row and page projection",
                    "legacy customer offset API compatibility mapping",
                ),
                depends_on=("ui.list_contracts",),
            ),
            SOTService(
                name="ui.subscriber_list_projection",
                module="app.services.web_subscriber_lists",
                owns=(
                    "subscriber table searchable fields",
                    "subscriber table filter semantics",
                    "subscriber table stable sort semantics",
                    "subscriber table page projection",
                    "legacy subscriber offset API compatibility mapping",
                ),
                depends_on=("ui.list_contracts",),
                notes=(
                    "Subscriber scope and full-text search delegate to "
                    "app.services.subscriber.Subscribers.query. List reads never "
                    "generate or persist subscriber identifiers."
                ),
            ),
            SOTService(
                name="ui.invoice_list_projection",
                module="app.services.web_billing_overview",
                owns=(
                    "admin invoice searchable fields",
                    "admin invoice filter semantics",
                    "admin invoice stable sort semantics",
                    "admin invoice page and status-summary projection",
                    "admin invoice export scope",
                ),
                depends_on=("ui.list_contracts", "financial.invoices"),
                notes=(
                    "The full page and HTMX response share one list partial. "
                    "Exports consume the same canonical scope without a page cap."
                ),
            ),
            SOTService(
                name="ui.support_ticket_list_projection",
                module="app.services.web_support_tickets",
                owns=(
                    "admin support-ticket searchable fields",
                    "admin support-ticket filter semantics",
                    "admin support-ticket stable sort semantics",
                    "admin support-ticket page and status-summary projection",
                    "admin support-ticket export scope",
                ),
                depends_on=(
                    "ui.list_contracts",
                    "support.ticket_lifecycle",
                    "support.ticket_configuration",
                ),
                notes=(
                    "app.services.support.Tickets owns the canonical filtered "
                    "domain query. The web projection declares list capabilities, "
                    "normalizes request state, and renders full-page and HTMX "
                    "reads through one partial. Exports consume the same complete "
                    "scope without a silent row cap."
                ),
            ),
        ),
        entrypoints=(
            "app.api.tables",
            "app.services.subscriber",
            "app.services.table_config",
            "app.web.admin.customers",
            "app.web.admin.billing_invoices",
            "app.web.admin.support_tickets",
            "templates.admin.billing.invoices",
            "templates.admin.customers",
            "templates.admin.support.tickets",
        ),
        rule=(
            "List routes normalize request parameters through one declared list "
            "contract. Owners filter before pagination and apply a stable unique "
            "tie-breaker. Compatibility APIs delegate row selection to a named "
            "resource owner and list reads do not mutate domain records. Templates "
            "consume ListQuery and PageMeta, preserve the canonical URL, and do not "
            "rebuild pagination or sort semantics."
        ),
    ),
    DomainSOT(
        domain="ui_bulk_actions",
        services=(
            SOTService(
                name="ui.bulk_action_contracts",
                module="app.services.bulk_actions",
                owns=(
                    "bulk selection mode normalization",
                    "bulk action capability presentation",
                    "bulk preview and confirmation declarations",
                    "bulk execution-mode presentation",
                ),
                depends_on=("ui.list_contracts",),
                notes=(
                    "These are read-side interaction contracts. Domain command "
                    "owners re-check permission, eligibility, scope, and impact "
                    "when executing a mutation."
                ),
            ),
            SOTService(
                name="ui.customer_bulk_action_projection",
                module="app.services.web_customer_bulk_actions",
                owns=(
                    "admin customer bulk action visibility",
                    "admin customer bulk selection presentation",
                    "admin customer filtered-selection promotion",
                ),
                depends_on=(
                    "ui.bulk_action_contracts",
                    "ui.customer_list_projection",
                ),
            ),
            SOTService(
                name="ui.invoice_bulk_action_projection",
                module="app.services.web_billing_invoice_bulk_actions",
                owns=(
                    "admin invoice bulk action visibility",
                    "admin invoice page-selection presentation",
                    "admin invoice bulk eligibility presentation",
                ),
                depends_on=(
                    "ui.bulk_action_contracts",
                    "ui.invoice_list_projection",
                    "financial.invoices",
                ),
                notes=(
                    "app.services.web_billing_invoice_bulk remains the command "
                    "eligibility, preview, mutation, audit, and outcome owner."
                ),
            ),
            SOTService(
                name="ui.support_ticket_bulk_action_projection",
                module="app.services.web_support_ticket_bulk_actions",
                owns=(
                    "admin support-ticket bulk action visibility",
                    "admin support-ticket page-selection presentation",
                    "admin support-ticket row eligibility presentation",
                ),
                depends_on=(
                    "ui.bulk_action_contracts",
                    "ui.support_ticket_list_projection",
                    "support.ticket_bulk_commands",
                ),
                notes=(
                    "Selection is page-only. The command owner previews exact "
                    "membership, proposed changes, and eligibility before execution."
                ),
            ),
        ),
        entrypoints=(
            "app.web.admin.customers",
            "app.web.admin.billing_invoice_bulk",
            "app.web.admin.billing_invoices",
            "app.web.admin.support_tickets",
            "app.services.web_customer_actions",
            "app.services.web_billing_invoice_bulk",
            "app.services.web_support_ticket_bulk",
            "templates.admin.billing.invoices",
            "templates.admin.customers",
            "templates.admin.support.tickets",
        ),
        rule=(
            "No selection means no bulk action. Page select-all selects only the "
            "visible page; all-filtered scope requires an explicit promotion. "
            "Adapters submit selected IDs or a canonical filtered query, and "
            "command owners resolve the scope again, require impact preview and "
            "confirmation, reject membership or eligibility drift, and report "
            "structured outcomes."
        ),
    ),
    DomainSOT(
        domain="ui_display_formatting",
        services=(
            SOTService(
                name="ui.display_formatting",
                module="app.services.display_format",
                owns=(
                    "display currency-code normalization",
                    "single-value money formatting",
                    "multi-currency summary grouping and ordering",
                    "display-timezone resolution",
                    "timestamp display formatting",
                    "missing-value display marker",
                ),
                depends_on=("control.settings_spec",),
                notes=(
                    "Domain services own amount, currency, unit, timestamp, and "
                    "missing-value facts. Web and mobile renderers consume this "
                    "projection and do not invent default currency or timezone."
                ),
            ),
        ),
        entrypoints=(
            "app.services.web_billing_overview",
            "app.services.web_billing_payments",
            "app.services.web_billing_ledger",
            "app.services.web_billing_reconciliation",
            "app.web.brand_globals",
            "mobile.lib.src.core.formatters",
        ),
        rule=(
            "Domain owners provide typed amount, currency, unit, timestamp, and "
            "availability facts. Display owners normalize and format them once. "
            "Mixed currencies remain separate and explicitly labeled; UI callers "
            "do not maintain local currency defaults or formatter copies."
        ),
    ),
    DomainSOT(
        domain="ui_semantic_presentation",
        services=(
            SOTService(
                name="ui.status_presentation",
                module="app.services.status_presentation",
                owns=(
                    "account status labels, semantic tones, and icon keys",
                    "subscription status labels, semantic tones, and icon keys",
                    "invoice status labels, semantic tones, and icon keys",
                    "payment status labels, semantic tones, and icon keys",
                    "outage incident status labels, semantic tones, and icon keys",
                    "device operational status labels, semantic tones, and icon keys",
                    "customer connection health labels, semantic tones, and icon keys",
                    "RADIUS access-session observation labels, semantic tones, and icon keys",
                    "support-ticket status labels, semantic tones, and icon keys",
                    "field work-order status labels, semantic tones, and icon keys",
                    "status presentation fallback semantics",
                ),
                depends_on=(
                    "financial.invoices",
                    "financial.payments",
                    "network.device_state",
                    "network.connection_health",
                    "network.outage_lifecycle",
                    "support.ticket_lifecycle",
                    "operations.work_order_status",
                ),
                notes=(
                    "Domain services own lifecycle or derived operational state. "
                    "This read projection owns its cross-client semantic meaning; "
                    "customer.branding owns the concrete color behind each tone. "
                    "Clients render the tone through brand/theme tokens and do not "
                    "keep local tone-to-color maps."
                ),
            ),
        ),
        entrypoints=(
            "app.schemas.catalog.SubscriptionRead",
            "app.schemas.billing.InvoiceRead",
            "app.schemas.billing.PaymentRead",
            "app.schemas.service_status.ServiceStatusItem",
            "app.schemas.support.TicketRead",
            "app.schemas.network_monitoring.NetworkDeviceRead",
            "app.services.crm_api.outage_incident_row",
            "app.services.web_customer_lists",
            "app.services.web_customer_details",
            "app.services.customer_portal_context",
            "app.schemas.field.FieldJobSummary",
            "app.schemas.field.FieldManagerJob",
            "app.services.field.map_search",
            "templates.admin.customers",
            "templates.admin.billing",
            "templates.admin.network.outages",
            "templates.admin.network.core-devices",
            "templates.admin.network.network-devices",
            "templates.admin.network.monitoring",
            "templates.customer.connection",
            "templates.reseller.dashboard",
            "templates.customer.dashboard.restricted",
            "templates.customer.billing",
            "templates.admin.support.tickets",
            "templates.customer.support",
            "mobile",
            "field_mobile",
        ),
        rule=(
            "Domain state owners provide raw or derived status values. Server read "
            "projections add one StatusPresentation label/tone/icon contract. "
            "Templates and mobile clients render that contract and do not map "
            "the same domain values independently."
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
