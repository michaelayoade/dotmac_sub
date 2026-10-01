# Payment email composition cutover

**Status: expand candidate; no customer send path has switched.** S10 records the
customer-facing symptom. This design covers the first bounded cutover:
`payment_received` and `invoice_paid` emails caused by the same proved payment.
It does not change either domain event or any non-email channel. Service
restoration notices need a separate correlation contract before they can join
an episode.

## Current evidence and ownership

- `app.services.events.handlers.notification.NotificationHandler` loads active
  `NotificationTemplate` rows by channel, checks conditions, builds context,
  renders subject/body and calls `communication_intents.submit`. The payment
  context derives a receipt number and authenticated receipt URL from the
  succeeded payment. The `payment_received` SMS has its own active template and
  body. Existing delivery tests require both receipt fields in email and SMS.
- A payment event carries `payment_id` and, for an allocated invoice,
  `invoice_id`. At base revision
  `b4b7502a85038ebdc60c3fd5ffb8921988b6a227`, the only source occurrence
  of `EventType.invoice_paid` was in `Invoices.update`, but that same method
  refuses a requested paid status before reaching the emitter. Actual settlement status is derived in
  `billing._common._recalculate_invoice_totals` and finalized through several
  payment and non-Payment application paths. The event is therefore not emitted
  by the normal settlement path at this revision. An invoice may also be paid
  by another mechanism. Subscriber plus time, or invoice ID alone, does not
  prove that two payment attempts are the same episode. A single Payment can
  fund multiple invoices. Some paths emit a receipt per allocation while
  others name only a primary invoice. The first cutover is limited to a Payment
  with exactly one proved invoice allocation; multi-invoice payments retain
  their existing per-event routes until a many-invoice contract is defined.
- `ont_online` has a notification spec and template, but no event producer was
  found under `app/` at the 2026-09-30 source revision
  `b4b7502a85038ebdc60c3fd5ffb8921988b6a227`. It cannot be a completion
  signal. A future service-notice cutover must prove its own producer and
  correlation identity.
- ADR-0006 § 5b assigns reusable content rendering to the published Starter
  `dotmac-template-studio` module. Sub owns which domain events share a payment
  episode and when a customer should be contacted. Existing consent, channel,
  intent and delivery owners remain in place.

## Required behavior

1. The billing settlement owner must first emit `invoice_paid` for an actual
   issued/overdue/partially-paid to paid transition caused by a proved Payment,
   exactly once in the same transaction as the status change. The event carries
   that originating `payment_id`. Carry the causal allocation and previous
   status through the settlement owner; neither can be reconstructed safely
   from a later invoice snapshot. Non-Payment and consolidated paths remain
   outside this first producer contract until they carry an exact provenance
   and customer-notice contract. Until the proved producer and its tests exist,
   there is no payment pair to consolidate.
2. The notification handler completes the normal per-channel template,
   condition, recipient and unresolved-variable checks before submitting an
   email part for composition. SMS and every other configured channel use the
   existing immediate submission path. The composed email contains the exact
   rendered receipt body, including number and authorized URL, and the exact
   rendered invoice-paid body if one was collected. The receipt supplies its
   subject when present. No generic payment sentence replaces a template.
3. The Sub communications owner records a durable payment-scoped window,
   deduplicated by source event ID and protected by a database uniqueness and
   locking rule. Its canonical execution boundary must retain a separate
   durable intent and dedupe identity for each source event while creating one
   physical notification per compatible recipient. An explicit association
   with foreign keys and uniqueness records which intents that delivery covers;
   the delivery-outcome owner projects its result to both. The existing single
   `Notification.communication_intent_id` and its outcome projector cannot
   represent this relationship, and an unchecked metadata list is insufficient.
   Closure holds the window row lock through intent, coverage and notification
   creation in one transaction, using a stable composed-delivery dedupe key.
   Two sweeps or an event racing a sweep cannot queue twice; a PostgreSQL race
   test must prove this. A losing unique claim reloads the committed coverage
   rather than submitting a fallback. An uncorrelated event cannot be absorbed
   into the window. This execution seam is required future work, not part of
   the dormant template expansion.
4. The added wait for a payment receipt email is at most **one minute** from
   its eligible rendered event. A sweep closes a window even if no second
   event arrives. A late event is not silently discarded: it is suppressed
   only when durable evidence proves its content was already covered; otherwise
   it uses its original template path. Ordinary delivery policy, including
   quiet hours, remains with the intent and notification owners.
5. A missing or invalid receipt reference or URL refuses the composed email
   and records a retryable failure. It must never fall back to a generic
   acknowledgement that looks like a receipt. A recipient or template policy
   suppression remains visible and cannot be overridden by the window.

The first cutover aims for one email for a proved payment pair within the
one-minute window. It does not claim that every subscriber restoration will
produce exactly one email: there is no shared payment-to-service episode key
today, and the current source does not emit `ont_online`.

