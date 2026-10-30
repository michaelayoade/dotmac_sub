# Website to WhatsApp acquisition attribution

## Contract and owners

The existing AI classification and `sales.lead_intake` command retain the
decision and transaction for classified WhatsApp Leads. Existing authorized
manual creation through `sales.lead_authoring`, `communications.inbox_lead_actions`,
and legacy intake completion remains supported. Saving a website handoff is
not a Lead, does not stage `lead.created`, and must not send a Meta conversion.

`communications.team_inbox_observations` records the signed
`fiber-whatsapp-v1` website observation. The existing processing coordinator
stores the enquiry in Inbox and returns an opaque `FWA-` reference. Website
transport retries preserve the delivery ID and identical payload bytes.

`communications.website_handoff_attribution` owns the read-only resolution of a
reference in a verified inbound WhatsApp message. Matching requires exactly one
processed website observation, the same normalized phone, the declared WhatsApp
account, a reference in the expected format, and a submission no more than 30
days before the message. Missing, altered, expired, or ambiguous references do
not prevent normal message handling or Lead classification. They contribute no
website attribution. Customer-editable text is never accepted as campaign data.

The resolver supplies a typed acquisition snapshot to `sales.lead_lifecycle`,
the existing immutable origin owner. Every new Inbox Lead path supplies typed
conversation context to `create_party_lead`; the calling command still owns
whether to create a Lead. Lifecycle locks the signed receipt and checks its
unique claim before composing and validating the origin inside the standard
owner savepoint. A claimed receipt contributes no attribution to a second Lead.
The receipt lock remains held until the outer command completes. The bounded query includes earlier
reference-bearing messages in the conversation so clarification replies do not
lose attribution. Several different valid enquiries remain ambiguous. The contact channel
remains WhatsApp. Native provider campaign/ad-set/ad/click identifiers take
precedence as one identifier family; missing native fields are not filled from
a different website campaign. Website identifiers are used only when no native
identifier exists. Original website evidence remains in the linked signed
`IntegrationInbox` receipt, with its FWA reference and journey on the immutable
origin. Existing origin receipts, journeys, forms and references cannot be
repointed. Campaign fields record first-party URL observations, not proof
of Meta click attribution. A later message cannot rewrite an existing Lead's
origin or infer attribution from a phone number alone. Optional resolver failure
uses the standard owner savepoint and records durable failure evidence on the
Lead after rollback; it never suppresses an otherwise valid classified Lead.

## Website enquiry browse contract

`admin.sales.website-enquiries` is a read-only Sales list and detail surface at
`/admin/sales/website-enquiries`. Sales can find saved submissions without opening
Inbox or resolved conversations. Both `crm:lead:read` and `support:ticket:read`
are required, retaining existing access to prospect and Inbox enquiry data.
Links are exposed in Sales navigation only with the required permission.

The registered `sales.lead_lifecycle` acquisition read owner consumes verified
Fiber `IntegrationInbox` receipts and exact `LeadOriginCapture` receipt links.
It does not own enquiry persistence, Inbox status, identity or Lead creation.
The list deliberately does not join/filter conversation lifecycle or soft-active
state. Resolving a conversation cannot remove its saved enquiry from this view.
Defaults are all saved receipts, newest receipt time then UUID descending,
25 per page, with bounded 10/25/50/100 pagination. Search covers submitted
name/phone/email/address/area, reference and website campaign tag; source and
campaign filters use retained website tags, received-date bounds use inclusive
UTC days, and an optional exact-origin-link filter distinguishes linked/unlinked.
Totals count submissions, not distinct people or Meta-attributed conversions.

List rows show received time, confirmed reference, submitted contact/location,
website campaign/source, receipt status and a Lead link where the immutable
origin supplies it. An unlinked receipt does not prove that the person has no
Lead elsewhere. The detail page shows the same allowlisted submitted fields
and tracking evidence as the Lead page. No phone-based inferred link, contact
overwrite, backfill, conversion replay, create command or export is added.
Only receipts already received by Selfcare appear; undelivered website outbox
entries and unstored historical details require a separate audited recovery.

Invalid receipt data is a visible unavailable-details row, never silently
discarded from totals. Unknown processing state remains unknown. SQL/read failure
renders an unavailable state with no misleading zero total. Missing receipt IDs
are 404; unauthorized access is 403. Desktop uses a dense table; mobile uses
equivalent stacked rows. Details and filter controls remain keyboard accessible,
HTML escaped and readable in dark mode. Primary search stays visible; advanced
filters expand on demand and remain open when one is active. Existing navigation is retained; Open
enquiry is the row action and View Lead is a secondary action. Search/apply is
the list's primary control. No raw payload, headers or secrets are rendered.
Required tests cover resolved-independent reads, linked/unlinked receipts,
literal search, filtered counts/pagination, date bounds, permissions, invalid
evidence, unavailable-vs-empty UI and escaping. These changes share the existing
dependency, full-suite, migrated PostgreSQL and staging release gates.

## Lead detail page contract

