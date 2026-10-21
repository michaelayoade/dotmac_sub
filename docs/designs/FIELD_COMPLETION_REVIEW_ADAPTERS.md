# Completion review staff and field adapters

`operations.field_completion` owns explicit native or engineering work-order enrollment and scoped policy, immutable field
observations, criterion decisions, withdrawal, freshness, and retained evidence.
The staff API and authenticated admin pages are thin typed adapters. Publishing an explicit project or exact-work-order policy records the reviewer independence choice. Enrolling each exact work order is separate. Native work orders use operator-authored immutable criterion revisions; engineering enrollment names a real migration-676 issuance and copies its immutable issued criteria. Both reviewer independence choices are supported. No adapter changes execution status, assignment, ticket
or project closure, provisioning, or payment.

Staff READ exposes protected current and immutable history. WRITE-purpose
workspaces expose only exact current review inputs and readiness, without an
implicit READ grant. Protected retained-evidence downloads are scoped to the
immutable submission: READ permits history; WRITE permits the current pending
submission only. Bytes remain private and the owner validates their checksum
and size. The admin review form shows exact enrolled criterion prose and requires
an explicit verdict and reason for each criterion. Withdrawal uses the owner's
historical lineage and remains independent of current engineering approval.

`ui.field_completion_review_projection` composes owner snapshots and readiness
into registered ActionForms and FormContracts without ORM or policy decisions.
CSRF is checked before admin commands. All API mutations require the canonical
conditional browser CSRF header for cookie authentication; authenticated bearer
mobile requests remain exempt. Withdrawal labels its historical snapshot as a
recorded decision, never proof of current engineering acceptance. Original intent UUID, generation,
enrollment, policy, submission and criterion values survive refusals. Stale forms
are disabled until an explicit reload starts a new intent. Successful responses
label receipts as recorded historical facts and show current review separately.

GET `/api/v1/field/jobs/{public_work_order_id}/completion-review` returns exact
immutable enrolled criteria, criteria revision, completed-execution event, policy and assignment tokens, owner submission
readiness and the last recorded staff review. This recorded state does not
prove engineering-current acceptance; the staff current view owns freshness.
Unenrolled native jobs return a successful blocked readiness projection naming the enrollment prerequisite; no engineering issuance is fabricated or required for native review. Enrollment and policy/criteria editing are usable through WRITE-purpose staff pages without an implicit READ floor. POST at the same
path plus `/submissions` records observations using authenticated field identity,
a stable intent UUID and the original preview tokens. The owner correlates the
route's public work-order identity before receipt replay. No caller-supplied
admission or supervisor verdict is accepted. Mobile retains original intent
and observations across refusals and requires explicit reload.

`resolve_current_for_completion_review` is the engineering owner's narrow
WRITE-purpose freshness query. It hardwires completion-review WRITE admission,
uses the shared exact approval freshness implementation, and does not invoke a
command executor or impose an engineering READ floor.

Validation: typed fast adapter and architecture tests cover owner delegation,
transport identity, refusal/readiness serialization, CSRF, explicit verdicts,
and stale-input preservation. PostgreSQL migration/concurrency and hosted
browser/mobile acceptance remain the coordinator's dedicated-test-server gates.

Native setup uses `/admin/dispatch/work-orders/{public_id}/completion-review/policy`,
`/enroll`, and `/criteria`. These registered forms render owner readiness, the
exact policy scope, and individually labelled requirement/verification rows.
Selecting a row count explicitly starts a fresh form; native authoring supplies
no measurements or pass thresholds. Native criteria publication uses the
owner's separate criteria generation token, not the review-chain generation.
API equivalents are `/api/v1/completion-review/work-orders/{public_id}/policy`,
`/enrollment`, and `/enrollments/{enrollment_id}/criteria`; each mutation retains
the canonical conditional browser CSRF dependency. Review/history/retained
evidence API routes use `/enrollments/{enrollment_id}`. The owner correlates
public work-order routes before receipt replay; transport identity grants no
additional review authority.

Field encrypted drafts version 2 retain enrollment, immutable criteria revision,
completed-event identity and all original assignment/policy/review tokens.
Restoration neither submits nor replaces them with current values. Older
issuance-shaped drafts remain encrypted and blocked until the user explicitly
discards and reloads. Vendor location capture remains disabled, and maps remain
limited to existing authorized job destinations.
