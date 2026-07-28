# Provisioning surface — build spec (from owners)

The fifth network surface, built as a projection of the provisioning SOT owners
(per `NETWORK_SOT_SERVICE_MAP.md`), consistent with the four shipped surfaces
(fiber / NOC / access / IPAM).

## Correction to the earlier read

An earlier pass flagged provisioning as having "no clean list owner." That was
wrong — it missed `app/services/provisioning_managers.py`, which exposes **seven
CRUD list owners**. Provisioning is fully buildable from owners like the others;
no model is queried directly.

## Owners (source of truth)

Owner singletons in `provisioning_managers.py`, each a `CRUDManager` with
`.list(...)`:

| Entity | Owner singleton | Status enum | Presentation |
|---|---|---|---|
| `ServiceOrder` | `service_orders` | `ServiceOrderStatus` | ✅ `service_order_status_presentation` |
| `InstallAppointment` | `install_appointments` | `AppointmentStatus` | ✅ `appointment_status_presentation` |
| `ProvisioningTask` | `provisioning_tasks` | `TaskStatus` | ✅ `provisioning_task_status_presentation` |
| `ProvisioningRun` | `provisioning_runs` | `ProvisioningRunStatus` (pending/running/success/failed) | ❌ **add `provisioning_run_status_presentation`** |
| `ServiceStateTransition` | `service_state_transitions` | `ServiceState` (pending/installing/provisioning/active/suspended/canceled/disconnected) | history facet — audit trail |
| `ProvisioningWorkflow` | `provisioning_workflows` | — | definition/config, not queue |
| `ProvisioningStep` | `provisioning_steps` | `ProvisioningStepType` | definition/config, not queue |

`ServiceOrders.list` signature (representative):
`list(db, subscriber_id, subscription_id, status, order_by="created_at", order_dir="desc", limit=20, offset=0, account_id)`.

## Two status vocabularies — the key architectural point

Provisioning carries **two distinct status lenses**; keep them separate:

1. **Entity lifecycle statuses** (`ServiceOrderStatus`, `ProvisioningRunStatus`,
   `TaskStatus`, `AppointmentStatus`) — own the workflow *record* state. These
   already have presentations (except run). A **ledger** projects these.

2. **`ControlPlanePhase`** (`control_plane_intent`) — the canonical cross-vendor
   *convergence* projection: `desired → planned → queued → applying →
   readback_pending → verified → drifted → failed`. Owned by
   `control_plane_intent.phase_for_{network_operation,uisp_intent,huawei_sync,
   router_push,router_push_result}`. This is the "is it converging / drifted /
   failed" lens, projected from raw **vendor-operation** statuses, NOT from
   `ProvisioningRunStatus`. A **triage** view projects these.

**SOT rule:** never re-derive control-plane phase in the web layer. Project it
from `control_plane_intent`. `ProvisioningRun` has no `vendor` column (vendor
lives on its `ProvisioningWorkflow`/`ProvisioningStep`), so a run→phase
projection must dispatch by the run's workflow/step vendor — put that dispatch
in the owner as `phase_for_provisioning_run(...)` inside `control_plane_intent`,
never in the adapter. `control_plane_intent` also owns the transition contract
(`assert_phase_transition`, `assert_intent_head`); the UI reads phase, it never
asserts transitions.

## Recommended surface — phase 1: archetype-D ledger

`/admin/network/provisioning`, facets **Orders / Runs / Tasks / Appointments**.

- Each facet sourced from its `.list()` owner; default ordering `created_at
  desc`, and a sensible in-flight bias (non-terminal statuses surfaced first).
- Status column via the existing presentations + the new
  `provisioning_run_status_presentation`.
- Same `{facet, facets, columns, rows, __status, detail_base}` contract as
  fiber/access/IPAM — reuse the generic ledger template verbatim.
- New page-data service `app/services/web_network_provisioning_ledger.py`
  normalising the owner reads (mirror `web_network_access_ledger.py`).
- Route folded into the existing `app/web/admin/provisioning.py` router, gated
  on `require_permission("provisioning:read")` (the perm already exists).
- Read-only projection. Mutations stay on their owners:
  `web_provisioning_actions` / `web_provisioning_bulk_activate` /
  `web_provisioning_migration` → the provisioning managers. The ledger links to
  those; it does not re-implement them.

## Recommended fast-follow — phase 2: archetype-A convergence triage

"Provisioning in flight," worst-first by `ControlPlanePhase`
(`failed`/`drifted` → `applying` → `queued` → `verified`), exactly the NOC
pattern. Requires:

- `phase_for_provisioning_run(...)` added to `control_plane_intent` (owner-side
  dispatch by vendor).
- `control_plane_phase_presentation(ControlPlanePhase)` added to the
  presentation owner (8 phases; `failed`→negative, `drifted`→warning,
  `applying`/`queued`→info, `verified`→positive, etc.).
- A page-data service unioning active runs + open orders + pending tasks, each
  carrying its control-plane phase, reusing the NOC `_TONE_RANK` worst-first
  merge (`web_network_noc.py`).

## Presentations to add

- `provisioning_run_status_presentation(ProvisioningRunStatus)` — `pending`→info,
  `running`→info (clock), `success`→positive, `failed`→negative. (phase 1)
- `control_plane_phase_presentation(ControlPlanePhase)` — 8 phases. (phase 2)

Both go in `app/services/status_presentation.py` (the single presentation
owner), same as the fiber / FUP / IPv6-prefix tones already added on #1508.

## Build recipe (identical to the four shipped surfaces)

recon owners → page-data service normalising `.list()` into `{columns, rows}`
→ generic ledger template → route → test against an empty `db_session` → ruff →
commit.

## Open decisions for Michael

1. **Ledger-first (phase 1) then triage (phase 2), or triage-first?** Recommend
   ledger-first for parity with the shipped surfaces; the phase-2 triage is the
   higher-value operational view but needs the owner-side phase projector.
2. **Default facet + in-flight filter** — all rows, or non-terminal first?
3. Confirm `provisioning:read` is the right gate (it is used by existing
   provisioning routes).
