# Admin Overview — Page Contract

Status: draft contract for the incremental migration of the admin dashboard
(`/admin/dashboard`) onto `docs/UI_INFORMATION_AND_ACTION_STANDARD.md`.

Governs the ISP operations overview. The backend owners decide truth, state
meaning, eligibility, and freshness; this contract decides relevance, ordering,
disclosure, and interaction. The page composes owners — it is not itself a
source of truth.

## Screen identity

- **Screen id / page type:** `admin.overview` / Dashboard.
- **Route:** `GET /admin/dashboard` (+ HTMX partials `/dashboard/stats`,
  `/dashboard/activity`, `/dashboard/server-health`).
- **Audience / job:** ISP operations staff (support, NOC, finance, admin)
  landing to answer **"what needs attention right now?"** before "what happened
  over time?"
- **Decision supported:** where to direct attention next — which exceptions
  (financial, network, service) to act on, and where to drill in.
- **Primary entity:** the deployment (single-ISP instance). No tenant scope;
  RBAC governs which cards are visible, not the numbers inside them.

## Ownership map (authoritative read owner for every displayed number)

No number on this page may be produced by a query or threshold in
`web_admin_dashboard.py` or a template. Target owners:

| Widget | Authoritative owner | Status today |
| --- | --- | --- |
| Subscribers (total/active/new) | `subscriber.subscribers.get_dashboard_stats` | ✅ owner |
| Network devices (online/total, alarms, uptime) | `network_monitoring.network_devices.get_dashboard_stats` | ✅ owner |
| Revenue this period | `billing.reporting.get_dashboard_stats` (`stats.total_revenue`) | ❌ inline `sa_text` SUM over payments |
| AR aging (1–30 / 31–60 / 61–90 / 90+) | `billing.reporting.get_ar_aging_buckets` | ❌ inline `sa_text` over invoices; 90+ via collectibility |
| Overdue receivables | `invoice_collectibility` helpers | ✅ owner |
| Online customers / active sessions | `network.radius_sessions` (**add** `online_summary`) | ❌ raw `RadiusActiveSession` query |
| Current bandwidth | `network_monitoring` (**add** `bandwidth_summary`) | ❌ inline SUM over `device_metrics` |
| ONT service (online/total, low-signal) | `network_monitoring` ONT read owner | ❌ raw query + inline signal threshold |
| PON interfaces (up/total, PON outages) | `network_monitoring` PON read owner | ❌ raw ILIKE classification |
| OLT/ONT inventory + network-health ring | `network_monitoring` | ❌ raw counts + inline warn/crit % |
| Unconfigured ONTs | ONT/autofind owner | ❌ raw `OltAutofindCandidate` count |
| Pending service orders | provisioning/order owner | ❌ raw `ServiceOrder` FILTER counts |
| Sync status (Splynx) | integration/sync owner | ❌ raw `SplynxIdMapping` + inline age threshold |
| Host health (RAM/disk/load/uptime) + status | `system_health` (+ `DomainSetting` thresholds) | ✅ owner, settings-driven |
| Infrastructure services / workers | `infrastructure_health` / `web_system_health` | ✅ owner (but status vocabulary duplicated in template) |
| "Needs attention" items (severity + reason) | **owner-provided** exception feed | ❌ severities/thresholds inline in service AND template |
| What's new | `admin_whats_new_service` | ✅ owner |
| Recent activity | `audit_adapter` | ✅ owner (two competing builders today) |

The composed result is one typed **overview snapshot**. Status meaning
(`status`, `reason`, `tone`) is owner-provided; templates map `tone → color`
only (reuse the `status_presentation` pattern). Colors never encode domain, and
never communicate status alone.

## First viewport & information depth

1. **Attention queue (glance):** owner-provided exceptions, ordered by severity
   then recency, each with subject, state, reason, freshness, and the next valid
   action / drill-down. Exceptions precede all aggregate totals.
2. **4–6 decision-bearing KPIs (glance):** each links to the exact filtered
   cohort that produced it. Candidate set (final choice is an ops decision):
   Subscribers, Online customers, Network health, Overdue AR, Revenue (period),
   Alarms. Everything else moves to its module page or a column chooser.
3. **One primary work surface (work):** the highest-priority queue (e.g. today's
   actionable exceptions or overdue accounts), not a chart.
4. Host/infrastructure state and recent activity are **investigation** depth,
   below the fold / lazy-loaded.

Removed from the overview per the Relevance Test: the module-directory
Quick-Actions grid and the Customers/Network/Finance navigation cards (they are
navigation, not decisions); zero-value vanity tiles; any KPI without a cohort
link; discarded chart data.

## Actions

- **Exactly one page-level primary action:** "Add Customer".
- Attention items and KPIs expose at most one row action (drill to cohort);
  additional actions go to the module page.
- Worker restart (`/dashboard/workers/restart`) stays gated by
  `system:settings:write`, audited through the canonical audit owner, with a
  non-optimistic result.

## Status, provenance, freshness

- Surface the snapshot's real `refreshed_at`, not `datetime.now()`. Header reads
  "Data as of <refreshed_at>"; a stale snapshot is labelled stale, not fresh.
- Keep the never-interchangeable distinctions: unknown ≠ zero ≠ N/A; stale ≠
  unavailable ≠ failed; device reachability ≠ customer impact; invoice state ≠
  payment state. Unknown/stale values must not silently render as 0.

## States

Loading (skeleton preserving layout), empty ("nothing needs attention" is a
valid, positive state), partial (a widget's owner unavailable → that widget
shows stale/unavailable, page still renders), error, unauthorized (card hidden,
never a broken number).

## RBAC

Read gate: `require_any_permission("billing:invoice:read", "monitoring:read",
"customer:read")`. Per-widget visibility from granular perms
(`show_financials` / `show_network` / `show_subscribers`). Visibility hides
cards; it does not change the numbers (single-instance model).

## Migration plan

Incremental per the standard's "Migration of existing screens":

- **Contract and gap baseline.** Record the current contract and gaps. ✅
- **Core dashboard read ownership.** Route Revenue, AR aging, Online
  customers, and Bandwidth through read owners (add `radius_sessions.online_
  summary` and `network_monitoring.bandwidth_summary`); surface real
  `refreshed_at`. Delete the dead `_build_dashboard_stats_summary` path and the
  duplicate recent-activity builder. No visual change beyond correct freshness.
- **Owner-provided status semantics.** Move attention-item severities, key-section
  health, and the network-health ring into owner-provided `status/reason/tone`;
  templates map tone→color only; de-duplicate the infra status vocabulary.
- **Remaining raw-read retirement.** ONT/PON/OLT/unconfigured/pending-orders/
  sync move behind network and provisioning/integration owners.
- **Overview UX contract.** Cut to 4–6 KPIs, lead with the attention queue +
  one work surface, remove the module-directory grid, wire KPI→cohort links.
- **Composition guardrail.** Architecture test failing if `web_admin_dashboard`
  issues ORM aggregates or derives domain status/eligibility; contract/browser
  tests for KPI-cohort parity and empty/stale/partial/unauthorized states.

## Tests required (enforcement §)

Action eligibility at the owner boundary; KPI-to-filtered-cohort parity; the
unknown/stale/unavailable/empty/partial/error/unauthorized states relevant to
the overview; desktop + mobile first-viewport usefulness and action hierarchy.
