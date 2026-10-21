# Customer assignment updates

`operations.work_order_commands.assign` owns assignment and schedule decisions.
Its typed locked preview supplies customer identity, technician/vendor identity,
visit lineage, and the effective schedule to
`communications.work_order_assignment_updates`. The participant stages a
`CommunicationIntent` before the assignment transaction commits. Intent failure
rolls back the assignment; no channel transport runs within this transaction.

A first target produces `work_order_assigned`; a different target produces
`work_order_reassigned`; a changed schedule with the same target produces
`work_order_schedule_changed`. The stable assignment command UUID dedupes the
intent. Assignment receipts replay without staging another intent. Equivalent
new commands with no target or schedule change stage no customer update.
Shared infrastructure visits without a subscriber have no customer audience.
Channel/recipient selection and suppression remain with communications.intents.

This slice covers schedule changes submitted through the typed assignment
command. The legacy header update boundary is unchanged and does not acquire a
parallel communication policy. Migrating that boundary remains required before
adding its schedule notifications. Explicit assignment commands rotate a native UUID generation even for the same
technician. `operation=reschedule` preserves it and requires the current target.
Migration `671_assignment_generation` adds nullable provenance columns without
backfilling old evidence. Existing NULL-generation assignments retain legacy
behavior until an explicit assignment; generated assignments reject missing or
mismatched transition and location UUIDs. Device capture timestamps never replace
generation authority. Job detail/list reads expose the generation and mobile
queues freeze it at capture time, including encrypted offline persistence.

Customer tracking requires an actual `en_route` event for the current assignment,
a job-tagged fix from that generation, enabled sharing, and a fix no more than two
minutes old. Terminal jobs, another subscriber, another job, and superseded
assignment evidence cannot expose coordinates. Work-order locking serializes
assignment mutation against tagged ingest and authorized tracking reads. The narrow tracking service owns read policy.
Customer lifecycle actions use this departure policy. Chat actions use the chat
owner's actual conversation availability, retaining closed conversation history
with the label “View conversation”. No status-only chat grant is introduced.

Migration acceptance requires coordinator-run fresh and predecessor PostgreSQL
checks on the dedicated test server. Cached mobile summaries from older app
versions may omit generation; their generated-assignment writes fail closed until
a fresh job read supplies it. Queued observations are never restamped.

Technician feedback uses the typed `customer.work_order_selfcare` command. An
subscriber-before-work-order lock order protects subscriber ownership, first-score replay and
new-rating eligibility; feedback, audit and event commit together. Only a completed
visit with an active native technician assignment and actual bound active staff user permits new technician feedback.
Vendor organizations and arbitrary legacy header names never supply a technician
name or phone. Reviewed portraits and verified badges remain identity-owner policy.

New feedback freezes native technician, assignment queue and generation provenance.
`customer.technician_satisfaction` consumes that provenance, never latest assignment.
Historical scores without native attribution remain visible in owned customer
history but are excluded from per-technician scorecards. No identity backfill is
inferred. Replay returns the first score only after checking current subscriber
ownership, and produces no additional audit or event.

Reseller rating commands carry the authenticated representative's reseller UUID,
principal UUID and principal kind. Inside the owner transaction, the subscriber
lock protects the account mapping, then shared locks protect the active reseller
and current active representative binding before the work-order lock. A transferred
account or revoked binding is denied before both rating creation and historical
replay. Adapter prechecks never replace that transactional authorization.


## Supervisor vendor assignment review

The assignment page serves dispatch supervisors deciding who will attend a
native work order. `operations.work_order_commands` supplies the current job
revision, generation, eligible native vendor members, and before/after preview.
The first view identifies the work order and vendor; its primary action selects
the attending person. The next review shows that person's name, company, schedule,
and assignment reason before explicit confirmation. Internal identifiers remain
hidden confirmation fields rather than competing with the operational decision.

The form retains the original command UUID, reason, person, revision and
generation after rejection. A changed assignment requires an explicit reload and
fresh review; it never silently retries a stale confirmation. Empty eligible
rosters explain that an active linked Field user is required and offer return to
the work order. Permission admission comes from the existing canonical dispatch
gate, and customer disclosure still requires independently reviewed identity.
No vendor owner or arbitrary company member receives job execution access.
