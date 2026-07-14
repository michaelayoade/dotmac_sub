# Single Source of Truth Relationship Map

This document names the service layers that should own decisions. Web/API
routes and Celery tasks should be thin wrappers around these services.

The executable registry is `app/services/sot_relationships.py`. When a domain
is harmonised, add or update its service boundary there and cover it with tests
before migrating more callers.

## Domain Order

1. `customer_context`
2. `financial_access`
3. `network`
4. `subscriber_sessions`
5. `application_sessions`
6. `secrets_credentials`
7. `notifications_communications`
8. `events_webhooks`
9. `runtime_infrastructure`
10. `observability`
11. `provisioning_operations`
12. `feature_control_plane`
13. `authorization_control_plane`
14. `scheduler_control_plane`
15. `network_access_control_plane`
16. `service_intent_control_plane`
17. `integration_control_plane`

Rule: each PR should finish one domain slice: define the owner service, migrate
the highest-risk callers, and add focused tests. Avoid broad mechanical rewrites
that obscure business behavior.

## Financial and Access

1. `financial.ledger` owns the append-only record lifecycle and reversal
   invariant. Domain owners decide why money moves.
2. `financial.payments`, `financial.invoices`, and `financial.credit_notes` own
   their document lifecycles and the ledger postings those transitions require.
3. `financial.vas_wallet` owns its separate append-only wallet, spendable
   balance, and atomic bridge into `financial.payments` for bill settlement.
4. Customer financial position owns read-side financial summaries, including
   the bounded bulk projection used by cohort monitoring. Bulk callers do not
   loop the single-customer ledger reader.
5. `financial.access_resolution` owns financial suspension/restoration
   eligibility. For prepaid service, both directions compare the customer
   financial position with the single `financial.prepaid_threshold`; the
   existence or size of one payment is never itself permission to restore.
6. `financial.prepaid_enforcement` owns the prepaid candidate cohort and the
   warn/suspend/restore plan consumed by both dry-run and execution. It consumes
   the funding decision from `financial.access_resolution`; it does not create
   another balance or threshold rule.
7. `financial.prepaid_plan_change` owns the immediate prepaid plan-change quote,
   affordability decision, and idempotent financial adjustment. It locks the
   account and recomputes at write time; portal, admin, API, and change-request
   application paths do not post their own plan-change debit.
8. Dunning owns postpaid enforcement; prepaid enforcement owns prepaid access.
   Both converge on the account lifecycle writer, which re-checks billing
   profile validity, payment-arrangement/proof/extension shields, and billing
   enforcement health immediately before a financial suspension.
9. Scheduled billing, collections, and payment-reconciliation services own DB
   sessions, transaction outcomes, and operational logging for Celery runners.
10. `financial.payment_webhooks` owns signature-verified provider-payload
   projection and inbound dead-letter lifecycle. Replay rebuilds the same
   settlement command as live delivery; `financial.payment_provider_events`
   owns idempotent event processing, delegates the monetary write to the
   payment owner, and must resume an incomplete event rather than treating
   receipt identity as proof that money was posted.
11. `financial.vas_operations` owns admin VAS mutation transactions and manual
   purchase resolution. `financial.vas_refunds` exclusively owns
   refund-to-source eligibility, the durable request lifecycle, and wallet
   reservation/reversal projection. It commits the request and wallet debit
   before contacting the gateway; provider responses are observations that an
   idempotent reconciler can replay. Gateway adapters only submit or observe a
   refund against the original funding transaction and never decide eligibility
   or lifecycle state.

Rule: no module should infer access from draft invoices, ad hoc balances, or
legacy import fields when a billing/access resolver exists. Celery tasks only
apply scheduling, routing, idempotency, and feature-gate concerns before calling
the owning financial service. Admin VAS routes do not mutate catalog or
transaction rows, control wallet transactions, or decide whether funds may
leave through a gateway.

## Customer Context

1. Customer context owns identity, account, billing, service, support, and
network summary composition.
2. Customer network context owns the raw customer-to-network footprint.
3. Network access path owns the customer service path.
4. `customer.service_status` owns customer-visible service health and action
   hints, including whether payment can restore every active service hold and
   the authoritative amount required by financial policy.
5. `customer.usage_summary` owns customer usage windows, headline totals, and
   total provenance. An authoritative zero is a valid value, not a missing-data
   sentinel.

Rule: admin, portal, support, and reporting views should consume context
services instead of rebuilding customer joins. Customer clients must not infer
that `blocked` or `suspended` means payment-restorable, or calculate restoration
amounts from locally loaded invoice rows; they consume `/me/service-status`.
Customer clients consume `/me/usage-summary` totals and provenance; they do not
replace a server total with a loaded-session page, chart-series sum, or a
different time window.

## Secrets and Credentials

1. Bootstrap secrets required before the application starts use environment or
   mounted secret files.
