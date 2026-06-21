# Customer KYC — Design

Status: **Design / proposed** · Owner: TBD · Last updated: 2026-06-15

## 1. Goal

Turn the one-off "verify your email" flow into a **per-customer KYC profile** that
tracks the verification state of every identity-bearing attribute — **email,
phone, service address, and identity (NIN)** — rolls them up into a single
**KYC level**, and lets that level **gate** sensitive actions. Identity (NIN)
verification is designed around a **pluggable provider seam** so a real provider
(Mono is already the shape on disk) can be switched on later without schema or
flow changes; until then it runs in **manual / admin** mode.

Non-goals (initial): biometric/liveness checks, document OCR, AML/PEP screening,
sanctions lists. The model leaves room for them as additional channels later.

## 2. What already exists (build on, don't duplicate)

| Channel | Today | Reuse |
|---|---|---|
| **Email** | `Subscriber.email_verified` + verify/resend (JWT link), re-arm on change | done — becomes the `email` channel as-is |
| **Identity (NIN)** | `SubscriberNINVerification` (Mono-shaped: `nin`, `status` pending/success/failed, `is_match`, `match_score`, `mono_response`, `failure_reason`, `verified_at`); `nin_verifications.py` persistence helpers. **No provider call wired.** | becomes the `identity` channel; wrap the provider call behind an interface |
| **Phone** | `Subscriber.phone` (nullable). **No verification.** | new `phone` channel (in-house SMS OTP) |
| **Address** | `Subscriber.service_address` → `Address`; self-hosted **Nominatim** geocoding (live) | new `address` channel (proof upload / geo-match / confirmed-at-install) |
| Infra | `record_audit_event`; `credential_crypto` (Fernet) used for card tokens; SMS stack (**queue runner currently OFF**) | audit every transition; encrypt NIN at rest; OTP rides SMS |

## 3. Concepts

- **Channel** — one verifiable attribute: `email | phone | address | identity`.
  Extensible (future: `document`, `bvn`, …).
- **Channel status** — `unverified → pending → verified | failed | expired`.
  (Re-uses the existing NIN `pending/success/failed`; `success`≡`verified`.)
- **KYC level** — a derived rollup over channel statuses:
  - **L0 — Unverified**: nothing verified.
  - **L1 — Contactable**: email **and** phone verified.
  - **L2 — Located**: L1 **and** address verified.
  - **L3 — Identified (full KYC)**: L2 **and** identity (NIN) verified.
  Level definitions are config-driven (which channels each level requires) so the
  ladder can be retuned without code.
- **Gate** — a named action that requires a minimum KYC level
  (e.g. `service_activation`, `vtu_high_value`, `reseller_payout`). Gates are
  config + a single `require_kyc_level(subscriber, gate)` check; **all default to
  L0 (off)** so KYC is observe-only until explicitly enforced.

## 4. Data model

Reuse existing per-channel detail where present; add the two missing detail
tables; add **one denormalized rollup** for fast reads and gating.

### 4.1 Rollup — `subscriber_kyc` (1:1 with Subscriber)
```
subscriber_id        FK → subscribers.id (unique, cascade)
email_status         enum channel_status   (mirrors Subscriber.email_verified)
phone_status         enum channel_status
address_status       enum channel_status
identity_status      enum channel_status
kyc_level            enum (l0..l3)          -- derived, stored for indexing/gating
last_evaluated_at    timestamptz
created_at / updated_at
```
Rollup is **derived**, never authoritative: a `recompute_kyc(subscriber_id)`
service reads the channel sources (below), recomputes statuses + level, writes
the row, and emits an audit event + `kyc.level_changed` domain event on change.
Called after any channel transition. (Alternatively computed-on-read; stored is
chosen so admin lists and gates can filter/sort by level cheaply.)

### 4.2 Channel sources
- **email** — `Subscriber.email_verified` (already authoritative). No new table.
- **identity** — `SubscriberNINVerification` (exists). `latest_nin_verification`
  drives `identity_status` (`success→verified`, `failed→failed`, else `pending`).
- **phone** — new `subscriber_phone_verifications`:
  ```
  id, subscriber_id FK, phone (E.164), code_hash, status, attempts,
  expires_at, verified_at, created_at
  ```
  In-house OTP; code stored **hashed** (never plaintext), short TTL, attempt cap
  + rate limit (mirror MFA/auth lockout patterns).
- **address** — new `subscriber_address_verifications`:
  ```
  id, subscriber_id FK, address_id FK, method enum(proof|geo|field),
  status, proof_file_url, geo_lat, geo_lon, geo_distance_m,
  reviewed_by, review_note, verified_at, created_at
  ```
  Three acceptable methods: **proof** (utility bill/photo upload → admin review,
  reuse the bank-transfer-proof upload+review machinery), **geo** (Nominatim
  forward-geocode of the stored address vs. a captured pin within a threshold),
  **field** (installer confirms at activation). Any one satisfies the channel.

> Migration adds: `channel_status` + `kyc_level` enums, `subscriber_kyc`,
> `subscriber_phone_verifications`, `subscriber_address_verifications`. The NIN
> table and `email_verified` are untouched. Backfill `subscriber_kyc` from
> existing `email_verified` + latest NIN per subscriber.

## 5. Provider seam (the "NIN via a provider in future" hook)

