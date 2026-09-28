# Automation Center source of truth

Status: reusable rule builder and native script control-plane draft

Decision owner: Michael

## Scope

The Automation Center is the central authoring and lifecycle surface for
rules plus governed client and server script drafts. Existing assignment, alert, FUP, inbox, NAS,
provisioning, SLA, escalation, and routing rules remain managed by their
existing owners until a later, separately approved migration. That migration
must hand off each rule without leaving two active writers.

Custom fields remain explicitly out of scope as an authoring mechanism; they
may be read by a declared script target only when its owner permits it.

## Capability boundary

`automation.capability_registry` owns the answer to whether a module, trigger,
condition field, target, or action may be used by the Automation Center. The
registry is derived from canonical `DomainSOT` declarations. Every SOT domain
is visible in the module catalogue, but an undeclared domain has no executable
automation surface.

An administrator can create and activate rules from capabilities the code has
made available, but cannot change capability availability or invent a module
key, event payload field, database field, command owner, permission, or action
from the UI. Adding or restoring a capability is a reviewed code change in its
owning domain. The code change must be deployed before the capability becomes
available for rule activation.

Owner declarations also publish a typed business-automation catalogue. The
central page shows whether each listed Support, communications, billing,
subscription, usage/access, provisioning, customer-identity, network, sales,
field-operations, integration, reporting, export, or maintenance item is ready
for new rules, still managed on its existing page, unavailable because it has
no safe Center contract yet, or retired. Unavailable and retired items include
a plain-language reason. An item is ready only when its declared trigger and
actions have registered runtime support. The catalogue is read-only: it does
not activate rules or change existing automation.

Every trigger declares the exact payload fields carrying tenant and target
identity. Events without both identities cannot be registered for automation;
the runtime never guesses tenancy from an unrelated record or a UI session.
Customer-specific rules may also name an explicit set of customer identities.
Those identities come from the trigger's declared customer field and are
validated against the customer owner when a draft is saved and published.

## Rule shape

The first contract is deliberately bounded:

1. one versioned domain event trigger;
2. a typed conjunction of declared conditions;
3. an ordered list of declared typed actions.

Loops, arbitrary scripts, arbitrary HTTP requests, delays, schedules, and a
general workflow DAG are not part of the first cut. Each can be introduced by
a later capability contract without weakening the closed registry.

Published rule versions are immutable. A change creates a new draft version
and publication atomically changes the active version. Runtime execution pins
the exact rule version, trigger schema version, action schema versions and
event identity used for the decision.

## Script shape

Client and server scripts are separate mechanisms in the same Center. A script
must name one registered target, one registered browser or server event, one
supported language, and an immutable source version. The source is hashed and
stored separately from the script identity. The target declaration, not the
editor, owns allowed events, read/write permissions, payload shape, and the
typed owner command boundary.

The initial language is JavaScript, but the application never evaluates it
in-process. Client scripts are delivered only to an approved browser form
adapter on declared module forms. The adapter fetches only the published
target/event bundle, verifies each immutable SHA-256 source hash, and exposes a
frozen field snapshot plus `get`, `set`, `error`, `clearError`, and
`preventDefault` helpers. Client scripts cannot fetch, access the DOM directly,
or issue database/API writes. Server scripts are submitted to the existing external OCI runner
boundary using a digest-pinned runtime image, read-only filesystem, dropped
capabilities, bounded memory/processes, and default-deny network. Missing or
ambiguous runtime configuration blocks publication. A server-script result is
not itself a database mutation: any requested activity must be mapped to a
declared typed owner command and retain script/version/event provenance.
The only admitted server-script side-effect result is an `actions` list whose
entries contain an `action_key` and typed `inputs`. The registry re-validates
the action, target module, required inputs, and value types before invoking the
same owner adapter used by a native rule; unknown actions and malformed output
fail the script run.

The event boundary stamps the single operator tenant on durable SQLAlchemy
events when a producer has not already supplied it. This keeps legacy event
producers usable as script triggers without inferring tenancy from a record or
from the browser session; an explicit producer value remains authoritative and
is checked by the runtime.

## Runtime boundary

The durable event dispatcher invokes one Automation Center handler. The
handler selects rules for the exact registered event type and independently
dispatches published server scripts by their declared target/event pair. It
evaluates only declared fields and delegates each rule action to the module's
typed command owner. It never performs generic ORM mutation.

Execution is idempotent by event ID, rule version and step position. A durable
run and step ledger records matched, skipped, succeeded, failed and blocked
outcomes. Step details retain a safe explanation and stable error code. The
admin run view shows the affected record, ordered steps, and retry history; it
does not display event payload values.

