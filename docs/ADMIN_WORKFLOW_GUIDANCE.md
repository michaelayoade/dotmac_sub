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

Future AI retrieval must call the typed knowledge projection with permission
keys loaded for the authenticated staff principal. That projection reuses this
registry, removes unauthorized guides and action steps, and publishes a source
watermark. It is not a separate knowledge base and contains no customer data.
The typed lexical retrieval query ranks only this filtered view, uses bounded
terms and results, and persists nothing. The Help Center now consumes the same
permission-filtered projection instead of duplicating its section, guide, and
action checks in the route.

When an Admin route or workflow changes, update its linked guidance contract in
the same pull request. `scripts/architecture/workflow_guidance_gate.py` is run
by the required **Workflow Guidance Gate** CI job and fails a PR that changes
`app/web/admin` without changing the guidance registry.

## Fiber evidence lists

The five fiber evidence routes share a bounded browsing workflow. Connectivity
coverage and point identity coverage initially show searchable, paged latest
source facts. Field verification projects evidence for the selected source page.
Use **Run complete audit** when complete counts, gate results, priority order,
or report digests are needed. Fiber trace pages all matching subscriptions,
and ONT identity reviews loads only the chosen queue. Batch rows preview three
decisions with a link to the complete decision search; opening Candidates runs
the complete assignment audit.

The Field verification map opens its shell while the complete source overlay
loads. Wait for the feature count and report hashes before inspecting it. If
the report fails to load, reload the page to retry. Browser filters change only
visible features; they do not change the complete report or its hashes.

Ticket Settings explains the editable infrastructure ticket-type and target-kind
policy, when Save and Cancel appear, and how to correct a retained draft after
a failed save. The support ticket guide describes creating a customerless
infrastructure work order from a ticket; the dispatch guide describes assigning
its technician after creation.

The Fiber Plant guide explains selecting a tab, searching and paging its bounded
inventory, and opening asset details. Imported topology evidence is disclosed
separately from human notes. The Core Devices guide explains that summary cards
count the full filtered inventory while the table shows one page. Fiber Change
Requests guidance covers the independent pending-request and map-proposal
queues. These guides describe navigation; the fiber and device owners remain
authoritative for inventory state and totals.

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

## Detected outages

Use `/admin/network/detected-outages` to review persisted, debounced classifier
incidents first. The incident table pages 50 rows at a time. The separate live
node-health and candidate view loads only when opened or scrolled into view;
its raw verdicts have not passed the incident debounce window. A failed live
load leaves the persisted incident list visible and offers a retry.

Diagnose opens one selected domain without repeating the fleet sweep. Customer
verdicts page 50 at a time; OLT branch inference is requested separately, one
PON per request. A monitoring fault calls for checking the monitoring path,
while Notify remains an authorized preview before a confirmed send.

## Network monitoring

Use `/admin/network/monitoring` for a current exception snapshot. Open NOC or
Alarms from the summary cards, review the bounded alarm preview, and run the
PON outage scan only when needed. Site, VPN, and bandwidth panels load on
request and can be retried independently. Open Devices for the paginated
inventory; older search links redirect there. Bulk controls appear only with
`monitoring:write`, while device-detail links require `network:device:read`.
The snapshot time marks the read, not the time of every underlying observation.

## Router health

Use `/admin/network/routers` to find routers that need attention. The Health
column shows the most urgent open router health finding (Critical, High,
Medium, Low), OK, or No data when Sub has neither a configuration snapshot nor a
runtime poll for the router, and the Health filter narrows the list to one
level. A Shadow label means findings are visible but do not notify yet. A
router's detail page has a Health section with each finding's title, impact,
evidence, fix, known issue and first/last seen, plus the last snapshot and
runtime poll times. `/admin/drift` shows a read-only Router posture summary:
counts for everyone with drift access, finding detail only with `router:read`
because evidence contains management addresses. The reconciler never changes a
router, never uses `Router.status`, and writes no drift findings.
