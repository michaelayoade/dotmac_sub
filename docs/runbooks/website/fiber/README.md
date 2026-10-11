# Fiber website patch bundle

This is a reviewable snapshot of the website corrections recovered from the
earlier website investigation. These files are not loaded by Selfcare. Merging
this bundle does not install or configure WordPress.

Selfcare remains authoritative for saved Leads. The producer targets the typed
`FiberInquiryPayload` v1 receiver contract: `fiber-coverage-v1`, installation
address, name, phone, timezone-aware submitted time, and attribution journey.
The unsupported top-level service preference is retained in the message.
The producer limits area to 80 characters, validates paired coordinates, and
requires a reference and a recognized coverage result before confirming receipt.
Coverage failure and out-of-area results have different customer wording.

## Files and installation

- `dotmac-fiber-acquisition.php`: candidate MU plugin.
- `fiber-coverage-page.html`: candidate coverage-page source.
- `dotmac-fiber-outbox.php`: typed encrypted transport store; install beside the MU plugin.
- `test-producer.php`, `test-outbox.php`, `test-browser.cjs`: isolated fixtures; never install tests.

An authorized website operator must first back up the current plugin and page,
compare this bundle with the current live source, and apply it to a website
test environment. Install the plugin under `wp-content/mu-plugins` and update
the coverage page through the existing website publishing procedure. Do not
overwrite newer website changes without reconciliation.

Supply connection settings only through the approved server configuration:
Selfcare base URL, existing binding UUID
`8697e0d5-b2b3-4ec4-8c06-2f1e644402b1`, and signing secret. The secret must come
from an authorized operator and secure exchange, never from a tracked file.
The signing-secret and binding step remains deferred. Do not enable the binding
or deploy this bundle as part of PR creation.

Run `php test-producer.php` in this directory for the isolated contract harness.
It uses fake credentials, mocked WordPress, and no network or customer records.
It does not prove a complete WordPress installation or real server integration.

## Durable recovery candidate and remaining acceptance

The candidate now freezes payload bytes, submitted time, attribution, target and
delivery ID; encrypts durable pending server records; rejects changed content;
uses atomic per-delivery claims and bounded retries; and validates receipts on
both sides. Browser pending input is encrypted in IndexedDB with a nonextractable
key and serialized across tabs. The success transient has been removed from
the delivery owner. Pending confirmation never reports a saved Lead or fires
a Lead conversion. This is implemented source, not an installed website.

Read [DELIVERY_RECOVERY.md](DELIVERY_RECOVERY.md) for the owning transport contract,
retention, filesystem assumptions, required OS worker, failure visibility,
operator recovery, current read-only evidence and installation/rollback proposal.
That procedure still requires operator review and explicit target/action
authorization; no production binding or secret is changed by this PR.

Run `php test-producer.php`, `php test-outbox.php`, and `node test-browser.cjs`
for isolated fixtures. Tests do not contact Selfcare or create Leads. Mocked
browser storage and receiver counters are not real browser/WordPress/database
acceptance. The required real staging matrix is documented in DELIVERY_RECOVERY.

Remaining gates: approved test target/binding, real synthetic journeys proving
one committed Lead/stable replay reference, required CI, secret resolution and
secure exchange, WordPress connection/outbox settings, worker installation,
reviewed rollback, maintained `fiber-site` builder synchronization, and explicitly
authorized installation. Do not describe recovery as operational until the
worker, persistent private storage and real receiver have passed acceptance.
