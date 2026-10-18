# ADR 0032: Owner state machines for money and access lifecycles

Status: accepted

Date: 2026-10-18

Accepted: 2026-10-18 by Michael

Decision owner: Michael / Dotmac architecture

Affected systems and domains: Starter `dotmac-billing` (money lifecycles),
Starter access policy and collections, Sub thin-client event consumers,
support "why" view and manual exceptions queue

## Context

The thin-client cutover (ADR 0007 destination, ADR 0031 policies) removes
the multiplicity of Sub writers, but multiplicity is only half of the
failure class seen in production on 2026-10-18: a payment discharged a
July draft instead of renewing, and a service pause never lifted. Those
failures came from lifecycle behaviour that exists only as emergent
choreography — several pieces of code each switching service on and off,
policies buried in code, and no single place able to answer "what state
is this in, and which step is outstanding?". Moving writers to Starter
while carrying the same implicit behaviour would relocate the bugs
behind a cleaner API.

The industry review of the simplified plan drew the same line: core
money and access lifecycles must not be free-form rules. Michael
confirmed the layered design on 2026-10-18.

## Decision

1. **Three layers, by risk.**
   - *Core lifecycles (money and access)* run as explicit state machines,
     one per lifecycle, each owned by exactly one Starter module.
     Examples: prepaid renewal, dunning ladder, SLA compensation hold,
     vacation hold, fair-usage throttle.
   - *Optional reactions (low risk)* run on a slimmed, staff-composed
     rules engine in the UI. Examples: ticket routing and priority,
     notifications and reminders, inbox routing.
   - *Shared by both*: kernel registry, outbox, approvals, audit, and
     the "why" view.

2. **State machines are owner code, not a general engine.** Transitions
   are implemented and tested in the owning module and versioned with
   the definition. "Persisted as data" means data-defined policies and
   visible states — every lifecycle instance exposes its current state,
   its deadline, and its reason as a query — not an embeddable workflow
   engine with free-form graphs.

3. **Structural obligation identity (ADR 0007).** A settlement
   allocation only accepts a confirmation bound to that obligation's
   intent. A payment therefore cannot drift onto an unrelated open
   draft; the incident class is unrepresentable, not patched.

4. **One writer for service on/off.** Billing emits facts
   (`obligation.discharged`); it never touches service state. The access
   module is the single access-decision owner and the only writer of
   service state. Sub submits intents and renders state; it runs no
   transitions.

5. **Failure is a state.** Every waiting state carries a deadline from
   versioned policy settings (ADR 0031 pattern). Expiry moves the
   instance to an explicit `attention-required` state with the reason
   attached; that state *is* the manual exceptions queue entry. The
   queue has a screen, an owner (billing staff in business hours), and
   age alerts, so nothing waits silently. A periodic recovery sweep
   re-drives instances whose expected event never arrived.

6. **In-flight safety.** A lifecycle instance keeps the policy version
   it started under; changing a setting shapes new instances only and
   never rewrites history mid-flight.

7. **No distributed transactions.** Cross-module steps travel over the
   kernel outbox/inbox, at-least-once, with every handler idempotent on
   a stable key. Compensation is declared per transition (for example
   release the intent); partial discharge stays visibly partial and is
   never silently reallocated.

8. **SLA compensation is a lifecycle**, not a rule: `breached → held`
   (billing clock paused, access untouched) `→ released` when the
   ticket resolves `→ renewal extended`.

## Invariants

- Exactly one module may write each lifecycle's state; exactly one
  module (access) may write service on/off.
- Every lifecycle instance's state, deadline, and reason are queryable
  at all times; the why view and the exceptions queue are the same
  surface seen from two ends.
- Replay of any event is a no-op: handlers key on stable identities.
- A confirmation can discharge only the obligation its intent binds.

## Acceptance tests (incident-derived, at the Starter boundary)

1. A payment bound to one intent cannot settle another obligation.
2. A full settlement yields exactly one access signal and lifts the
   pause, even if replayed.
3. Nothing outside the access owner can switch service.

## Consequences

- Support diagnosis collapses from log archaeology to one state query.
- Rejected alternative: one general rules/workflow engine for
  everything — rejected because free-form composition reintroduces
  unowned behaviour for money and access, the exact failure class being
  retired.
- Rejected alternative: synchronous cross-module call chains —
  rejected because they couple availability and hide partial failure;
  the outbox pattern is already the kernel's contract.

## Migration and cutover

- Old owner and paths: emergent event choreography across Sub billing,
  enforcement, arrangements, and notification handlers; Sub stop-gaps
  remain until the Starter machines land.
- New owner and paths: Starter `dotmac-billing` (money lifecycles),
  Starter access policy (access decisions), Starter collections
  (dunning), built before Sub's billing cutover completes.
- Backfill/repair: none by this ADR; in-flight Sub cases drain or are
  exception-queued at their lane's gate.
- Shadow or verification phase: the three acceptance tests run at the
  Starter boundary per lifecycle, starting with prepaid renewal.
- Cutover gate and evidence: per-lifecycle gates under ADR 0007; this
  ADR moves no authority by itself.
- Fallback retirement: Sub-side service switching surfaces retire as
  each lifecycle's Starter machine lands, under the money-writer and
  thin-client ratchets.

## Verification

- The three acceptance tests above are executable at the Starter
  boundary for the prepaid renewal lifecycle first, then per lifecycle.
- Architecture guards keep Sub service-state writes attributable to the
  retiring legacy owners until their deletion, and forbid new ones.
- Recorded in Knowledge as `dotmac-lifecycle-workflow-design-2026-10-18`.
