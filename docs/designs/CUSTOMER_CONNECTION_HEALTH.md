# Customer Connection Health Source of Truth

## Decision

`network.connection_health`, implemented by
`app.services.topology.connection_status`, is the source of truth for the
customer-safe answer to “what is wrong with my connection?”. It owns:

- the complete `connected`, `trouble`, and `outage` vocabulary;
- the last-mile-versus-area-outage verdict;
- the customer-safe headline, message, and one-action advice;
- suppression of customer self-blame during a known area outage.

`ui.status_presentation`, implemented by
`app.services.status_presentation`, owns only the cross-client label, semantic
tone, and icon key for that already-derived verdict. It does not diagnose the
connection.

## Boundaries

- `network.device_state` remains the NOC device-operational owner. Its
  `up/degraded/down/maintenance` vocabulary is not customer connection health.
- RADIUS sessions, ONT observations, monitoring observations, access paths,
  and incidents are inputs. Their adapters do not independently decide the
  customer verdict.
- Raw “online now” indicators on subscription and admin detail screens remain
  observations. They are not migrated into the connection-health vocabulary.
- `customer_service_state` may compose connection health with billing and
  support consequences, but does not reword or reclassify the verdict.

## Transport contract

Customer-safe responses carry both the raw state and presentation metadata:

```json
{
  "state": "trouble",
  "status_presentation": {
    "value": "trouble",
    "label": "Connection issue",
    "tone": "warning",
    "icon": "alert"
  },
  "headline": "Router not responding",
  "message": "Your router isn't responding.",
  "advice": "Power it off, wait 30 seconds, then turn it back on.",
  "medium": "fiber",
  "area_outage": false,
  "checked_at": "2026-07-14T12:00:00+00:00"
}
```

Responses do not expose topology identifiers, device names, raw signal values,
other customers, or the internal last-mile verdict.

## Migration and cutover

- Old presentation owners: state-to-color/label switches in the customer
  connection page, reseller dashboard, and customer Flutter connection screen
  and home banner.
- New presentation owner: `app.services.status_presentation`, transported as
  `status_presentation`.
- Verification: exhaustive vocabulary/presentation tests, safe-payload tests,
  web/mobile architecture tests, and Flutter parser/widget tests.
- Cutover gate: migrated verdict surfaces render tone/icon from the server
  contract and retain raw state only for behavior such as drill-in eligibility.
- Compatibility: old or cached mobile payloads humanize the raw state with a
  neutral tone and info icon; they do not recreate domain tone policy.

## Consumers

- `/portal/connection` and `/portal/connection/status.json`
- `/api/v1/me/connection-status`
- reseller customer-connection summaries
- `CustomerServiceState.support_context()`
- customer Flutter connection status and home connection banner
