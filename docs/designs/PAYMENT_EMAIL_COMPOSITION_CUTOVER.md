# Payment email composition cutover

**Status: cutover implementation candidate; no customer send path has switched.** S10 records the
customer-facing symptom. This design covers the first bounded cutover:
`payment_received` and `invoice_paid` emails caused by the same proved payment.
The existing `invoice_paid` envelope receives additive typed payment causation; financial status derivation and non-email channels retain their owners. Service
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
   The first accepted recipient decision immediately creates its ordinary
   durable notification in the same transaction. An advisory transaction lock
   serializes the absent-episode race. For an existing episode, lock order is
   notification, episode, parts, then source decisions/coverage. A compatible
   second source can update the pending body and acquire coverage only before
   the fixed deadline. Replays reload their source coverage; a losing first
   creator cannot submit another notification. PostgreSQL tests must prove
   concurrent first creators and an event racing a delivery claim.
4. The collection floor is **first accepted decision time plus 60 seconds**;
   lock waits never reset it. The queue combines this minimum with its existing
   canonical send time, including quiet hours. Immediate-latency delivery asks
   the existing worker for an after-commit ETA wakeup, and its periodic queue
   recovery owns dispatch failure and overdue rows. There is no episode sweep
   required to create a receipt and no separate retry engine. Scheduler or
   provider outage can delay actual delivery; the one-minute bound concerns
   added collection delay and does not override quiet hours. Staging acceptance
   must show the real periodic recovery path is running. A late uncovered source
   uses its original published body through the same intent owner.
5. A missing or invalid receipt reference or URL refuses published receipt
   rendering and fails the event transaction for controlled repair/retry. It must never fall back to a generic
   acknowledgement that looks like a receipt. A recipient or template policy
   suppression remains visible and cannot be overridden by the window.

6. Composition accepts plain text only. The shared composer joins full rendered
   bodies with a blank line; repeated greetings/sign-offs remain intact. HTML
   documents or fragments retain their unchanged individual published delivery
   path until a versioned fragment/layout contract is available. Attachments,
   audience, policy class and timing must be compatible; the pilot composes
   subscriber recipients and leaves reseller copies on their existing route.
7. Before provider claim, the existing worker holds the notification row lock
   while the correlation owner rechecks covered recipients and rebuilds the
   body from eligible frozen parts. Suppressed sources cannot be credited with
   another source's delivery. One accepted source still sends its full body;
   if all become ineligible, the physical row is canceled and both outcomes
   remain suppressed. Coverage prevents duplicate application queue rows. The
   incumbent transactional transport uses bounded retries and has no general
   provider idempotency contract, so a crash after provider acceptance can
   still produce a duplicate provider delivery; this cutover does not claim
   provider-level exactly-once delivery.

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
   payment episode route and causal producer be enabled by the same persisted activation row. A local backfill and
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
   suppression, recent duplicates and delivery timing. Recheck each source
   when joining and again under the existing delivery claim.
   Compose only two accepted parts with compatible recipient, audience,
   category, timing and attachment semantics. Keep a suppressed part suppressed;
   an eligible uncovered part uses its direct route once. A delivery already
   linked to an intent forbids a fallback send for that covered recipient.
   Rendered content alone is insufficient evidence: attributing a
   pair under the receipt event identity can otherwise change the
   `invoice_paid` policy outcome. The explicit composition pause restores
   individual published emails for new sources while keeping Studio authority
   and the existing coverage for queued episodes.
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

## Implementation candidate and remaining acceptance gates

The expansion pins published Template Studio a5 and Kernel a97 and supplies
explicit adoption/parity. The cutover adds per-recipient intent planning and
physical coverage, tenant-scoped queued episode/source evidence, a typed
settled-account `invoice_paid` consequence, and published Studio rendering.
`payment_email_cutovers` has no bootstrap/default row. Its explicit owner
command proves current locked adoption parity before activation; the same row
controls the handler and producer. Installation, deployment and content
adoption alone activate none of these paths.

The activation adapter is `POST /admin/notifications/payment-email-cutover`,
with staff authentication, `notification:write`, existing admin CSRF protection,
confirmation `ACTIVATE_PAYMENT_EMAIL_CUTOVER` and both reviewed legacy UUIDs.
The publication adapter requires an expected published version. Legacy content
fields and routing identity are sealed by the notification owner and a
PostgreSQL trigger once active; legacy conditions, purpose and routing active
state remain Sub policy inputs. Subsequent content publication uses only
Template Studio's versioned service. Cutover, publication and source-collection
identifier evidence uses `emit_event(record_only=True)`: it commits as completed
with `processed_at` and never enters the pending dispatcher or webhook path.
`dispatch_after_commit=False` alone only defers ordinary dispatch and is
insufficient for control evidence.

