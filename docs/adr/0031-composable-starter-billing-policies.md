# ADR 0031: Composable Starter billing policies for the thin-client cutover

Status: accepted

Date: 2026-10-17

Accepted: 2026-10-17 by Michael

Decision owner: Michael / Dotmac architecture

Affected systems and domains: billing issuance and settlement composition
wiring in Sub, domain settings, Starter `dotmac-billing` / `dotmac-payments` /
`dotmac-numbering` consumption, subscriber account identity

## Context

Sub is cutting over in full to the Starter money pack (`dotmac-billing`,
`dotmac-payments`, `dotmac-integration`, `dotmac-numbering`, tax/accounting
contracts) as sole ledger authority, with Sub as thin client only (ADR 0007
destination, ADR 0020 in Starter). The Starter contracts are deliberately
evidence-explicit: issuance, settlement acceptance and opening import require
the assembly to supply identity conventions, tax/due policy evidence,
numbering series and account mappings. Sub's current admin paths do not carry
most of that evidence as data; it exists only as scattered literals, settings
and render-time lookups.

Michael directed on 2026-10-17 that the cutover use **composable policies
with defaults — no hardcoding** — resolved from settings, because Sub's
account reality is linked and heterogeneous: one person may be responsible
for several accounts, one person may hold two subscription types (for
example radio and fiber), and a reseller may operate many accounts. Any
convention baked in as a literal (account identity, charge component, tax
policy, due terms, series) would silently break one of those shapes.

## Decision

1. **Policy layer.** All assembly-supplied values for Starter money-pack
   contracts are resolved through one composable policy surface in Sub,
   `app/services/billing/starter_policies.py`, backed by domain settings
   (`SettingDomain.billing`) with typed defaults declared in
   `app/services/settings_spec.py`. Callers never inline Starter-contract
   literals (series codes, profile codes, evidence tokens, jurisdiction,
   identity templates). Defaults live in exactly one place (the settings
   spec) and are overridable per deployment without code changes.
2. **Account granularity.** One Starter billing account per Sub Subscriber
   per currency, referenced by a policy-rendered `external_account_ref`
   (default template `subscriber:{subscriber_id}`). A person is not a
   billing account: linked Subscribers (radio + fiber, or several accounts
   under one person) keep independent ledger accounts, balances and
   dunning. Person identity travels as the obligation `subject_ref`
   (party identity where present); service heterogeneity travels as
   `service_ref` and policy-mapped `charge_component`. Reseller
   consolidated billing keeps its own account mapping lane and is not
   forced into Subscriber granularity.
3. **Policy families.** The surface covers, at minimum: account reference,
   obligation identity (source system/kind, contract line/version
   templates, charge component by service kind, price version), tax
   snapshot policy (jurisdiction, policy id/version, percent-to-fraction
   conversion), due-date terms derivation, collection timing (billing
   mode mapping), numbering series per document kind, document profile,
   locale/timezone defaults, and accepted settlement confirmation
   evidence. Each family is a small typed resolver; policies compose, they
   do not inherit behaviour from legacy writers.
4. **No silent constants in proofs or wiring.** Composition proofs and
   migration slices must present policy-resolved values (or, in tests,
   explicitly fixtured policy values). A new hardcoded Starter-contract
   literal in runtime code is a defect against this ADR.
5. **Deployment profile constants remain declarations, not policies.**
   `app/composition.py` profile posture (allowed currencies, locales,
   legal authority, provider seams) stays versioned deployment metadata;
   policies read their defaults from settings, and the commercial
   provider switch remains the separately gated cutover event.

## Invariants

- The same Subscriber always resolves to the same Starter billing account
  reference for a given currency; the mapping is bijective per
  (account, currency).
- Changing a settings value never rewrites already-issued Starter facts;
  policies shape new commands only (issued documents carry frozen
  snapshots).
- Policy resolution is deterministic and side-effect free apart from
  settings reads; it performs no money-state writes.
- Linked accounts never share a ledger account by default; any future
  consolidation is an explicit policy change, not an identity accident.

## Consequences

- The issuance slice can declare obligation-identity semantics once, as
  policy defaults, instead of blocking on shadow contract data that the
  admin lane never produced; operations can tune conventions (series,
  terms, jurisdiction) per deployment.
- Settings surface grows by a bounded set of `starter_*` billing keys;
  operators own their values after cutover.
- Rejected alternative: hardcoded synthetic constants per call site —
  rejected because they fracture under linked-account and reseller
  shapes and cannot be tuned without releases.
- Rejected alternative: one billing account per person/party — rejected
  because it couples independent services' balances and enforcement.

## Migration and cutover

- Old owner and paths: literals and render-time lookups across Sub billing
  services; legacy writers remain authoritative until their slice gates.
- New owner and paths: `starter_policies` resolvers consumed by thin-client
  slices in order (pay-link intent, issuance lanes, settlement,
  opening assembly).
- Backfill/repair: none; policies affect new commands only.
- Shadow or verification phase: composition proofs on migrated
  PostgreSQL (#3677, #3681, #3683, #3684) plus per-slice parity checks.
- Cutover gate and evidence: per-slice gates under ADR 0007; this ADR
  moves no authority by itself.
- Fallback retirement: policy defaults that exist only to bridge legacy
  evidence gaps (for example manual-override due terms) retire when the
  owning evidence source lands.
- Schema contract step: none (settings are data, not schema).

## Verification

- Unit tests pin every policy default and its settings override.
- The money-writer and thin-client ratchets bound where policy wiring may
  live; the commercial composition allowlist registers each runtime
  Starter import the policies enable.
