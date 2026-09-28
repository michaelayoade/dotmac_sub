# Meta CAPI Fiber Lead Delivery

## Authority and eligibility

Selfcare is the conversion authority. The signed
`POST /api/v1/webhooks/fiber-inquiry/{binding_id}` adapter verifies and claims an
`IntegrationInbox` delivery, records the provider observation, and invokes the
Team Inbox fiber owner. A Lead is confirmed only after `sales.capture` persists
the `Lead` and immutable `LeadOriginCapture` and commits `lead.created`.
Endpoint receipt, signature success, and form validation are not conversions.

`integration.meta_capi_lead` accepts only origins with all of:

- `capture_source == fiber.website_inquiry` and `source_platform == website`;
- `external_form_id == fiber-coverage-v1`;
- the signed inbox payload's `interest == new_connection`.

This excludes support/contact forms and every non-fiber CRM or Inbox Lead.

## Delivery and idempotency

The post-commit event handler writes one `IntegrationDelivery` before waking a
Celery worker. Meta availability is outside the customer transaction. The
idempotency key is `meta-capi-website-lead:{origin_capture_id}`. Meta `event_id`
is UUIDv5 over `dotmac:meta-capi:website-lead:{origin_capture_id}`. Both remain
stable across WordPress retry, event replay, Celery retry, timeout, worker
restart, and operator replay. A future browser Lead can use this exact ID for
browser/server deduplication; no browser Lead is implemented here.

The durable payload contains safe Lead/inquiry/origin identifiers, event time,
`fiber.dotmac.ng` source URL, and SHA-256 hashes of normalized email and phone.
Email is trimmed and lower-cased. Phone is normalized to an international
country-code number, reduced to digits, then hashed. The connector sends only
`em` and `ph`. It does not send names, address, message, plan, UTMs, IP, user
agent, `fbp`, or `fbc`. The received `click_id` remains first-party attribution
only: this repository cannot establish it is `fbclid`/`fbc` (tests use a Google
`gclid` example), so sending it as a Meta identifier would be incorrect.

## Configuration and secrets

Install connector `meta.capi` version `1.0.0` in Integration Marketplace and
bind `marketing.website_lead.send.v1`. The binding and installation must both
be enabled; disabled is the `META_CAPI_ENABLED=false` equivalent.

Create an immutable configuration revision with:

| Concept | Integration config | Value |
| --- | --- | --- |
| `META_CAPI_PIXEL_ID` | `pixel_id` | `410389919883152` |
| `META_CAPI_API_VERSION` | `api_version` | `v26.0` |
| `META_CAPI_TEST_EVENT_CODE` | `test_event_code` | optional; staging only |
| timeout | `timeout_seconds` | `10` recommended |
| bounded retry | `max_attempts` | `8` recommended |

Bind secret `access_token` to
`bao://secret/integrations/meta_capi#access_token`. This is the
`META_CAPI_ACCESS_TOKEN` concept. Put the real token only in OpenBao; never in
tracked environment files, configuration JSON, API responses, frontend code,
or logs.

## Failure handling and observability

Meta 429, network errors/timeouts, 5xx, and responses with no accepted event
are retryable with bounded exponential backoff (`Retry-After` is honored).
Authentication/configuration and other 4xx validation failures dead-letter.
A 60-second redrive recovers pending, due retryable, and expired-lease rows.

Query `integration_deliveries` for capability
`marketing.website_lead.send.v1`. States answer queued (`pending`), succeeded
(`delivered`), failed (`dead_letter`), and retrying (`retryable`/`leased`).
`external_receipt_json.deduplicated_count` records replay suppression;
`delivered_at` gives the last success. Receipts contain only status, accepted
count, Meta trace ID, and error classification. Prometheus exports
`meta_capi_lead_events_total{outcome="queued|succeeded|failed|retrying|deduplicated"}`
and `meta_capi_lead_last_success_timestamp_seconds`; the durable rows remain the
authoritative source if worker/web metrics processes are separate.

## Staging Test Events

1. Keep production disabled. In staging, store the access token at the OpenBao
   reference above.
2. Configure pixel `410389919883152`, API `v26.0`, and the temporary Test Events
   code from Meta Events Manager in `test_event_code`.
3. Validate the connection, bind `marketing.website_lead.send.v1`, and enable
   the staging installation/binding.
4. Submit one synthetic signed `fiber-coverage-v1` inquiry with
   `interest=new_connection` and a unique webhook delivery ID.
5. In Meta Events Manager > dataset `410389919883152` > Test Events, verify
   exactly one `Lead` with connection method `Server`. Replay the same delivery
   and verify no second event.
6. Confirm its `IntegrationDelivery` is `delivered`, has one stable `event_id`,
   and contains no raw contact data.
7. Disable the binding. Create a new immutable config revision with
   `test_event_code` omitted/empty, validate it, and only then continue. Never
   promote a revision carrying a Test Events code.

## Production enablement and rollback

1. Deploy with the production installation/binding disabled.
2. Complete staging acceptance above on the candidate digest.
3. In production OpenBao, set only `secret/integrations/meta_capi#access_token`.
4. Configure pixel `410389919883152`, API `v26.0`, no `test_event_code`, timeout
   10, and max attempts 8; validate the connection.
5. Enable `marketing.website_lead.send.v1` for a small canary and submit one
   genuine test inquiry approved for production measurement.
6. Confirm one server `Lead`, stable event ID, delivery success, and healthy
   dataset diagnostics before broad traffic. Monitor states, deduplication,
   retry classifications, and last success.

Rollback is immediate and non-destructive: disable the capability binding (or
installation). Fiber inquiry persistence continues. Existing delivery evidence
is retained; do not replay it until configuration and measurement window are
reviewed.