Screen `admin.sales.leads.detail` supports Sales reviewing a Lead's acquisition
and original website enquiry. Existing `crm:lead:read` authorization applies;
the new section adds no command or permission bypass. Canonical Party/contact,
Lead address, status and Quote actions remain in the existing page context.
`sales.lead_lifecycle` supplies `LeadAcquisitionQuery` / `LeadAcquisitionView`
from the immutable origin and its exact linked receipt, with no phone search,
conversation re-resolution or historical repair. The web adapter exposes a
typed `LeadDetailContext`; templates render facts without deriving attribution.

The section shows channel, campaign/source tags, reference and original form
name, phone, optional email, installation address/area, optional plan and message.
Submitted details are explicitly labelled as submitted and never replace current
canonical contact or reviewed address. Technical origin/ad/click/journey IDs,
website tags, optional coordinates and timestamps use expandable evidence.
Website tags are observations, not proof of Meta attribution. Payloads are
validated against the ingress contract; only named display fields are exposed,
never raw JSON, signing configuration, tokens or headers. Personal details stay
inside the existing authenticated Lead page and are HTML-escaped.

Missing origin, no linked website enquiry, unreadable/mismatched receipt and
available enquiry have distinct read outcomes. An unavailable enquiry leaves
the captured origin visible and never becomes an empty "no enquiry" claim.
No new action, table, filter, pagination or export is introduced. The existing
page primary action remains Create Quote. The section stacks on mobile, supports
dark mode, uses text for availability, and permits long identifiers to wrap.
Required checks cover owner-to-context binding, legacy/no-origin display,
original-vs-canonical details, invalid receipt/journey rejection and HTML escaping,
plus mobile/desktop visual acceptance. This UI ships with the attribution slice
after the same dependency, full-suite, PostgreSQL and staging gates pass.

## Website contract

The website collects name, phone, installation address, and optional pin, sends
`fiber-whatsapp-v1` through its signed server-side connector, and waits for a
confirmed receipt before opening WhatsApp with the reference. It declares the
destination WhatsApp phone-number account ID in server configuration. The HMAC
secret never enters browser code. Pending submissions remain pending and retry
the same delivery. This requires a reviewed website bundle and staging acceptance;
the old `fiber-coverage-v1` producer must not be enabled as a substitute because
it creates a Lead immediately.

## Meta feedback

`meta.capi` 1.1.0 adds an explicit `report_whatsapp_leads` flag, disabled by
default. The existing projection admits a committed automatically or manually
created WhatsApp Lead with the saved verified handoff origin and signed receipt.
It does not resolve conversations again or depend on an AI-only capture label.
It uses the CRM Lead creation time and `action_source=system_generated`, rather
than claiming a browser event or a native Click-to-WhatsApp conversion. Only
normalized email/phone hashes are sent. The existing stable origin event ID,
durable delivery, post-commit dispatcher and retry lifecycle are reused. Enquiry
receipts, pending delivery, clicks and classifications without a committed Lead
are ineligible. Meta response or failure cannot roll back a Lead.
The old 1.0.0 manifest digest remains executable without new permissions.
Production still requires credentials, dataset configuration and Test Events evidence.
Never relabel a WhatsApp conversion as a website event or invent `ctwa_clid`/`fbc`
from arbitrary UTM or click strings.

The existing `sales.marketing_conversion_projection` also accepts receipt-bound
WhatsApp origins. It emits visitor and Lead milestones on committed creation;
the Lead timestamp is origin creation, while the visitor timestamp is website
capture. Qualification, payment, installation and activation retain their
existing event owners, payload and origin-stage deduplication. Coverage is
reported only for a real `fiber.coverage_evaluated` event. Enquiry submission
does not manufacture coverage or a Lead milestone. Linking an existing Lead
does not create a new Lead event or change historical acquisition evidence.

## PR 3784 boundary

This branch does not merge the sender-identity correction PR. Identity review
and clearance remain owned by Inbox contact resolution; attribution does not
clear identity, classify a customer or authorize Lead creation. Its import
conflict must preserve both typed attribution and staff-audit imports. The
PR's `reviewed_no_match` state is interpreted by the typed read query in
`app/services/team_inbox_prospect_resolution.py`, an implementation module of
`communications.team_inbox_contact_resolution`. Sales delegates to that owner.
Ordinary unmatched eligibility is retained. Open/missing clearance is blocked;
reviewed no-match requires the recorded clearing principal, an aware non-future
clearance timestamp and a fresh canonical no-match result. Existing customer
rechecks remain in place. This query cannot clear identity itself. Regression
tests cover those cases; actual combined-branch validation is still required.

## Validation and operation

Required tests cover lead-free enquiry persistence, exact replay, account/phone/
time/reference rejection, immutable Lead attribution, existing Lead creation
without a reference and optional-attribution failure. Pure policy/transport and
mocked browser recovery tests run locally. Database-backed tests and the full
registry generator currently require the unavailable pinned `dotmac_kernel`
package. Their failure is not acceptance. Integration acceptance must use
migrated disposable PostgreSQL; no PostgreSQL test target is configured locally.
Do not submit production test Leads. Rollback restores the website's current
WhatsApp-only handoff and disables signed website intake; preserve pending
enquiries and all Lead origin records.
