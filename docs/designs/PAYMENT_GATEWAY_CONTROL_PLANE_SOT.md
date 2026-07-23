# Payment Gateway Control Plane Source of Truth

Status: approved and implemented

## Decision

Online payment gateway setup is installation-backed. Product surfaces may call
these integrations plugins; the executable architecture calls them connectors.
The connector platform owns transport configuration, secret references,
capability grants, validation, and installation lifecycle.

Each payment connector manifest declares its capability bundle, required secret
bindings, allowed egress host, and safe non-secret configuration defaults. The
admin setup adapter renders and applies that manifest; it does not maintain a
second Paystack or Flutterwave setup definition.

There is no primary, secondary, or automatic-failover setting. Customer
presentment is the deterministic ordered projection of healthy, enabled
`payments.intent.v1` bindings. Higher `policy_json.presentment_priority` values
appear first. An operator choosing an option is not a failover event.

## Ownership

| Concern | Owner |
| --- | --- |
| Connector config, OpenBao references, capability bindings, validation and enabled state | `integration.installations` |
| Eligible gateway options and checkout ordering | `financial.payment_routing` |
| Gateway finance identity and settlement-channel bootstrap | `financial.payment_gateway_finance` |
| Intent amount, account, provider and pinned checkout binding | `financial.gateway_topup_intent_commands` |
| Provider webhook observations | `financial.payment_provider_events` |
| Payment, allocation, refund and ledger consequences | Existing financial command owners |
| Direct-transfer customer presentment | `financial.collection_accounts` |
| Recorded settlement classification | `financial.payment_channels` |

`PaymentProvider` and `PaymentChannel` rows are finance attribution identities.
Their `is_active` fields do not make online checkout available. Payment channels
classify where money landed; they do not route a customer to a gateway.

## Required gateway bundle

Saving Paystack or Flutterwave setup binds the following capabilities on one
installation:

- `payments.intent.v1`
- `payments.webhook.v1`
- `payments.reconcile.v1`
- `payments.refund.v1`

Checkout is unavailable unless the installation and the complete bundle are
enabled. Paystack also requires secret references for gateway credentials and
its public key. Flutterwave requires gateway credentials and its webhook
signing secret. Secret values live only in OpenBao (or an explicitly approved
environment reference); connector config revisions store references only.

Disabling new checkout disables only `payments.intent.v1`. Webhook,
reconciliation, and refund bindings remain enabled so in-flight payments can
finish safely. Disabling or quarantining the full installation is a separate
incident action.

## Intent provenance

New customer and reseller gateway intents persist the provider type, finance
provider identity, and selected `payments.intent.v1` binding. Initialization
and return verification resolve the required sibling capability from the same
pinned installation. Reordering gateway presentment after checkout does not
change an in-flight intent.

## UI contract

Settings → Secrets manages secret values and can add fields to an existing
OpenBao path. Integrations → Marketplace → Paystack/Flutterwave manages
references, ordering, bundle status, validation, enablement and disablement.

Customer and reseller checkout render only the server-owned gateway option
projection. An empty projection produces an honest unavailable state. Templates
must never invent Paystack, a provider key, or a default provider.

### Payment gateway setup page contract

- Screen: `admin.integration.payment_gateway_setup`; control-plane editor.
- Audience and job: authorized platform/finance operator configuring one
  supported online payment gateway.
- Decision: whether the gateway has complete referenced credentials and a
  healthy capability bundle, and whether it should accept new checkouts.
- Primary entity: connector installation; human identity is Paystack or
  Flutterwave and the installation name.
- Read owners: `integration.installations` for config/bindings/validation and
  `integration.registry` for the plugin manifest, and
  `financial.payment_routing` for effective checkout health.
- First viewport: checkout state and reason, installation state, lifecycle
  bundle readiness, stored-reference presence, presentment priority, and the
  next valid setup action.
- Primary action: save a reference-only configuration revision. Secondary
  action: explicitly confirm the impact preview, then validate and enable or
  disable new checkout.
- Command owners: `integration.installations` owns config and binding
  transitions; `financial.payment_gateway_finance` participates only to ensure
  attribution identities during setup.
- States: not configured, not installed, disabled, incomplete, healthy,
  checkout disabled, validation error, unauthorized, and empty activity.
- Sensitive fields: only masked OpenBao/environment references render. Secret
  values are write-only under Settings → Secrets and never enter this page,
  audit context, events, or logs.
- Drill-downs: secret management, integration marketplace, and canonical audit
  activity.
- Responsive projection: cards and forms stack; checkout state, impact, and
  confirmation remain visible before secondary evidence.
- Audit/observability: installation revisions and transitions carry the actor;
  connector validation produces bounded operation evidence; the page links the
  matching audit activity.

## Cutover and retirement

The cutover is direct:

- retired settings:
  `billing.payment_gateway_primary_provider`,
  `billing.payment_gateway_secondary_provider`, and
  `billing.payment_gateway_failover_enabled`;
- retired admin provider CRUD and failover pages;
- retired payment-provider mutation API;
- retired template and route-level Paystack fallback.

No legacy routing-setting reader or dual-write remains. Migration
`410_payment_gateway_control_plane` removes the retired settings and adds the
pinned capability binding to payment intents. Pre-cutover intents without a
binding remain provider-pinned so an already-debited customer is not stranded;
all new intents require and persist the selected binding.

## Verification

Tests must prove that unavailable gateways never render, complete bundles appear
in priority order, duplicate finance identities fail closed, saved cards select
Paystack in the service owner, intents retain their checkout binding, and secret
values never return from admin projections.
