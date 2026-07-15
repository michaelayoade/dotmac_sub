# Audit closure checklist — all modules

Master tracker to close out the 14-domain UX-polish/operator-control audit
program plus the admin-authz security review. Compiled 2026-07-03 from every
audit doc's remediation-status section (all 14 domains are past their
required-tier remediation; this is the remaining tail).

**How to use**: tick items with the PR number (`- [x] … (#123)`). When a tier-5
item is consciously dropped, mark it `[won't-do]` with a word of rationale
instead of leaving it unchecked forever. Source of detail: each domain's
`*_UX_POLISH_AUDIT.md` appendix.

---

## Tier 0 — verify-and-tick (already believed fixed; confirm, then close)

- [x] SECURITY #2: connector `auth_config` encrypted at rest — code verified
  (`EncryptedJSON`, #534); prod DB check found 2 pre-migration rows still
  PLAINTEXT (one held a live DB password) → re-encrypted + round-trip verified
  2026-07-03; security doc updated
- [x] SECURITY #12: header/metadata masking verified in code
  (`mask_secret_values`, #540); security doc updated 2026-07-03
- [x] SECURITY #13: scopes model + enforcement verified in code (#539/#541);
  security doc updated 2026-07-03

## Tier 1 — remaining correctness / security-adjacent work

- [ ] SECURITY (systemic): extend the build-failing route-permission arch test
  from `/api/v1`-only to **all `/admin` web routers**, with a quarantine list
  burned down over time
- [x] SECURITY: API-key hashing → HMAC-SHA256 keyed with a derived subkey of
  the credential-encryption key. Dual-read + rehash-on-use: legacy sha256 rows
  keep authenticating and upgrade to HMAC on next call — NO key rotation, NO
  external (ERP/CRM) changes, zero downtime. Legacy sha256 verify path can be
  retired once all active keys have re-authenticated (2 prod keys).
- [ ] BILLING: currency cleanup remainder — forms, adapters, **Flutterwave init
  (blocks non-NGN today)**, integrity SQL, `crm_billing_push` (`os.getenv`)
- [ ] APP-INTEGRATIONS P-C: fix fake observability — connector health is
  unconditionally green, API-key `last_used_at` never stamped, activity-log
  latency always "-", Connector column shows raw UUID instead of name
- [ ] AUTH (security-review pointer): portal login throttle is per-worker
  in-memory (`limit=10/900s`) — move to a distributed limiter + settings
- [ ] AUTH (security-review pointer): reset-token passed in redirect URL
  (`web_auth.py:196`) — move out of the URL

## Tier 2 — structural / settings-system unification

- [x] SYSTEM-CONFIG C-1: migrate the remaining **billing/collections bespoke
  string-save forms** to typed `settings_spec` (the two-settings-systems split)
  — all five saves (`save_billing_config`, `save_direct_bank_transfer_config`,
  `save_reminders`, `save_billing_notifications`, `save_plan_change`) now pass
  `use_specs=True`; registered keys get spec type coercion/validation, and the
  seven `direct_bank_transfer_*` keys gained specs (they have portal readers).
  Reader-less keys (payment_period, invoice/proforma toggles, the reminder/
  blocking-wave waves) are intentionally left un-spec'd to avoid orphans.
- [ ] SYSTEM-CONFIG: bespoke-save validation/feedback consistency for the
  remaining untyped forms (monitoring/preferences/portal/radius already typed)
- [ ] BILLING: bulk/scheduled money-job **result history** surface (autopay,
  reconcilers, runners: last-run/result in-app) + raw-exception copy cleanup
- [ ] BILLING: remaining policy thresholds → settings (outside the resolved
  autopay/arrangements/extensions/sweep/gateway-timeout/AR-bucket/dedupe set)
- [ ] RESELLER C-2: per-reseller "restrict catalog to assigned offers" flag +
  global default (catalog is default-open today)
- [ ] CROSS-CUTTING: shared currency + timezone display helpers, then sweep the
  known hardcodes: catalog customer-detail `₦` + calculator totals,
  dunning/arrangement tz+currency, and the reseller UTC-label-only branch
- [x] CROSS-CUTTING: "no dead controls" lint — settings-key half done via
  `tests/architecture/test_no_orphan_settings.py`: every registered
  `SETTINGS_SPECS` key must have a reader or the build fails. Surfaced **35
  pre-existing dead keys** (burn-down `_KNOWN_ORPHAN_SETTINGS`) — see Tier 4.
  The "every form field maps to a consumer" half is deliberately deferred (too
  noisy for CI without a large allowlist).
- [ ] TIER-4 follow-up: burn down the 35 orphan settings the lint captured
  (wire a consumer or drop from SETTINGS_SPECS). Clusters: collections
  `prepaid_deactivation_*` / `prepaid_warning_*` / `prepaid_skip_*` (dead
  prepaid-dunning settings page), `meta_*` comms, `hotspot_*`, subscriber
  `account_number_*`, several network/monitoring poll intervals, vendor
  quote/bid thresholds, `vendor_*session*` auth.

## Tier 3 — product decisions needed (blocked on a human call)

- [ ] CATALOG C-4: change-plan effective timing — offer **next-cycle** option
  alongside instant-with-proration? (currently hardcoded instant)
- [ ] RESELLER C-1: partner economics — commission rate/markup, credit limit,
  payout terms fields + defaults (data-model decision)
- [ ] CUSTOMER-PORTAL: direct appointment reschedule/cancel workflow (replaces
  prefilled-ticket flow) — accept customer-side scheduling changes?
- [ ] CATALOG: bulk ops include suspended subscriptions via a configurable
  included-statuses set? (bulk-tariff/bulk-change-plan are active-only today)

## Tier 4 — deferred P2 tails (real work, low urgency)

### Networking (largest bucket — one settings-focused PR could take most of it)
- [ ] WireGuard tunables (4): `wireguard_default_keepalive` (25), per-server
  `router_api_ssl_verify` (currently off even with SSL on), log retention
  (90d), handshake-online window (180s)
- [ ] Threshold/TTL settings (~5): CoA neg-TTL 15m / burst 10 / open-session
  2h; VPN-tunnel-stale 3m; backup-stale 24h; ACS validate timeout 5s;
  availability window 365d + uptime badge cutoffs vs `infra_sla_target_percent`
- [ ] Deployment identifiers: captive `block_chain`/`oss_ports`, ONT
  `tag_transform` per-OLT, HTTP-mgmt-port validation, genieacs task-wait
  knobs, CPE LAN-port range by vendor
- [ ] Async/feedback polish batch (~8 small): push-detail poll, sessions
  "as of" label, import-PPPoE confirm, TR-069 task result poll + auto-refresh,
  map loading/empty/tile-error states, IP-ops busy state, VPN copy/regen
  toasts, fiber approve/reject disable+toast

### Catalog & services
- [ ] FUP rule **impact preview** (how many subscribers would a new rule
  throttle/block right now)
- [ ] Calculator VAT/proration accuracy (VAT on subtotal only; one-time fees
  VAT-free; "First Bill" ignores proration)
- [ ] GiB-computed-but-labeled-GB (a "100 GB" rule is ~107 GB)
- [ ] Tunable thresholds: serviceable radius 1.5km, service-request SLA/aging,
  password-reset throttle 3/hr, PPPoE-reveal 30/hr, 60d staleness
- [ ] Appendix tail (~10 small: usage price type in UI, orphan bulk routes,
  page-size caps, service-intent pppoe hardcode, etc. — see doc appendix)

### Billing
- [ ] AR-aging period selector breadth (month/quarter) + tz edge polish
- [ ] Appendix tail (~15 small: truncation footers on capped lists, reconciler
  last-run surfacing, `billing_enabled_expected` registration, partial-pay
  invoice field, statement-period selector + paperless, TTL settings, stale
  Pay-button label, manual-payment min/max — see doc appendix)

### Reports & dashboards
- [ ] Truncation footers / count banners on every capped table & export
  (row caps 200/100/20; CSV cap 5000)
- [ ] Default window/row-cap as settings (days=30, export ranges, network
  `hours` without UI) — possibly a reports settings group

### App integrations
- [ ] P-D: revoke/create flash results + system-user-owned API keys
- [ ] P2 tail: key rotate flow, max-keys/max-TTL/rate-limit per key,
  probe-timeout + catalog-version settings, schedule-restart hint, copy
  feedback, friendly error masking

## Tier 5 — optional / recommended-only (close or consciously drop)

- [ ] CRM (3): warn on 0 CRM-linked subscribers in billing push; conflicting
  IDs in ambiguous-identity logs; configurable require-name fuzzy toggle
- [ ] NOTIFICATIONS (2): client-side template lint before submit; queue
  batch-size + reclaim-category controls
- [ ] SUPPORT (4): searchable ticket picker for link/merge; scheduled
  SLA-breach materialization; richer team-management views; broader tz sweep
---

## Suggested closure order

1. **Tier 0** (an hour: verify three security items, update the security doc)
2. **Tier 1** (each item is a small-to-medium PR; the arch-test extension pays
   for itself immediately)
3. **Tier 2** — do the two cross-cutting helpers first (currency/tz, dead-
   controls lint); they shrink several Tier 4 tails to mechanical sweeps
4. **Tier 3** — needs product answers; batch the five decisions in one sitting
5. **Tier 4** — one PR per domain bucket (networking-settings PR is the big one)
6. **Tier 5** — tick or `[won't-do]` each; empty this list to declare the
   audit program closed
