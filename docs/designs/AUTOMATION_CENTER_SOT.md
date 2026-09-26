# Automation Center source of truth

Status: pilot runtime foundation

Decision owner: Michael

## Scope

The Automation Center is the central authoring and lifecycle surface for
Automation Center rules. Existing assignment, alert, FUP, inbox, NAS,
provisioning, SLA, escalation, and routing rules remain managed by their
existing owners until a later, separately approved migration. That migration
must hand off each rule without leaving two active writers.

Custom fields are explicitly out of scope.

## Capability boundary

`automation.capability_registry` owns the answer to whether a module, trigger,
condition field, target, or action may be used by the Automation Center. The
registry is derived from canonical `DomainSOT` declarations. Every SOT domain
is visible in the module catalogue, but an undeclared domain has no executable
automation surface.

An administrator may enable or disable a declared capability but cannot invent
a module key, event payload field, database field, command owner, permission,
or action from the UI. Adding a capability is a reviewed code change in its
owning domain.

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

## Runtime boundary

The durable event dispatcher invokes one Automation Center handler. The
handler selects rules for the exact registered event type, evaluates only
declared fields, and delegates each action to the module's typed command owner.
It never performs generic ORM mutation.

Execution is idempotent by event ID, rule version and step position. A durable
run and step ledger records matched, skipped, succeeded, failed and blocked
outcomes. Retry does not repeat completed steps. Sensitive event and action
values are not copied into the ledger.

Planning, step claim, module action, and step completion are separate committed
boundaries. The module command receives the stable event/version/step key and
must implement the idempotency contract in its own source-of-truth service. A
crash after a side effect therefore retries the same module command identity;
the Automation Center never attempts to reverse or reconstruct another
module's mutation.

## Authorization

Authoring requires an Automation Center permission and every permission
declared by the selected trigger and actions. Publication repeats the complete
check; possessing a central publish permission does not grant authority over a
module.

Runtime uses an automation service principal constrained to the action's
declared runtime scope. It does not impersonate the publisher. Removing or
disabling a registered capability makes affected rules ineligible to execute.

The admin shell is available at `/admin/automation`. Opening the hub requires
`automation:hub:read`; its rule and execution sections independently require
`automation:rule:read` and `automation:run:read`. The initial shell is
read-only. It exposes registry readiness, central definitions, run evidence,
and legacy ownership links without implying that rule authoring is available
before a complete module adapter exists.

The first admitted UI slice is Support Ticket Assignment management. An
authorized administrator can create a draft for a newly created urgent support
ticket, choose either all customers or a selected customer set, and assign an
existing active Service Team. Editing creates or replaces a draft version;
activating it changes only future event decisions. Pausing prevents new runs,
while already claimed runs finish. The Ticket owner stages the dedicated
`support.ticket.created` event with the operator tenant, Ticket identity,
priority, and canonical customer identity; the action delegates through the
typed Ticket lifecycle command with stable event, rule-version, and step
provenance.

Before publishing, the rule owner reads active legacy Ticket assignment and
Ticket-creation automation rules. A rule that can also assign an urgent Ticket
blocks publication and identifies the legacy rule. The check is deliberately
conservative where the Automation Center rule has no condition that proves a
legacy region, type, source, or tag rule cannot overlap.

The runtime capability remains gated until focused acceptance checks cover
event delivery, customer scoping, replay, assignment audit, rule activation,
pause behavior, and active legacy-rule conflicts.

The current ticket-assignment and ticket-creation automation pages are listed
as legacy ownership links. Their rules are not moved by this implementation
slice. The live rule-by-rule check supplies current conflict evidence when an
Automation Center rule is activated.

## Legacy coexistence

Legacy automation surfaces may register descriptive ownership and conflict
scopes. They are displayed read-only and continue to be managed at their
current routes. A new rule cannot publish against an exclusive legacy conflict
scope. The hub therefore gives operators one inventory without creating two
writers for the same decision.

## Deployment

Schema changes are additive. Permissions are seeded as assignable and are not
granted broadly. The runtime handler ships disabled until at least one module
adapter is reviewed and its capability is enabled. Deployment creates no rules
and produces no new business side effects by itself.
