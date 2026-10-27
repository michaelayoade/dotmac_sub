# Website to WhatsApp acquisition attribution

## Contract and owners

The existing AI classification and `sales.lead_intake` command remain the only
decision and writer for classified WhatsApp Leads. Saving a website handoff is
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

The resolver supplies a typed acquisition snapshot to the existing Sales
materialization owner, which reads it against the original observations before
creating immutable Lead origin evidence. The bounded query includes earlier
reference-bearing messages in the conversation so clarification replies do not
lose attribution. Several different valid enquiries remain ambiguous. The contact channel
remains WhatsApp. Campaign fields record first-party URL observations, not proof
of Meta click attribution. A later message cannot rewrite an existing Lead's
origin or infer attribution from a phone number alone. Optional resolver failure
uses the standard owner savepoint and records durable failure evidence on the
Lead after rollback; it never suppresses an otherwise valid classified Lead.

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
default. The existing projection admits a committed classified WhatsApp Lead
only when its immutable origin and its verified website enquiry still match.
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
