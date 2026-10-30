# Website to WhatsApp attribution: installation and acceptance

Status: implementation draft; not deployed, not launch acceptance.

## Local verification (30 October 2026 rework)

- Thirty-six database-free policy, boundary, template and mocked reporting tests passed,
  including native identifier precedence, shared acquisition ownership and the
  real lifecycle declaration's structural contract (not the full registry graph).
- Added database-backed automatic/manual acquisition detail reads, Lead page
  context, Meta opt-in/deduplication and marketing milestone regression tests;
  these are unexecuted until dependencies are restored.
- Rendered the actual acquisition template with production CSS and synthetic
  data at 1440px desktop and a true emulated 390px mobile viewport, including
  expanded details in dark mode. All three measured no horizontal overflow.
  This verifies the section, not the full authenticated application/database flow.
- Added Sales > Website enquiries list/detail with typed search, tag/date/link
  filters, bounded pagination and shared submitted-data display. Both existing
  CRM Lead and Inbox read permissions are required. The read query does not
  depend on conversation status and never creates or backfills a Lead.
- Desktop and true 390px mobile enquiry-list/detail previews use production CSS
  and synthetic data; read failure stays unavailable rather than zero results.
  Full database query and authenticated route checks remain blocked at test
  collection by the missing private Kernel package.
- JavaScript handoff tests passed, including encrypted reload recovery and retries.
- Scoped Ruff lint and formatting checks passed; website PHP syntax passed using
  the target host's interpreter through stdin without installing a file.
- Database-backed tests and ownership-map generation could not run because the
  pinned private `dotmac_kernel` development package is unavailable. The full
  prescribed suite and migrated PostgreSQL acceptance remain outstanding.
- The earlier website-form preview timed out; full website desktop/mobile and
  authenticated application acceptance remain pending.
- No customer enquiry, Lead, Meta event, deployment or advertisement change was
  made during these checks.

## Current live evidence (27 October 2026)

- Existing Fiber installation `c1df0da2-494c-44a8-9ec4-90e86700dead` and binding
  `8697e0d5-b2b3-4ec4-8c06-2f1e644402b1` are disabled pending connection validation.
- WordPress has the expected Selfcare URL/binding and the v1.2 durable outbox.
  Its signing secret is absent. Preserve all pending records and server salts.
- Selfcare has no installed `meta.capi` connector. Meta reports no recent server
  Lead events. Nothing in this branch configures a live credential or changes ads.

## Backend release gates

Restore the exact pinned private development packages, regenerate the SOT
relationship-map manifest, and run focused tests plus the full prescribed suite.
Run `make test-integration` against an explicitly disposable migrated PostgreSQL/
PostGIS target. Complete the mandatory feature-to-main, digest-bound staging
and production release process. A local draft is not production authorization.
Include the typed acquisition read and Lead detail template in the same backend
candidate as attribution and reporting. Staging must verify original submitted
name/phone/address, source tags and IDs on automatically and manually created
Leads, and confirm legacy Leads show an explicit missing-origin state. Do not
enable the website handoff or Meta reporting before that candidate is accepted.
Include the central enquiry list/detail and shared submitted-data templates in
that same candidate. Confirm both permissions, empty/failed reads, filter and
pagination behavior, and that resolving a conversation leaves its received
submission visible. The list contains receipts already received by Selfcare;
it does not import website outbox entries or reconstruct unstored older Leads.

Staging must prove: enquiry creates no Lead; exact replay preserves reference;
same reference with another phone/account or expired time contributes no origin;
clarification replies retain one matching reference; multiple enquiries remain
ambiguous; ordinary/complaint/support messages follow existing classification;
an optional resolver failure still creates an otherwise eligible Lead and records
failure evidence; repeated classified events create one Lead and one Meta delivery.

## Website operator (cPanel or approved deployment)

1. Back up WordPress page 1783, the served `coverage/index.html`, the producer,
   private configuration and retry-worker configuration outside `public_html`.
2. Verify the deployed backend supports `fiber-whatsapp-v1` before configuring it.
3. Use an authorized OpenBao operator to provision the signing field at
   `bao://secret/integrations/fiber_inquiry#webhook_signing_secret`. Securely
   configure the same secret in the existing private WordPress connection file.
   Keep the existing URL/binding; do not create duplicates or put secrets in chat.
4. Configure `DOTMAC_FIBER_WHATSAPP_ACCOUNT_ID` with the verified Meta phone-number
   account ID for Sales' +234 812 117 9536. It is an account ID, not the phone number.
5. Validate and enable the existing Fiber binding through the approved owner.
6. Install `website/fiber/dotmac-fiber-whatsapp-handoff.php` and
   `website/fiber/coverage-handoff.js` from this runbook directory under MU plugins.
   The original acquisition and outbox plugins remain dependencies, not replacements.
7. Rebuild the page with `scripts/website/prepare_whatsapp_handoff.py` from a fresh
   inspected live copy. The checked-in candidate is for review, not blind overwrite.
   Publish both served static and WordPress representations with canonical
   authenticated publisher attribution; preserve newer content and tracking.
8. Verify desktop/mobile form behavior and retry/reload recovery. Do not submit
   production test Leads without separate authorization.

## Ad link

After staging acceptance, tag only the existing Abuja ad destination with:

```text
utm_source=meta&utm_medium=paid_social&utm_campaign=abuja_fibre&campaign_id=120249775247830644&adset_id=120249775247930644&ad_id=120249775247940644
```

Keep the destination `https://fiber.dotmac.ng/coverage/` and existing budget/status.
Do not copy these IDs onto another ad. URL observations are first-party attribution
evidence, not authenticated Meta referral evidence. Removed references or a
different sending phone remain unattributed; do not guess from phone or message text.

## Meta operator

The reworked draft captures acquisition through `sales.lead_lifecycle` for both
existing automatic and manual Inbox Lead commands. Reporting reads the saved
origin and signed receipt, without reinterpreting later WhatsApp messages.
Validate native-ID precedence, receipt single-claim behavior, replay, optional
failure isolation, manual creation, and downstream marketing milestones before
enablement. Restore the pinned private Kernel dependency, regenerate the SOT
manifest and pass the prescribed suite on a development host/CI, including real
migrated disposable PostgreSQL. PR 3784 is not merged here; its identity-clearance
eligibility now delegates to the identity owner's typed clearance query. Actual
combined-branch validation remains an integration gate. Keep production disabled
until these checks and digest-bound staging acceptance succeed.

1. Provision a suitable dataset access token through an approved operator at
   `bao://secret/integrations/meta_capi#access_token`.
2. After backend deployment, install `meta.capi` 1.1.0, reuse an installation if one
   has since appeared, and bind `marketing.website_lead.send.v1`.
3. Configure pixel `410389919883152`, API `v26.0`, timeout 10, max attempts 8,
   `report_whatsapp_leads=true`, and a staging-only Test Events code.
4. Validate, then enable on staging. Prove one committed eligible WhatsApp Lead
   produces one accepted server Lead, and that event/retry replay preserves its ID.
   A mock response or website click is not proof of Meta receipt.
5. Remove the Test Events code in a new configuration revision, validate, and only
   then authorize production enablement. Monitor IntegrationDelivery outcomes.

Rollback: disable the Meta capability independently of Sales Lead creation.
Restore the previous website representations and pause enquiry intake if needed;
preserve the outbox, observation ledger, Lead links and immutable attribution.
