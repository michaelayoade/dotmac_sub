# Sales-to-Service Lifecycle Source of Truth

**Status:** Approved and implemented by migration 389
**System of record:** Sub
**Decision owner:** Michael

## Contract

```text
signed interaction / staff capture
  -> IntegrationInbox receipt (when external)
  -> Party + immutable Lead origin
  -> exact Lead/Party account conversion
  -> Quote
  -> SalesOrder
  -> Project + InstallationProject
  -> WorkOrder(s), optionally scoped to ProjectTask
  -> staff-verified implementation evidence
  -> ServiceOrder release
  -> successful provisioning result
  -> active Subscription
  -> ready CX handoff
  -> staff CX acceptance
  -> fulfilled SalesOrder
  -> ongoing support / service history
```

The chain uses structural foreign keys. Metadata identifiers remain migration
provenance and compatibility evidence; they are not canonical joins for new
writes.

## Named owners

| Decision or fact | Owner |
| --- | --- |
| Verified provider receipt | `integration.inbox` |
| Party-first capture and source replay | `sales.capture` |
| Immutable origin | `sales.lead_lifecycle` |
| Exact Lead/Party to account conversion | `sales.account_conversion` |
| Pipeline and Quote | `sales.service` |
| Sales Order and financial status | `sales.orders` |
| Project and implementation-scope coordination | `sales.fulfillment` calling `operations.project_lifecycle` |
| Vendor execution and verification evidence | `operations.vendor_project_lifecycle` |
| Committed cross-owner consequence delivery | registered `SalesLifecycleProjectionHandler` adapter |
| Work-order command and ProjectTask binding | `operations.work_order_commands` |
| ServiceOrder transitions and provisioning consequence | `operations.service_order_lifecycle` |
| Subscription/access transition | `access.subscription_lifecycle` |
| CX readiness, attention, and acceptance evidence | `customer.experience_handoff` |
| Aggregate drift report | `customer.lifecycle_audit` |
| Idempotent projection repair | `sales.lifecycle_reconciliation` |

Routes, templates, webhooks, event handlers, jobs, and commands are adapters.
They authorize/verify input, call the owner, and translate transport-neutral
errors. They do not write lifecycle state directly. Domain services must not
depend on HTTP request/response or exception types.

## Configuration versus code contracts

Operational values are not embedded in orchestration code. The default sales
implementation type, project status, project priority, provisioning workflow,
and connector header names come from domain settings or version-pinned
connector configuration. Provider-specific payload mapping belongs to the
installed connector/edge adapter.

Stable protocol vocabulary remains checked-in code: enum states, legal
state-machine edges, typed event names, capability IDs, idempotency-key formats,
and policy versions. Those are reviewed contracts, not mutable operating
configuration. Changing one requires a migration/versioned contract and tests.

## Lifecycle gates

1. Capture never creates a Subscriber implicitly and never deduplicates a
   person by email, phone, name, or social handle. Exact provider-event replay
   is idempotent; different content under the same event identity is rejected.
2. Account conversion locks the Lead and requires its exact Party. It creates
   or attaches one Subscriber through the account and Party owners, activates
   the customer role, and leaves the subscriber role pending.
3. Every non-cancelled SalesOrder receives at most one structurally linked
   Project and InstallationProject. ProjectTask may own several WorkOrders;
   WorkOrder owns the foreign key.
4. A partially paid SalesOrder records the receipt but creates no Subscription
   or ServiceOrder. Full funding creates one pending Subscription and one
   idempotent ServiceOrder per service line.
5. Sales ServiceOrders remain `draft` until the vendor-project owner records an
   append-only staff verification event. After that fact commits, the registered
   lifecycle projection handler asks `sales.fulfillment` to complete the native
   Project and release linked ServiceOrders. Replay is idempotent and failure is
   retryable; the vendor owner never writes project or provisioning roots.
6. Billing cannot directly activate a sales-created pending Subscription.
   Only a successful provisioning result may transition the linked ServiceOrder
   to `active`; that transition asks the subscription owner to activate access.
7. Successful activation emits the committed service-order completion fact.
   The lifecycle projection handler asks the CX owner to create a handoff only
   when funding, implementation, provisioning, and Subscription evidence all
   agree. CX staff acceptance is separately actor/time/reason evidenced and
   fulfils the SalesOrder.
8. Support Tickets and ticket-origin WorkOrders stay attached to the same
   Subscriber/Party history but do not rewrite sales attribution.

## Failure and repair

Money, implementation verification, provisioning outcomes, and CX acceptance
are never inferred. A downstream failure retains already-authoritative facts
and is retried by projection/reconciliation.

Run the PII-free audit:

```bash
python -m scripts.migration.audit_customer_lifecycle
```

Preview repairable projection drift:

```bash
python -m scripts.migration.reconcile_sales_lifecycle
```

Apply only owner-backed repairs:

```bash
python -m scripts.migration.reconcile_sales_lifecycle --apply
```

The reconciler may create a missing implementation scope, release a ServiceOrder
from existing verification evidence, or recreate a missing ready CX handoff. It
cannot invent an interaction, Party binding, payment, verification event,
provisioning result, or acceptance.
