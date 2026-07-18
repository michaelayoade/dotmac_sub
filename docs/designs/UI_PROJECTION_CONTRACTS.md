# UI Projection Contracts

**Owner:** `app/services/ui_contracts.py` (State, KPI, Action) + `app/services/list_query.py` (List)
**Status:** Foundation — additive; portals adopt incrementally
**Implements the shapes named in:** `docs/UI_INFORMATION_AND_ACTION_STANDARD.md`

## Why

The portal review found one recurring drift across the migrated modules: the
presentation layer re-derives business meaning — totals summed in Jinja over a
page, action eligibility decided from a status string, an unknown value rendered
as zero, a KPI that doesn't link to the cohort it counts. Each surface solved it
its own way (or not at all).

These contracts give every read/context owner one standard shape to return and
every template one standard shape to render, so a fix made once is the same
everywhere and the presentation layer can only *project* what the owner decided.

## The four contracts

| Contract | Lives in | Answers |
| --- | --- | --- |
| **List** | `list_query.ListDefinition` / `ListQuery` | filters, sorting, pagination, counts, and declared capabilities — URL-serializable so refresh, deep links, and back/forward reproduce the view |
| **State** | `ui_contracts.StateValue` | is this value present, stale, unknown, unavailable, or not-applicable — so unknown never renders as zero |
| **KPI** | `ui_contracts.Kpi` | a headline number, its state + freshness, its semantic tone, and the exact cohort URL that produced it |
| **Action** | `ui_contracts.Action` | is this action allowed (and why not), which permission it needs, and — for destructive/financial actions — its preview URL and impact count |

The **List** contract already existed and is unchanged; this work adds the other
three, which had no standard shape.

### State — `StateValue`

`present` / `stale` carry a value (and `as_of` freshness); `unknown` /
`unavailable` / `not_applicable` carry none and expose a `placeholder`. A
template checks `is_present` before formatting `value`. This is the standard's
"unknown ≠ zero, stale ≠ unavailable, disabled ≠ not-applicable" made concrete.

### KPI — `Kpi`

`value` is a `StateValue` (an unresolvable KPI shows "Unavailable", not `0`).
`cohort_url` is the required drill-down to the **exact filtered cohort** that produced the
number — supplied by the owner so a headline total and its list can never
diverge (the KPI-parity rule). `tone`/`icon` reuse the canonical `StatusTone` /
`StatusIcon`, giving a non-colour-only signal.

### Action — `Action`

`allowed` + `reason` come from the owning transition service, never a status
string in the template. `permission` is the granular RBAC key the route
enforces (the UI hides what the principal can't do; the route still authorizes).
`requires_confirmation` + `preview_url` + `affected` mark destructive/financial
actions that must preview impact and confirm before running. Confirmation policy
is deliberately separate from semantic `tone`: styling cannot weaken a safety
control.

The dataclasses reject contradictory shapes at construction time: absent state
cannot carry a value/freshness, every KPI has an application-relative cohort
URL, blocked actions explain why, negative impact counts are invalid, and a
confirmation requirement cannot exist without its preview URL.

## Adoption

These describe shapes the remediation slices already produced by hand; adopting
the contracts standardizes them:

- The customer billing KPIs (`get_billing_page`) become `Kpi` objects: the
  Outstanding/Overdue values as `StateValue` (unavailable, not zero, when the
  owner can't be reached), the Overdue KPI's `cohort_url` pointing at
  `/portal/billing?status=overdue`.
- The reseller account `status_actions` (restore/deactivate/disable, with
  affected counts) become `Action` objects.
- The dashboard balance and service-access become `StateValue` /
  a small state projection instead of ad-hoc `*_available` booleans.
- Every list migrated to `list_query` already satisfies the List contract; its
  KPI tiles become `Kpi` objects whose `cohort_url` is the same list filtered.

Adoption is incremental and per-surface — no big-bang rewrite. New surfaces
should return these shapes from the start.

## Enforcement

The template-business-arithmetic guard
(`tests/architecture/test_template_projection_boundary.py`) already bans the
worst re-derivation (summing totals in Jinja). As surfaces adopt `Kpi`/`Action`,
their `StateValue`/eligibility logic moves into the owner, which is the positive
form of the same rule. The contracts are plain frozen dataclasses with unit
coverage in `tests/test_ui_contracts.py`; they add no runtime coupling until a
surface chooses to return them.
