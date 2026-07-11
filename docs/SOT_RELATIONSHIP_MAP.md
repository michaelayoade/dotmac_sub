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
6. `notifications_communications`
7. `events_webhooks`
8. `runtime_infrastructure`
9. `observability`
10. `provisioning_operations`
11. `feature_control_plane`
12. `authorization_control_plane`
13. `scheduler_control_plane`
14. `network_access_control_plane`
15. `service_intent_control_plane`
16. `integration_control_plane`

Rule: each PR should finish one domain slice: define the owner service, migrate
the highest-risk callers, and add focused tests. Avoid broad mechanical rewrites
that obscure business behavior.

## Financial and Access

1. Ledger and billing account services own money movement and balances.
2. Customer financial position owns read-side financial summaries.
3. Billing/access resolvers own entitlement and service-state decisions.
4. Dunning owns postpaid enforcement; prepaid enforcement owns prepaid access.

Rule: no module should infer access from draft invoices, ad hoc balances, or
legacy import fields when a billing/access resolver exists.

## Customer Context

1. Customer context owns identity, account, billing, service, support, and
network summary composition.
2. Customer network context owns the raw customer-to-network footprint.
3. Network access path owns the customer service path.

Rule: admin, portal, support, and reporting views should consume context
services instead of rebuilding customer joins.

## Notifications and Communications

1. Notification channel policy owns channel eligibility and preferences.
2. Notification service owns notification rows and delivery lifecycle.
3. Staff notification service owns internal/admin notification creation.
4. Team inbox services own conversation notes, assignment, and collaboration.

Rule: domain services request a notification outcome; they should not construct
notification rows or choose email/SMS/WhatsApp directly.

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

Rule: Celery tasks report lifecycle through shared observability helpers; they
should not write heartbeat/run rows directly unless they are the helper.

## Network Domain

Dependency order:

1. `network.identity`: resolves cross-model network/customer links.
2. `network.access_path`: resolves `subscriber/subscription -> access path`.
3. `network.radius_sessions`: resolves online-now state from active sessions.
4. `network.device_state`: resolves device state from admin/live/poll signals.
5. `network.outage_impact`: resolves affected customers from topology.
6. `network.events`: turns resolved state/impact into event decisions.

Rule: pollers write observations; resolver services decide state; event services
decide consequences. Customer-facing outage, SLA, expiry suppression, support
context, and escalation should consume these network SOT layers.

## Subscriber Sessions

Dependency order:

1. `sessions.radius_live_view`: owns `radius_active_sessions` mutations from
   accounting start/interim/stop events.
2. `sessions.radius_reconciliation`: reconciles external `radacct` open
   sessions into the live view and prunes dead rows.
3. `sessions.radius_resolution`: answers online-now and primary-session
   questions for customers/subscribers.
4. `sessions.enforcement`: owns CoA, disconnect, and session refresh outcomes
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

Rule: provisioning callers should resolve customer/network context once through
the operations context service before running workflow steps. Step executors may
consume context, but should not rediscover subscriber/ONT/CPE links themselves.

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
2. `access.radius_state`: maps desired access to RADIUS groups/profiles.
3. `access.radius_reject`: owns reject IP lifecycle.
4. `access.session_enforcement`: applies CoA/disconnect outcomes.

Rule: billing, FUP, and admin actions resolve the desired access outcome once,
map it to RADIUS state once, and let enforcement apply the network-side change.

Service intent:

1. `service_intent.catalog_policy`: owns catalog policy lookup.
2. `service_intent.catalog_validation`: owns catalog consistency checks.
3. `service_intent.catalog_to_network`: translates commercial subscriptions
   into network-safe provisioning payloads.
4. `service_intent.ont`: projects provisioning intent to ONT operations.

Rule: catalog defines the commercial service. Network/provisioning code consumes
service-intent payloads and should not infer plan meaning directly from catalog
models.

Integrations:

1. `integration.registry`: owns connectors and capabilities.
2. `integration.jobs`: owns targets, jobs, and runs.
3. `integration.sync`: owns sync orchestration.
4. `integration.hooks`: owns hook dispatch and subscriptions.

Rule: integration routes/webhooks validate and enqueue. Connector behavior,
sync lifecycle, and hook delivery stay inside integration services.
