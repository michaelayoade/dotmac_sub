# Admin workflow guidance

`app.services.admin_workflow_guidance` is the single source for plain-language
Admin help. It explains a workflow but never decides permissions, state, or
financial/service consequences; the route and named domain owner remain
authoritative.

Every staff-facing workflow guide declares its route selector, audience,
purpose, permission-linked actions, ordered instructions, and safety notes. The
Admin layout renders the matching guide's permitted action titles in a Page
Overview modal. `/admin/help` renders the searchable, Admin-sidebar-aligned
catalogue and deep-links directly to the selected page guide.

Help-only sidebar guides and contextual page guides are deliberately separate:
adding a sidebar destination to the Help Center does not add a question-mark
control to that destination.

When an Admin route or workflow changes, update its linked guidance contract in
the same pull request. `scripts/architecture/workflow_guidance_gate.py` is run
by the required **Workflow Guidance Gate** CI job and fails a PR that changes
`app/web/admin` without changing the guidance registry.

## Churn analysis

Use `/admin/reports/churn` to review subscriber churn within a defined calendar
window. Start with Last 1 month or Last 3 months, or choose Custom range and
provide both dates. Event type narrows the report to cancellations,
suspensions, or both. The KPI totals, monthly trend, recent-event list, reasons,
and CSV export all use the selected scope; do not compare an exported cohort
with an unfiltered page. Historical rows without trusted lifecycle evidence are
shown through the compatibility fallback and should be backfilled before being
treated as a complete historical event record.

## Automation Center authoring

Use `/admin/automation` as the starting point. Choose the mechanism before
authoring: a rule combines a registered event with typed owner actions; a
client script is bound to a declared browser form event; a server script is
bound to a declared durable event and is eligible for publication only when
the isolated, digest-pinned runtime and typed owner adapter are ready.

Published client scripts run only on forms marked for their registered target.
They receive a frozen field snapshot and the small `api` surface documented by
the Center; they execute in an opaque-origin browser sandbox and cannot call the
database, arbitrary HTTP APIs, or the parent DOM. Never place secrets in client
source because published source is delivered to the browser.

Open a script to inspect its active source and hash. Editing creates or replaces
one unpublished draft version, so the current behavior remains active until a
reviewer publishes the draft. Pause a published script when execution must stop
immediately; resume only after the behavior is understood, and retire scripts
that must never run again.

Server scripts are observational unless they return the typed `actions`
result contract: an array of registered `action_key` values with their declared
inputs. The Center validates that result against the selected module and then
calls the same typed owner action as a native rule. A script cannot invent an
action or write the database directly.

The module registry is authoritative. A module may be scriptable before it is
rule-executable, so a draft being selectable does not mean it can be
published. Never use a script to write the database directly or to bypass the
module's owner command. Save drafts, review permissions and runtime status,
then publish only after the resulting run evidence and rollback/reconciliation
path are understood.