The seal is stored on each reviewed global legacy row, independently of the
tenant visibility of the cutover record. Activation locks the legacy table
against concurrent alias insertion/rebinding until parity and both seals
commit. Tenant foreign keys for the new evidence tables are installed by 638
after the operator-tenant provider, following the existing domain_settings/523
pattern; current ORM metadata must not introduce a dependency into Sub's older
squashed base before that provider exists.

Startup template seeding reads existing `(code, channel)` identities before
attempting inserts. An existing sealed payment email therefore takes only the
read and receipt-validation path: PostgreSQL runs its `BEFORE INSERT` seal
trigger even for `ON CONFLICT DO NOTHING`. Missing defaults still use the
unique constraint for concurrent seed arbitration. Activation and the one-way
pause never reopen insertion of a payment email alias.

Before switching the runtime DSN to `app_user`, prove that the complete startup
template inventory already exists, or seed missing defaults through a separately
authorized step before runtime starts. This slice grants only `SELECT, UPDATE`
on `notification_templates`; the generic startup seeder still inserts genuinely
missing defaults. The PostgreSQL canary pre-seeds those defaults and proves the
sealed existing-row path, not readiness of a live staging inventory. A missing
default must not be repaired by silently widening runtime grants.

`POST /admin/notifications/payment-email-cutover/pause` requires the same staff,
write and CSRF guards plus `PAUSE_PAYMENT_EMAIL_COMPOSITION`. It disables the
causal producer and new pair collection in one owner-command transaction.
Producer/dispatcher reads hold a shared gate lock until commit, so a completed
pause fences new collection. Already queued episodes drain with their existing
coverage and frozen published bodies. Studio rendering, versioned publication
and legacy content seals stay active; activation replay does not resume a
paused gate. Resumption requires a separately reviewed activation contract.
An inactive Studio publication suppresses its email only and never restores
legacy email content or disables SMS.

The causal producer covers the two normal settled-account create/settle paths
and captures prior status under the invoice lock before canonical recomputation.
Credit notes, historical repair, consolidated/multi-invoice payments and other
allocation workflows do not manufacture causation. They keep their incumbent
routes pending a separately proved contract.

This source is a candidate until behavior, mature-channel parity, real
PostgreSQL concurrency/RLS and exact-image staging gates pass. No activation,
content backfill, ownership transfer or staging parity operation has executed.
The earlier experimental sweep and active-by-default switch are not the
implementation being promoted.

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

Migration 638 supplies only this slice's named legacy runtime prerequisites:
`app_user` `SELECT, UPDATE` on the single-operator `notification_templates`
routing catalog for reading, locking and sealing the reviewed identities,
and `SELECT, INSERT` on `event_store` for causal replay checks and durable owner
evidence. The migrated-role tests must exercise these grants directly, without
test-side privilege changes. These two global assembly tables retain their
existing single-operator contract; they are not tenant-module storage. This
limited grant does not resolve the separate ownership, remaining application
grants, migration credential, or deployed-role blockers above. Downgrading the
episode schema does not revoke pre-existing application prerequisites; the
operational rollback remains the one-way composition pause.

### Automatic image rollback floor

The deploy adapter may restore a previous application image only while the
actual PostgreSQL catalog proves the pre-installation legacy shape. The
candidate image reads the catalog under its actual `app_user` login before any
previous-image repin or recreation, using Sub's read-only snapshot seam before
the transaction begins. A `payment_email_cutovers` relation **or**
the `notification_templates.studio_content_sealed` column closes the automatic
image rollback path, even with no activation row or after composition is paused.
Both absent permits rollback only when the expected legacy template relation
and columns are present. Missing, partial, unreadable, or failed catalog or role
proof refuses; an old image must never interpret installed seals or create
uncovered notifications. This installation floor does not claim adoption.

On floor refusal the deploy leaves the current pin and any healthy warm
candidate in place instead of restarting the old image. If repinning,
recreation, or restored health fails, the candidate remains available and the
operator must inspect both pin values before repair. Repair forward with the
current image;
the one-way composition pause remains the operational behavior rollback after
activation. Database migrations are never automatically reversed.
