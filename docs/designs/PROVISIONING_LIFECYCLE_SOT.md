# Provisioning Lifecycle Source of Truth

Status: implemented by migration `390_provisioning_lifecycle_sot`

## Decision

`app.services.provisioning_lifecycle` owns provisioning readiness decisions.
`app.services.service_order_lifecycle` remains the sole writer of terminal
`ServiceOrder.status` transitions to `active` or `failed`. A provisioning
workflow records technical execution facts; it does not decide that customer
service is ready or write terminal status.

The lifecycle is deliberately two phase:

1. A terminal `ProvisioningRun` event asks the owner to evaluate readiness.
2. The owner locks the exact service order and run, records normalized checks,
   and either blocks, fails, or requests activation.
3. A request transitions the pending subscription through the existing
   subscription lifecycle owner and emits `subscription.activated` with the
   exact `service_order_id` and `readiness_decision_id`.
4. Existing IP, RADIUS, and NAS activation projections execute. A projection
   failure fails event delivery and remains retryable.
5. Only after those projections succeed does the handler ask the readiness
   owner to confirm that exact service order. Confirmation records an
   `activated` decision and asks the service-order lifecycle owner to set the
   order active and emit `service_order.completed`.

Subscription-wide completion and direct admin activation are not fallback
paths. They are removed.

## Authoritative relationships

`ServiceOrder.project_id` is the native installation-project link.
`ServiceOrder.activation_project_task_id` is the exact project task whose
completion gates a new installation. Field work remains related through the
existing native contract:

```text
SalesOrderLine 1 ── 0..1 ServiceOrder
                          │
                          ├── 0..1 Project
                          │        │
                          │        └── 0..n ProjectTask
                          │                    │
                          └── activation task └── 0..n WorkOrder
```

One activation task may require zero visits (no field dispatch) or multiple
visits. If visits exist, every active work order must be terminal and at least
one must be completed. Work-order completion is evidence; it does not itself
complete a project task or activate service.

## Readiness facts and decisions

`ProvisioningReadinessDecision` is append-only and idempotent on
`CommandContext.command_id`. Each decision owns one row per named check in
`ProvisioningReadinessCheck`:

- `provisioning_run`: the exact run succeeded or failed;
- `project_binding`: a new install resolves to the subscriber's active project;
- `activation_task`: the bound project task is `done`;
- `field_work`: linked native work orders are complete, or no visit was needed;
- `ip_assignment`: an active assignment exists for the exact subscription.

Decision states are `blocked`, `activation_requested`, `activated`, and
`failed`. Facts are observations owned by their domain services. Only this
owner turns them into an activation consequence.

## Connectivity boundary

This change does not promote the shadow connectivity reconciler and does not
change IPAM, RADIUS, NAS, or ONT authority. The active IP assignment is consumed
as a readiness observation. The existing activation projections remain in
place, but failures now propagate to the event store instead of being logged
and treated as customer success. The connectivity cutover described in
`CONNECTIVITY_STATE_MACHINE.md` remains a separate guarded migration.

## Migration and repair

Migration 390 adds the native links and append-only evidence tables. It
backfills a project from matching native `sales_order_id`, then quote identity,
and binds the `power_splicing_activation` task only when the match is unique.
Ambiguous or missing links remain null and fail closed during readiness.

New sales-order service orders resolve and persist both links at creation.
Repeating that staging operation repairs previously missing unambiguous links.
Customer project and service-order pages read the latest persisted decision;
they do not reimplement readiness policy.

## Retired authority

The following old decision paths are removed in the same cutover:

- provisioning-completed events assigning `ServiceOrder.status = active`;
- provisioning-failed events assigning order status outside the owner;
- subscription activation completing every in-flight service order for a
  subscription;
- `ServiceOrders.update` activating a pending subscription from a raw status
  edit.

Legacy appointment and provisioning-task storage is not activation authority.
Its customer/field scheduling replacement remains a separate coherent domain
slice; it may not regain activation decision rights while it is retired.

## Verification

- `tests/test_provisioning_lifecycle.py` covers blocked readiness, two-phase
  activation, confirmation guards, and event replay.
- `tests/architecture/test_provisioning_lifecycle_sot.py` enforces one terminal
  order-state writer and thin event adapters.
- SOT manifest contract validation checks authority, transactions, errors,
  events, projections, migration state, and source references.
