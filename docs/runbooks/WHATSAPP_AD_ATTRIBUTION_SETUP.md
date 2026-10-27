# Website to WhatsApp attribution: installation and acceptance

Status: local implementation draft; not deployed, not launch acceptance.

## Local verification (27 October 2026)

- Fourteen database-free policy, boundary and mocked reporting tests passed.
- JavaScript handoff tests passed, including encrypted reload recovery and retries.
- Scoped Ruff lint and formatting checks passed; website PHP syntax passed using
  the target host's interpreter through stdin without installing a file.
- Database-backed tests and ownership-map generation could not run because the
  pinned private `dotmac_kernel` development package is unavailable. The full
  prescribed suite and migrated PostgreSQL acceptance remain outstanding.
- The local visual preview timed out; desktop/mobile visual acceptance is pending.
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