## Adoption and cutover gates

### Explicit expand identity and two-step content gate

Both public parity and adoption boundaries first inspect PostgreSQL's actual
`current_user` role posture. They refuse a superuser, a role with `BYPASSRLS`,
an unavailable posture observation, or an unsupported database dialect before
reading legacy content or accessing Studio. Adoption checks inside the owning
transaction, so the role observation cannot create a caller transaction before
the command boundary. SQLite remains a content-only unit test lane.

Catalog flags and tests that assume `SET ROLE app_user` prove that role's
policy, not the application connection's identity. Controlled staging
acceptance requires an observation through the real app and worker connection
showing `SUPERUSER=false` and `BYPASSRLS=false`, then parity and replay under
that identity.

The dormant `communications.payment_template_adoption` command takes the
operator tenant UUID from `operator_tenant_id()` and maps exactly one EMAIL
`NotificationTemplate` row for each code. For `payment_received`, the one row
may have legacy code `payment_received` or `payment_received_email`; for
`invoice_paid`, it may be `invoice_paid` or `invoice_paid_email`. Having both
candidates, even if one is inactive, is ambiguous and stops the command. The
Template Studio identities are respectively `(tenant_id,
"payment-received", "email")` and `(tenant_id, "invoice-paid", "email")`.
The published version copies the legacy effective subject/body, including the
existing event-spec fallback where a source field is empty. The legacy UUID,
conditions, active flag, and purpose stay on the legacy row; the Studio active
flag copies it. Purpose governs manual customer-page sends and is included in
the source fingerprint and parity report. Studio metadata carries the legacy
UUID and a SHA-256 fingerprint
of its full source snapshot. An exact rerun does nothing; a changed source or
an existing Studio template with any metadata, content, publication or draft
change stops rather than replacing an operator edit. The Studio version and
provenance metadata persist with the command; the adapter returns both fixed
identities and versions for operator review. Adoption creates no EventStore
event or IntegrationDelivery, because the generic event dispatcher can queue
outbound webhooks. There is no automatic caller or scheduler.

The content gate has two distinct steps:

1. **Expand and verify:** explicitly backfill through Sub's contracted owner
   command using Template Studio's supported service API. Run the read-only
   subject/body parity report with representative event contexts. Verify
   receipt number and URL, inactive state, legacy conditions and purpose,
   channel and suppression outcomes; resolve any ambiguity or drift. Legacy remains the
   live writer and renderer during this step.
2. **Seal and switch:** in one reviewed cutover, prevent the legacy content
   writer from changing these email templates and change the live renderer to
   Template Studio's published version. Prove the old writer cannot return and
   keep the legacy UUID/conditions/active/purpose reference until their dependent
   policy and notification rows are migrated. Only after this step may the
   payment episode route be enabled with its due sweep. A local backfill and
   parity report alone do not authorize this switch.

The controlled operator adapter is
`GET /admin/notifications/payment-email-adoption/parity` and
`POST /admin/notifications/payment-email-adoption`. The GET needs
`notification:read` and returns only identity, active/condition and
representative parity evidence. The POST needs `notification:write`, Sub's
staff-only admin authentication and `/admin/` CSRF middleware. It requires the
exact `ADOPT_PAYMENT_EMAIL_TEMPLATES` confirmation and both legacy UUIDs from
the reviewed report in JSON fields `confirm`, `payment_received_legacy_id`
and `invoice_paid_legacy_id`, with matching CSRF cookie and `X-CSRF-Token`
header. The adapter authenticates, validates the request, releases the read
transaction and calls the public backfill owner once with an operator-scoped
`CommandContext` and those typed reviewed UUIDs. Inside that same owner-command
transaction, the owner locks the current legacy rows, verifies both reviewed
identities and representative parity, and refuses changed or incompatible
evidence before any Studio write. The adapter returns the committed adoption
and a fresh read-only parity outcome. No page, task or scheduler calls it automatically; its
staging invocation and evidence must still be controlled and reviewed. The
payment receipt
RenderContext permits only subscriber name, amount, portal URL, receipt number
and receipt URL. The invoice-paid context permits only subscriber name, amount,
invoice number, portal URL and invoice URL. Optional invoice/due-date values on
receipt events and receipt values on invoice-paid events are deliberately
excluded; an existing legacy template that uses them is an explicit adoption
conflict to resolve before cutover.

1. **Package prerequisite.** The base revision pins Kernel `0.1.0a94`, below
   Template Studio's `>=0.1.0a97` floor. The expand branch exact-pins
   Kernel `0.1.0a97` and published Template Studio `0.2.0a5` in both
   dependency declarations and the lock; compatibility and migration checks
   must pass before release.
