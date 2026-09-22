# Fiber acquisition attribution deployment

The implementation is deployed through the normal immutable-digest release
sequence. Do not configure WordPress until the staging migration and signed
smoke test have passed.

## Required secrets

- Create a random HMAC secret in the approved secret manager and bind it as
  `webhook_signing_secret` on the `fiber.inquiry.http` `1.1.0` installation.
  Never put
  the value in connector config, logs, commits, screenshots, or tickets.
- Set `CONVERSION_INGEST_API_KEY` from the approved secret manager for stable
  pseudonymous subject-key derivation.
- Bind the same marketing API key to the outbound `webhook.http` `1.1.0`
  installation as secret `authorization`; configure `authorization_scheme` as
  `Bearer`.

The repository's OpenBao bootstrap accepts the two Sub-owned values through
`FIBER_INQUIRY_WEBHOOK_SIGNING_SECRET` and `CONVERSION_INGEST_API_KEY`. It
stores them at `secret/integrations/fiber_inquiry` and
`secret/settings/marketing`; neither value belongs in Git, connector config,
or operator output. The application OpenBao token is read-only, so secret
provisioning must use the operator bootstrap token.

## Capability bindings

Create and enable `communications.fiber_inquiry.receive.v1` with:

```json
{
  "site_id": "fiber.dotmac.ng"
}
```

After provisioning the secret, configure the receiver through the checked-in,
idempotent adapter:

```bash
python -m scripts.one_off.bootstrap_fiber_inquiry_integration \
  --apply \
  --environment sandbox
```

Use `--environment production` only for the production installation. The
command prints the binding UUID and callback path but never the secret. Run
without `--apply` first for a no-write preview, or use `--prepare` to create a
disabled binding before the secret exists.

The omitted values intentionally use these defaults:

- signature header: `X-Dotmac-Fiber-Signature`
- delivery header: `X-Dotmac-Fiber-Delivery`
- signature prefix: `sha256=`

## What the availability result means

This deployment work does not introduce a new coverage algorithm. It exposes
the existing `sales.selfserve.compute_feasibility` rule to the Fiber website:

- The website must submit a latitude and longitude. An address without a map
  pin creates the Lead but returns no automatic coverage result, so staff can
  arrange a survey.
- PostGIS measures the straight-line map distance from that pin to the nearest
  active Fiber Access Point with recorded geometry.
- `covered` means the distance is within
  `selfserve_quote_feasibility_radius_meters` (2,000 metres by default).
- No active Fiber Access Point produces `out_of_area`; a point beyond the
  configured radius produces `survey_required`.

This is an initial feasibility indication, not an installation guarantee. It
does not currently prove available splitter/OLT ports, route continuity,
building access, or construction readiness. The public response deliberately
omits the internal access-point identity and measured distance, and tells the
customer that Dotmac will confirm the installation details.

### Fiber website location UX contract

- Browser location must remain optional. Denying permission, using a browser
  without geolocation, or submitting an address without coordinates must not
  prevent the enquiry from being recorded for manual review.
- Label the action `Use my current location` and place this explanation beside
  it: `Use this only if you are currently at the installation address.`
- Do not describe browser coordinates as a guaranteed or final coverage check.
  When coordinates are present, the result is only the distance-based initial
  feasibility indication described above.
- The website should later add an address search or draggable map pin so a
  customer can select the installation property while somewhere else. That
  control must submit the selected property's coordinates through the same
  optional latitude/longitude fields; it must not introduce a second coverage
  rule in WordPress.

Create and enable an `events.deliver.v1` `webhook.http` binding with URL
`${MARKETING_BASE_URL}/api/v1/conversions/events`, method `POST`, and
`authorization_scheme: Bearer`. Subscribe it to
`marketing.conversion_ready` with payload policy
`{"projection":"event_payload.v1"}`.

## Staging acceptance

1. Upgrade the staging database to Alembic head and confirm revision
   `616_fiber_acquisition_attribution`.
2. Submit one redacted, signed `fiber-coverage-v1` request with a unique
   delivery ID. Confirm a single IntegrationInbox receipt, provider observation,
   Lead, immutable origin, customer reference, and safe coverage response.
3. Replay the identical delivery and confirm the same reference, coverage,
   conversation, and message with no new records.
4. Confirm invalid signatures create no durable receipt or business data.
5. Inspect the marketing delivery payload and confirm it contains none of:
   customer name, email, phone number, or street address.
6. Submit an address without coordinates and confirm a Lead is created while
   `coverage` is null.

Record only the base URL, binding UUID, header names/prefix, migration revision,
test results, and a redacted response. Hand the website administrator a secret
manager reference or approved one-time secure exchange; never return the secret
itself.

## Repository versus external configuration

- This repository owns the signed receiver, the Integration Platform bootstrap,
  lead/origin persistence, coverage evaluation, and the Selfcare callback path.
- The `fiber.dotmac.ng` WordPress deployment owns its upstream callback URL,
  binding UUID, and matching HMAC secret. Those settings cannot be committed to
  this repository and must be applied on the website host after staging
  acceptance.
- A deployment is incomplete until both sides use the same secret and the
  WordPress callback URL includes the enabled binding UUID.