Identity verification hides behind one interface so the source is swappable:

```python
class IdentityProvider(Protocol):
    key: str
    def verify_nin(self, *, nin: str, subscriber: SubscriberRef) -> IdentityResult: ...
    # IdentityResult: status, is_match, match_score, raw (→ mono_response), failure_reason
```

- `ManualIdentityProvider` (**default now**) — records the NIN as `pending`; an
  admin marks `success/failed` from the review queue. No external call.
- `MonoIdentityProvider` (**future**) — calls Mono, maps the response into the
  existing `mono_response/is_match/match_score` columns (the table is already
  shaped for it), sets `success/failed`, may be async (provider webhook →
  `recompute_kyc`).
- Future others (Dojah/VerifyMe/Smile/QoreID) implement the same Protocol.

Selected by a single setting `kyc_identity_provider` (`manual` default). Swapping
providers is **config-only**; no schema/flow change. Phone OTP is in-house (rides
the existing SMS provider), not a third-party identity provider — kept separate.

## 6. Flows

- **Email** — unchanged (link verify/resend; re-arm on email change).
- **Phone** — customer enters/edits phone → request OTP (SMS) → enter code →
  `verified`. Re-arm on phone change. Depends on the **SMS runner being ON**.
- **Address** — customer picks a method: upload proof (→ pending → admin review),
  or confirm location on a map (→ geo-match auto-verify within threshold, else
  pending); or installer confirms at activation (`field`).
- **Identity (NIN)** — customer enters NIN → `get_or_create_pending` →
  provider.verify_nin(): manual mode parks it for admin; provider mode calls out
  and resolves (sync or via webhook). NIN is **encrypted at rest** and shown
  masked (`***••6789`).
- Every transition → `recompute_kyc` → audit + `kyc.level_changed` event (which
  can fire notifications and re-evaluate gates).

## 7. Surfaces

- **Customer (web + mobile)** — a **KYC card** on the profile: one row per channel
  (status chip + action: Verify / Resend / Upload / Enter NIN), and the overall
  **KYC level** with "what this unlocks". Reuses the email tile we just built as
  the email row.
- **Reseller** — same card for the reseller's own login subscriber (parity with
  the email-verify tile already shipped). Their *customers'* KYC is **read-only**
  in reseller account views (no acting on behalf, matching the existing read-only
  view-as posture).
- **Admin** — a **KYC review queue**: pending address proofs and pending/failed
  NIN attempts, with approve/reject + reason; per-subscriber KYC panel; filter the
  customer list by `kyc_level`. NIN reveal is staff-permissioned + audited (mirror
  the PPPoE-reveal pattern).

## 8. Gating (opt-in)

`require_kyc_level(db, subscriber_id, gate) -> Decision`. Gate→min-level map in
settings, **all L0 by default**. Candidate gates (enable per business decision):
`service_activation` (L2/L3), `vtu_high_value` (L3 above a ₦ threshold),
`reseller_payout` (L3), `change_payout_bank` (L3). Enforcement points call the
single check; UI shows "verify X to continue" with a deep link to the KYC card.

## 9. Security & privacy

- **NIN is sensitive PII**: encrypt at rest (Fernet, reuse `credential_crypto`),
  store/display masked, never log; restrict provider `mono_response` to required
  fields; staff reveal permissioned + audited; define a **retention policy**
  (e.g. drop raw provider payloads after N days, keep status + match score).
- OTP codes hashed, TTL'd, attempt-capped, rate-limited (reuse auth lockout).
- Address proof files: same access controls as bank-transfer proofs.
- All transitions audited; `kyc_level` changes are first-class audit events.
- Data-subject: support "what KYC data do you hold / delete it" for compliance.

## 10. Phasing

1. **P1 — Unify & surface (no new provider).** `channel_status`/`kyc_level`
   enums, `subscriber_kyc` rollup + `recompute_kyc`, backfill from email+NIN,
   customer/reseller KYC card, admin KYC panel + NIN review (manual provider),
   gates wired but all L0. *Mostly glue over existing data.*
2. **P2 — Phone OTP.** `subscriber_phone_verifications` + SMS OTP flow.
   **Prereq: turn the SMS queue runner ON.**
3. **P3 — Address verification.** `subscriber_address_verifications` + proof
   upload/review, Nominatim geo-match, installer `field` confirmation.
4. **P4 — Real NIN provider.** Implement `MonoIdentityProvider` behind the seam,
   flip `kyc_identity_provider=mono`, webhook → recompute. No schema change.
5. **P5 — Enforce gates.** Turn specific gates from L0 to required levels per
   business policy.

## 11. Open decisions

- **KYC ladder**: are email+phone enough for L1, or is phone optional? Confirm the
  channel→level map.
- **Address method**: which of proof / geo / field do we accept, and is geo-match
  alone sufficient or always admin-reviewed?
- **NIN provider**: confirm Mono as the first provider + commercial/throughput;
  sync vs. webhook; cost per check (affects whether we verify everyone or gate it).
- **Enforcement**: which gates, at which levels, and is KYC **required before
  activation** at launch or retro-applied to existing subscribers?
- **Existing subscribers**: backfill posture — treat imported customers as L0 and
  prompt, or grandfather to a level?
- **Reseller KYC**: do resellers themselves need L3 before payout (recommended)?