2. Low-cardinality application and integration secrets use OpenBao references.
3. High-cardinality customer, device, and connector credentials use the
   declared encrypted database-field inventory.
4. Scheduled rotation stages current and previous keys, converges stored
   ciphertext, and retires the previous key only after the grace period.

Rule: callers request a secret or credential outcome from the owning service.
They do not choose fallback precedence, store plaintext, reveal existing values
in forms, or rotate key material directly.

## Notifications and Communications

1. Notification channel policy owns channel eligibility and preferences.
2. Event notification policy owns event enablement and balance-notification
   suppression.
3. Notification service owns notification rows and delivery lifecycle.
4. Staff notification service owns internal/admin notification creation.
5. `communications.customer_read_state` owns customer notification read/unread
   state and unread counts across the web portal and mobile app. Subscriber
   metadata is its bounded persistence mechanism; `/me/notifications` projects
   that state, and `/me/notifications/read` is the self-scoped mutation
   boundary. Device storage is only a one-way legacy migration input. The
   identity-cleared GET response cache may render last-known state offline but
   never accepts read decisions.
6. `communications.team_inbox` owns conversation notes, assignment, replies,
   contact-linking, widget writes, inbound-channel ingestion, collaboration,
   and admin mutation transactions. `app.services.team_inbox_commands` is the
   committed admin command boundary; `app.web.admin.inbox` only translates HTTP
   inputs and outcomes.
7. Campaign services own marketing audience, sequence, and content decisions.
   They request a canonical sender key; email delivery alone resolves that key
   to SMTP identity and credentials.

Rule: domain services request a notification outcome; they should not construct
notification rows, choose email/SMS/WhatsApp directly, or maintain recipient
read state outside the owning service. Admin inbox routes must not load or
mutate inbox ORM rows, control commits, or select alternate mutation helpers.

## Events and Webhooks

1. Event dispatcher owns event routing.
2. Event-store service owns event rows, handler attempts, retry lookup, cleanup,
   and stale processing.
3. Webhook delivery service owns webhook delivery rows and queueing.
4. Subscription lifecycle event service owns lifecycle audit rows.

Rule: handlers orchestrate. Persistence and retry bookkeeping live in services.

## Observability

1. Observability service owns task/job run recording.
2. Task reliability owns task metadata, heartbeat interpretation, and alerting.
3. Metrics collectors expose read-only gauges/counters for runtime pressure.
4. Scheduled single-flight producers own expensive business-health snapshots;
   metrics collectors only read those bounded snapshots.

Rule: Celery tasks report lifecycle through shared observability helpers; they
should not write heartbeat/run rows directly unless they are the helper.
Scrape-time collectors must never perform unbounded business-table scans or
per-customer financial reconstruction.

## Network Domain

Dependency order:

1. `network.identity`: resolves cross-model network/customer links.
2. `network.monitoring_inventory`: owns monitoring inventory, metric records,
   alert rules, and alert state mutations.
3. `network.access_path`: resolves `subscriber/subscription -> access path`.
4. `network.radius_sessions`: resolves online-now state from active sessions.
5. `network.device_state`: derives NOC operational state, retry state, and alarm
   classification from administrative intent and monitoring observations.
6. `network.outage_impact`: resolves affected customers from topology.
7. `network.device_groups`: owns device-group mutations, membership, and bulk
   action queueing.
8. `network.outage_lifecycle`: owns incident transitions, escalation planning,
   and outage event emission.

Rule: pollers write observations; resolver services decide state; event services
decide consequences. Customer-facing outage, SLA, expiry suppression, support
context, and escalation should consume these network SOT layers.

## Subscriber Sessions

Dependency order:

1. `sessions.radius_reconciliation`: is the canonical writer of the
   `radius_active_sessions` projection; it reconciles external `radacct` open
   sessions and prunes dead rows.
2. `sessions.radius_resolution`: answers online-now and primary-session
   questions for customers/subscribers.
3. `sessions.enforcement`: owns CoA, disconnect, and session refresh outcomes
   after billing/access/FUP state changes.

Rule: accounting imports write session facts; resolvers answer online state;
enforcement applies network-side consequences. Billing/access code should not
query `RadiusAccountingSession` or `radius_active_sessions` directly to decide
access.

## Application Sessions

Dependency order:

1. `app_sessions.store`: owns Redis-backed storage, principal indexes, fallback
   store, and revocation epochs.
2. `app_sessions.customer_portal`: owns customer portal session lifecycle,
   refresh, revoke-all, impersonation, and read-only policy.
3. `app_sessions.auth`: owns database auth-session listing and revocation.

Rule: routes authenticate and authorize, but session lifecycle and revocation
policy belongs in session services. Do not duplicate cookie/session mutation
logic in route handlers.

## Runtime Infrastructure

Dependency order:

1. `runtime.db_sessions`: owns background DB session lifecycle and advisory lock
   safety.