Planning, step claim, module action, and step completion are separate committed
boundaries. The module command receives the stable event/version/step key and
must implement the idempotency contract in its own source-of-truth service. A
crash after a side effect therefore retries the same module command identity;
the Automation Center never attempts to reverse or reconstruct another
module's mutation.

An administrator with `automation:run:redrive` may continue a failed run from
its first unfinished step. The command reads the exact durable event evidence,
pins the run's original immutable rule version, and skips every step already
recorded as succeeded. Pausing or editing the rule affects future events and
does not change this run. Each manual retry stores the administrator, start
time, final result, and any safe error explanation. Automatic event delivery
retries remain managed by the event dispatcher.

## Authorization

Authoring requires an Automation Center permission and every permission
declared by the selected trigger and actions. Publication repeats the complete
check; possessing a central publish permission does not grant authority over a
module.

Runtime uses an automation service principal constrained to the action's
declared runtime scope. It does not impersonate the publisher. Removing or
disabling a registered capability makes affected rules ineligible to execute.

Script publication repeats target permissions and runtime readiness checks.
The Center never grants a script arbitrary ORM access, imports, process access,
network access, dynamic code evaluation, or a generic database writer.

The admin shell is available at `/admin/automation`. Opening the hub requires
`automation:hub:read`; its rule and execution sections independently require
`automation:rule:read` and `automation:run:read`. Run details require
`automation:run:read`; continuing a failed run additionally requires
`automation:run:redrive`. The run detail shows the affected record, rule
version, timestamps, each action step and its attempts, safe failure guidance,
and administrator retry outcomes. A customer record link is shown when the
target type has a known admin destination.

The reusable builder admits runtime-ready targets from Support, Customer account
status, Sales Lead/Quote/Sales Order, Projects, Work Orders, Material Requests,
and Vendor Projects. Customer account rules use the reviewed status-action
protocol; Sales, Project, and Vendor actions delegate through typed owners;
material cancellation delegates to the existing ERP outbox consumer; and
work-order status changes stage through the native work-order owner without
committing independently. Each admitted action carries stable event,
rule-version, and step provenance. New capabilities are added through reviewed
domain declarations and typed runtime adapters; existing rules stay at their
current owners until a separate migration is approved.
Event schema 4 explicitly remains compatible with schema 3 rules because their
priority and customer conditions retain the same meaning.

Editing creates or replaces a draft version; activating it changes only future
event decisions. Pausing prevents new runs, while already claimed runs finish.

Before publishing, the rule owner reads active legacy Ticket assignment and
Ticket-creation automation rules, then checks other active Automation Center
rules for the same trigger and action. A possible overlap blocks publication
and identifies the existing rule. The overlap check only treats conditions as
disjoint when a shared field proves they cannot both match; uncertain overlaps
are blocked. Legacy rules remain in their existing pages and are not migrated.

Support ticket creation, service-team assignment, priority updates, customer
account status actions, sales status actions, project status, work-order status,
vendor-project status, and material cancellation are admitted to the runtime by
this reviewed code contract.
Focused checks for event delivery, customer scoping, replay, action audit,
activation, pause behavior, and both legacy and central rule conflicts run with
the pull request's CI suite before merge.

The current ticket-assignment and ticket-creation automation pages are listed
as existing ownership links. Their rules are not moved by this implementation
slice. The live rule-by-rule check supplies current conflict evidence when an
Automation Center rule is activated. The first catalogue slice covers Support,
Team Inbox, Messaging, and service-level automation. The second adds billing,
subscriptions, usage/access, provisioning/activation, and customer-identity
inventory. Items without a safe Center contract remain unavailable with an
explanation; retired items require a reviewed developer change before becoming
available. The third adds network monitoring, sales fulfilment, field and ERP
workflows, integrations, reports and exports, and maintenance inventory,
including retired items with explanations. These inventory changes do not
move existing schedules or rules or alter their behavior.

## Legacy coexistence

Legacy automation surfaces may register descriptive ownership and conflict
scopes. They are displayed read-only and continue to be managed at their
current routes. A new rule cannot publish against an exclusive legacy conflict
scope. The hub therefore gives operators one inventory without creating two
writers for the same decision.

## Deployment

Schema changes are additive. Permissions are seeded as assignable and are not
granted broadly. Migration 626 adds tenant-isolated script identities,
immutable versions, and redacted run evidence. Publication and execution remain
fail-closed until the digest-pinned OCI runtime is configured. Published server
scripts are selected by target/event and dispatched through the external runner;
client scripts are only delivered by approved browser-form adapters. Deployment
creates no rules and produces no new business side effects by itself.
