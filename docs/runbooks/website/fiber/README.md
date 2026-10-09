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
- `test-producer.php`: isolated fixture harness; never install this test file.

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

## Outstanding reliability implementation and acceptance

This snapshot is a contract correction, not a completed durable relay. Before
launch, the follow-up must satisfy every item below:

- [ ] Freeze the exact serialized payload, attribution and submitted time for
  one submission ID before its first delivery; reuse identical bytes on retry.
- [ ] Persist pending deliveries securely so browser closure and process restart
  cannot silently discard an accepted enquiry; specify retention and recovery.
- [ ] Use atomic claiming and bounded retry with visible terminal failures.
- [ ] Reject changed content under an existing submission identity.
- [ ] Show pending separately from confirmed Selfcare receipt; never report
  pending delivery as a saved Lead or fire the Lead conversion prematurely.
- [ ] Require the browser to validate the receipt before displaying success.
- [ ] Test lost response after commit, retry after restart, concurrency, invalid
  signature, malformed receipt and repeated delivery using synthetic staging data.
- [ ] Confirm exactly one Lead and stable reference for an identical replay.
- [ ] Synchronize the deployed WordPress source with its maintained source copy.

The current success transient is a cache, not a durable pending outbox. The
current producer regenerates submitted time on an uncached retry. These are
known remaining defects and must not be described as solved by this bundle.