2. `runtime.task_idempotency`: owns duplicate suppression and stale task
   execution rows.
3. `runtime.task_heartbeat`: owns task success/skip heartbeat signals.
4. `runtime.infrastructure_polling`: owns native poll observations and the
   pollable-device predicate.
5. `runtime.infrastructure_health`: owns dependency health checks for
   Postgres, Redis, VictoriaMetrics, Celery, and related infrastructure.

Rule: tasks should use shared DB-session, lock, idempotency, and heartbeat
helpers. Infrastructure pollers write observations only; network/device SOT
services interpret state for customer impact, alerts, and SLA.

## Provisioning Operations

Dependency order:

1. `operations.provisioning_context`: composes subscriber, subscription, ONT,
   CPE, TR-069, ACS, service address, and NAS context.
2. `operations.provisioning_workflow`: executes service-order workflows and
   provisioning steps from the resolved context.
3. `operations.work_orders`: exposes work-order read models and customer links.
4. `operations.field_completion`: owns field-job completion eligibility, evidence
   requirements, and completion transitions.
5. `operations.project_lifecycle`: owns native project field/status mutations,
   project SLA synchronization, and lifecycle event/notification requests.

Rule: provisioning callers should resolve customer/network context once through
the operations context service before running workflow steps. Step executors may
consume context, but should not rediscover subscriber/ONT/CPE links themselves.
`Projects.update` is the canonical writer for native project mutations;
Kanban, Gantt, normal edit, API, and web adapters delegate to it rather than
maintaining parallel SLA/event/notification paths. Customer and reseller read
authority remains controlled by `projects.native_read` until the documented CRM
mirror cutover is complete. Field job detail projects `completion_requirements`
from the same transition service that validates completion. Field clients consume
that contract and may offer advisory quality checks, but must not invent a separate
completion gate from local checklist state or cached settings.

## Control Planes

Feature controls:

1. `control.module_manager`: owns product module enablement.
2. `control.domain_settings`: owns stored setting mutation.
3. `control.settings_spec`: owns setting schema, coercion, and env fallback.
4. `control.feature_registry`: composes module, feature, safety, canonical, and
   legacy flag resolution.

Rule: task and feature gates should call the feature registry. Callers should
not separately read env vars, domain settings, module state, and legacy flags.
Registered capability gates include billing capture/collections/payment
options, prepaid monthly invoicing, RADIUS/session enforcement, VAS wallet,
usage/FUP emission gates, CRM/native transition flags, and GIS/network worker
toggles. Numeric intervals, thresholds, profile IDs, account lists, and other
tuning values remain in `settings_spec`.

Authorization:

1. `auth.rbac`: owns roles, permissions, and assignments.
2. `auth.permission_gate`: owns request/route permission dependencies.
3. `auth.staff_provisioning`: owns staff account bootstrap.

Rule: routes declare permissions and business services receive an authorized
principal. RBAC mutation stays inside RBAC services.

Scheduler:

1. `scheduler.registry`: owns effective task registration, cadence, and toggle
   synchronization.
2. `scheduler.operations`: owns `ScheduledTask` CRUD and manual enqueue.
3. `scheduler.worker_control`: owns worker restart targets/actions.

Rule: task cadence and enablement flow through scheduler config and the feature
control plane. Task bodies execute work and report status.

Network access:

1. `access.control_resolution`: owns desired service access outcomes.
2. `access.event_policy`: owns event-driven enforcement settings, FUP action
   policy, and overdue suspension policy reads.
3. `access.radius_state`: maps desired access to RADIUS groups/profiles.
4. `access.radius_reject`: owns reject IP lifecycle.
5. `access.session_enforcement`: applies CoA/disconnect outcomes.

Rule: billing, FUP, and admin actions resolve the desired access outcome once,
map it to RADIUS state once, and let enforcement apply the network-side change.

Service intent:

1. `service_intent.catalog_policy`: owns catalog policy lookup.
2. `service_intent.catalog_validation`: owns catalog consistency checks.
3. `service_intent.catalog_billing_governance`: owns billing-critical catalog
   mutation safety, audit, and operator alerts. Live pricing/cadence is versioned
   rather than edited in place, and routes require `catalog:billing_write`.
4. `service_intent.subscription_nas_assignment`: owns commercial-service NAS
   assignment.
5. `service_intent.ont`: projects provisioning intent to ONT operations.

Rule: catalog policy and subscription owners define commercial intent. Network
owners project configured intent without a parallel catalog-to-network adapter.

Integrations:

1. `integration.registry`: owns connectors and capabilities.
2. `integration.jobs`: owns targets, jobs, and runs.
3. `integration.sync`: owns sync orchestration.
4. `integration.hooks`: owns hook dispatch and subscriptions.

Rule: integration routes/webhooks validate and enqueue. Connector behavior,
sync lifecycle, and hook delivery stay inside integration services.
