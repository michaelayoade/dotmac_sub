# Support resolution and enrolled completion

Support retains customer consent and ticket lifecycle authority. Completion review retains enrollment, evidence and staff acceptance authority. Only explicitly enrolled WorkOrders whose native `origin_ticket_id` equals the Ticket participate. ProjectTask ticket links do not establish this scope. Unenrolled tickets retain their existing lifecycle; previously closed tickets are not reopened.

Direct closed/resolved transitions refuse when any linked WorkOrder is enrolled and direct staff to request customer confirmation. A request requires every enrolled linked WorkOrder to be canonically completed and currently accepted. Neither field completion nor staff acceptance supplies customer consent or closes a Ticket automatically.

A native immutable resolution request pins the exact WorkOrder, enrollment, criteria revision, policy revision, assignment generation, completed event, submission, accepted decision and review generation for every linked enrollment. Its token and immutable child rows belong to Support. Customer confirmation and the grace consumer revalidate the entire current basis. Additional enrollment, changed evidence or policy, reassignment, revocation, stale engineering approval or missing acceptance blocks closure and requires a fresh request. The grace consumer records a blocked attempt and consumes its event receipt without closing or manufacturing consent.

The typed request command binds its stable intent to ticket, actor, reason, grace and exact completion basis. Exact retries return the original active capability and deadline; changed payload or basis conflicts. Consumed, replaced or expired requests cannot be resurrected. New native requests use their immutable request time and grace. Historical requests without a native record retain the legacy deadline behavior. Timers target the immutable request UUID and must match the canonical fired timer identity, generation, event and deadline; replaced requests and ticket-only historical events cannot target a new request.

All linkage mutations and lifecycle checks acquire the canonical WorkOrder linkage fence before Ticket locks. Multi-ticket lifecycle operations acquire ticket fences in UUID order first. Completion participants never lock Tickets or call Support. The basis participant batches Project roots, tasks and engineering locks before WorkOrders, policy and review heads. Enrollment also locks the exact native nonterminal Ticket under the fence, preventing enrollment after closure wins the race. Ticket issuance takes the same fence before the Ticket lock.

The legacy multi-ticket auto-confirmation sweep handles only unenrolled Tickets. Under each Ticket linkage fence it checks canonical enrollment presence and skips any enrolled Ticket before requesting completion basis or engineering locks. Enrolled grace closure runs exclusively through its immutable request-bound timer, with one Support transaction per Ticket. This avoids accumulating Project locks across multiple Tickets in an order that could conflict with a concurrent request. No enrolled sweep behavior existed before this slice.

Migration 678 is additive and follows 677. Empty pre-existing tables are adopted only after exact native shape validation. Native triggers reject updates/deletes, cross-lineage basis rows and late child insertion; deferred validation requires the full enrolled set. Downgrade is forward-fix only. No financial, provisioning, payroll or project-completion effects are added.

Fast SQLite tests verify typed boundaries and lifecycle behavior only. Migrated PostgreSQL acceptance sources cover both close/enrollment race directions, pinned-basis changes and native immutability. The coordinator owns dedicated-server execution, migration adoption checks and the final migration head/source guard pin.

## Supervisor request adapters

The Ticket detail action **Customer confirmation** opens the scoped supervisor
editor at `/admin/support/tickets/{ticket_id}/resolution-confirmation`. Staff
with `support:ticket:update` may preview and request confirmation without an
implicit engineering or Ticket READ grant. READ remains required for the link
back to the ordinary Ticket detail page. The equivalent typed API uses GET/POST
`/api/v1/support/tickets/{ticket_id}/resolution-confirmation`.

Support supplies readiness, Ticket business labels, the current request and
deadline. The editor renders its ActionReadiness and registered FormContract;
it does not derive completion eligibility. The shared request schema forbids
extra fields and binds the caller's stable intent UUID, reason and whole-hour
grace period to a server-produced staff admission and command context. Browser
mutations require CSRF, including cookie-authenticated API clients; bearer API
clients retain the canonical exception.

The owner returns only a typed request receipt, never a customer token or
confirmation URL. Success states that the request was recorded, not that a
notification was delivered or customer consent received. The customer uses the
existing Ticket portal or configured confirmation notification. Exact retry
returns the original currently active request/deadline. Refused submissions
retain original intent and values, display the owner refusal and require an
explicit readiness reload before a new intent; no command is run by GET.

Migration679 extends each original required completion basis entry with an
optional exact effective enrollment/work order and an ordered immutable list of
reviewed replacement decisions. It does not drop failed or canceled enrolled
scopes. Confirm and grace freshly resolve and compare the entire lineage plus
accepted endpoint, including every source/replacement assignment, policy,
criteria and failure basis. Revocation or drift blocks confirmation until a new
current acceptance and request exist. Customer dispute remains allowed for a
current valid request capability even if that historical completion basis is
stale: a customer challenge must still reopen the ticket.
