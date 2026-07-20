# Payment Status Presentation

Date: 2026-07-14 · Status: implemented · Owner: Billing

## Boundary

`app.services.billing.payments` owns payment creation, settlement, failure,
refund, cancellation, allocation, and reconciliation. UI presentation is a
read-only projection and must not infer invoice collectibility, service
restoration, or refund eligibility from a badge.

`app.services.status_presentation` owns the label, semantic tone, and non-color
icon for the persisted `PaymentStatus` value:

| Value | Label | Tone | Icon |
|---|---|---|---|
| `pending` | Pending | warning | clock |
| `succeeded` | Succeeded | positive | check |
| `failed` | Failed | negative | x |
| `refunded` | Refunded | neutral | archive |
| `partially_refunded` | Partially refunded | warning | clock |
| `canceled` | Canceled | neutral | x |

Admin list/detail, customer activity/detail, reseller invoice detail, the API,
and customer mobile consume `status_presentation`. Raw payment status remains
available for behavior such as refund actions and filters.

## Color ownership

The payment projection emits a semantic role, never a color. Concrete status
colors are owned by branding (`positive`, `info`, `warning`, `negative`, and
`neutral`) and rendered through generated web theme tokens or the shared
Flutter build configuration. Icons and labels remain mandatory so color is not
the only cue. Branding updates must retain WCAG 2.2 AA text contrast in both
light and dark themes.

## Cutover gate

- Every persisted enum value has an exhaustive presentation test.
- API and portal projections serialize the same contract.
- Migrated payment surfaces have no local status-to-label, status-to-tone, or
  tone-to-color map.
- Lifecycle behavior still reads the raw enum and stays owned by Billing.