2. **Shadow rendering.** Register Sub's real event render contexts and compose
   Template Studio's `mod_tstudio` lineage. Run the explicit email-only
   expand/backfill and parity command above without clobbering operator text.
   The legacy UUID, active flag and conditions remain in Sub until explicit
   migration: notifications and policy rows reference them. Compare rendered
   subject/body and suppression outcomes against the live path without sending
   twice. SMS stays on its legacy template and path in this slice.
3. **Sealed owner switch.** Only after shadow parity, route rendering through
   Template Studio's published version while Sub retains episode, condition,
   recipient and channel decisions. Retire or gate the old content writer in
   the same cutover. Document the old/new owner in the SOT registry and
   relationship map, and prove the old write path cannot return.
4. **Email episode switch.** Enable the payment-scoped window only after the
   shared renderer is authoritative and the billing event carries exact
   payment provenance. Before activation, add event-specific non-sending
   communication-intent planning/eligibility for each candidate event and
   persist its accepted or suppressed outcome. Planning must use the existing
   communications and notification policy owners without queuing a delivery,
   and must cover billing-contact expansion, reseller copies, intent replay,
   suppression, recent duplicates and delivery timing. Replan at closure.
   Compose only two accepted parts with compatible recipient, audience,
   category, timing and attachment semantics. Keep a suppressed part suppressed;
   an eligible uncovered part uses its direct route once. A delivery already
   linked to an intent forbids a fallback send for that covered recipient.
   Rendered content alone is insufficient evidence: closing a
   pair under the receipt event identity can otherwise change the
   `invoice_paid` policy outcome. Keep the direct email path as a bounded
   rollback route
   during verification; never restore a second template-content writer.
5. **Release and observation.** Require focused tests for operator edits,
   receipt and SMS parity, no-payment invoice settlement, two payments on one
   invoice, one payment on multiple invoices, late events, retries, concurrent
   claims, timeout and policy
   suppressions. Require real PostgreSQL tests for the window and row locks.
   Build one image from an exact green `main` SHA, accept that digest in
   staging, and promote only the same digest through Sub's protected release
   workflow. Verify customer receipt content, SMS, intent counts and failures
   from controlled production evidence before retiring the direct email
   fallback and closing S10.

## Current expand state and blockers

This expand branch composes the published Template Studio a5 tenant lineage and
pins Kernel a97. Its only payment-template runtime addition is the guarded,
explicit adoption and read-only parity adapter. The legacy notification handler,
writer, seeder, direct email and SMS routes remain unchanged. The adoption
command is not called by any handler, task, schedule, or startup path; no real
backfill or staging parity run has occurred.

The base revision has no reachable normal `invoice_paid` producer, so there is
no proved payment pair to compose. The payment episode model, task, sweep and
handler route are absent from this expand branch. A later cutover must prove
billing provenance, event-specific policy outcomes, PostgreSQL locking/RLS,
controlled parity, and a sealed Template Studio content-owner switch before
customer delivery changes.

A separate, unmerged cutover worktree contains an experimental payment-origin
`invoice_paid` producer, dormant episode storage/task, and an active-by-default
Template Studio content switch. None of those source changes are in this
expand branch. They require their own review and release gate; this expand
release must preserve the incumbent direct email and SMS behavior.

### Staging runtime authority blocker observed 2026-10-01

Michael explicitly named `seabone` for staging. A read-only observation through
its existing `dotmac_sub_app` process, at source
`2c33d50c007cbba507b73a1a563809faaea36dfb` and accepted image digest
`sha256:fa78082b35f8b6dc0071c6116c7dd59a5f4ede7e87968a7c76d93cbedffda29b`,
returned database `dotmac_sub` and `current_user=postgres` with both
`SUPERUSER=true` and `BYPASSRLS=true`. Independent review found no checked-in
staging exception permitting this runtime posture. This blocks real adoption.
The source guard refuses real adoption and parity under that identity; a
dormant package/lineage deployment does not resolve it.

The same bounded catalog observation found only 14 of 661 public tables with
any `app_user` DML privilege, while 665 public relations and 52 module relations
were owned by `postgres`. Switching only the runtime DSN would therefore be
insufficient. Source also uses the runtime DSN for Alembic and deploy
prerequisite checks. Michael selected alignment of module schema and migration
ownership with `app_admin`, with `app_user` for runtime traffic, on 2026-10-01.
That separate reviewed change must reconcile the existing `dotmac_app` schema
owner contract, split execution credentials, derive named grants from table
and persistence-plane contracts, and rehearse against migrated PostgreSQL.
An existing estate's ownership transfer still needs the exact reviewed
database/owner/ordered-statement plan and a verified restorable backup. No
database role, ownership, credential or deployment configuration was changed
by this observation or by the dormant expansion.

These are release gates, not authorization to infer a deployed cutover from an
installed package or a source-only adoption test.
