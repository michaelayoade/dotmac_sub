# System / configuration / legal / GIS — UX-polish & operator-control audit

**Date:** 2026-06-29
**Method:** single-agent read-only review of the settings control plane + legal,
GIS, whats-new, design-system, admin-hub. (The billing settings form is covered in
`BILLING_UX_POLISH_AUDIT.md`.)
**Status:** remediation in progress via draft PR #518. Part of the remaining-module audit series. This is the
natural home for the cross-audit **"single-source-of-truth + no dead controls"**
theme.

## Remediation status

**Last updated:** 2026-06-29
**Tracking PR:** #518 (`audit/system-config-remediation`)

### Resolved in current draft

- Legal mutating routes now declare `system:write` route-level permissions for
  create, edit, upload, delete-file, publish, unpublish, and delete actions.
- Legal create/update/upload-file/delete-file/publish/unpublish/delete actions now
  emit `legal_document` audit events after successful mutation.
- Legal Publish and Unpublish actions now require an explicit browser confirmation.

### Still open

- Dead config pages and toggles: Data Retention, Monitoring drift, and the largely
  inert preference/subscriber/portal/CPE/IPv6 key groups.
- Unifying bespoke config saves with the typed/cached `settings_spec` system.
- GIS route-guard asymmetry and GIS sync last-run/result observability.
- Whats-new publish confirmation and invalid-status feedback cleanup.
- Geocoding provider/rate-limit controls and broader bespoke-save validation
  consistency.

### Verification

- `poetry run pytest tests/test_admin_route_permissions.py tests/test_legal_services.py`
  - Result: `46 passed`

## What this audit is

Two tracks (definition in `NETWORKING_UX_POLISH_AUDIT.md`). The headline is
structural: the settings control plane runs on **two parallel, unreconciled
systems** — the typed/validated/cached `settings_spec.py` registry (~400 keys) that
drives the generic UI and `resolve_value()`, and bespoke context-builders in
`web_system_config.py` that read/write `DomainSetting` rows as **untyped strings**,
bypassing validation, the cache, and the canonical resolver. Several bespoke pages
edit keys **no consumer reads** (dead config).

## Acceptance criteria (system/config-specific)

1. One settings system: every editable key is a registered `settings_spec` entry
   (typed/validated/cached); bespoke pages render from spec.
2. No dead config: every settings page changes real behavior, or it's removed.
3. The page that displays a threshold edits the *same* key the consumer reads (no
   displayed-vs-consumer drift).
4. Customer-visible publishing (legal, whats-new) is confirmed and audited.
5. Long-running ops (GIS sync) surface last-run/result; mutating routes are guarded.

## Cross-cutting themes

### CONTROL (primary)

**C-1. Two settings systems with divergent guarantees.** `_save_settings`/
`_read_settings` persist everything as `value_type=string`, no min/max/allowed/
required enforcement, and read via a private path — keys saved here are **invisible
to `settings_spec.resolve_value()`** (returns None) and to the Redis cache
(`app/services/web_system_config.py:42-72`). → migrate these key-groups into
`settings_spec` (typed/validated/cached); render bespoke pages from spec.

**C-2. Dead config (the signature, same footgun the codebase already deleted for
"Finance Automation").**
- **Data Retention** page (`RETENTION_KEYS`: admin_logs_months=6, …) has **zero
  consumers** — inert; looks like it governs pruning, changes nothing (`web_system_config.py:158-188`)
- `PREFERENCE/SUBSCRIBER/PORTAL/CPE/IPV6` key groups are largely **dead toggles** —
  only `force_2fa` + `selfcare_domain` are consumed; `login_format`,
  `max_search_results`, `show_payment_due`, `dhcp_lease_time`,
  `ipv6_auto_assign_enabled`, etc. are read nowhere (`web_system_config.py:78,97,127,616,818`)
- **Monitoring** page edits `cpu_warn_pct`/`mem_warn_pct`/`interface_warn_pct` (no
  consumer) while the live health evaluator reads the **different** spec keys
  `server_health_mem_warn_pct`/`_disk_warn_pct`/`network_health_warn_pct` —
  displayed-vs-consumer drift, no disk/load field at all (`web_system_config.py:647-768` vs `web_admin_dashboard.py:225-247`)

**C-3. Locked provider / missing rate-limit.** Geocoding `provider` locked to
`{"nominatim"}` with no rate-limit/throttle setting (only `timeout_sec`) — adding a
fallback or honoring 1 req/s needs code (`settings_spec.py:899-906`).

### POLISH

**P-A. Customer-visible publishing without confirm/audit.**
- Legal create/update/**publish/unpublish/delete** emit **no audit event** despite
  app-wide audit infra — publishing TOS is legally significant with no who/when
  record (`app/services/legal.py:122-153`, `web_legal.py:225-242`)
- Legal Publish/Unpublish submit with **no confirm** (only Delete confirms)
  (`templates/admin/system/legal/detail.html:179,187`)
- Whats-new status→active/featured publishes a customer-visible announcement with no
  confirm; invalid status redirects to `?status=invalid`, a magic param the index
  reuses as a filter (`app/web/admin/system_whats_new.py:212-237`)

**P-B. Run observability.** GIS sync (`queue_sync`) is fire-and-forget — `SyncResult`
counts discarded, no last-run timestamp, no error surface, no status on the GIS
page; combined with the destructive `deactivate_missing` flag an admin can't tell
what it deactivated (`app/services/gis_sync.py:79-102`).

**P-C. Validation/feedback inconsistency.** The scheduler save validates
(`is_valid_cron`, interval≥1) and round-trips errors; the bespoke config saves
(`save_preferences`/`save_radius_config`/`save_cpe_config`) do no validation and give
no explicit confirmation (`app/web/admin/system.py:2857-2877` is the good pattern).

### Security note (out of the two tracks)

Route-guard asymmetry: in **legal** (`app/web/admin/legal.py`), `list`/`publish` are
guarded but `create`/`update`/`upload`/`delete-file`/`unpublish`/**`delete`** declare
no `require_permission`; in **GIS** (`app/web/admin/gis.py`), `location_create` is
guarded but `update`/`delete` and all area/layer mutations are not. Verify against
the mount-registry RBAC layer; the asymmetry (publish guarded, delete not) is the
finding.

## Priority

| Tier | Items |
|------|-------|
| **P0** | Dead config pages that look load-bearing but aren't — Data Retention inert, Monitoring `*_warn_pct` orphan vs real keys, dead toggle groups (C-2); legal publish/unpublish/delete no audit (P-A) |
| **P1** | Unify the two settings systems / validate-on-save (C-1); confirm before customer-visible publish (legal + whats-new) (P-A); GIS sync observability (P-B); route-guard asymmetry legal/gis — verify vs mount-registry |
| **P2** | geocoding rate-limit + provider fallback (C-3); standardize bespoke-save validation/feedback (P-C); whats-new magic `?status=` param |

## Appendix — full findings
- [CONTROL] (High) `app/services/web_system_config.py:42-72` — `_save_settings`/`_read_settings` persist string-typed, unvalidated, invisible to `resolve_value()`/cache → migrate to settings_spec or validate against spec on save [recommend]
- [CONTROL] (High) `web_system_config.py:158-188` — Data Retention keys have zero consumers; page inert → wire to cleanup tasks or delete [recommend]
- [CONTROL] (High) `web_system_config.py:78,97,127,616,818` — PREFERENCE/SUBSCRIBER/PORTAL/CPE/IPV6 largely dead toggles (only force_2fa + selfcare_domain consumed) → audit each; delete inert or implement consumers [recommend]
- [CONTROL] (High) `web_system_config.py:647-768` vs `web_admin_dashboard.py:225-247` — Monitoring page edits `*_warn_pct` (no consumer) while evaluator reads `server_health_*`/`network_health_*` spec keys; no disk/load field → collapse to spec keys [recommend]
- [POLISH] (High) `app/services/legal.py:122-153` + `web_legal.py:225-242` — legal create/update/publish/unpublish/delete emit no audit event → emit audit on publish/unpublish/delete [recommend]
- [POLISH] (Med) `templates/admin/system/legal/detail.html:179,187` — Publish/Unpublish no confirm (only Delete confirms) → confirm-before-publish/unpublish [recommend]
- [CONTROL] (Med) `app/web/admin/legal.py` — guards asymmetric: publish guarded, create/update/upload/delete-file/unpublish/delete unguarded → apply `system:write` to all mutating routes [recommend]
- [CONTROL] (Med) `app/web/admin/gis.py` — `location_create` guarded but update/delete + area/layer mutations unguarded → add `gis:map:edit` to every mutating route [recommend]
- [POLISH] (Med) `app/services/gis_sync.py:79-102` + `gis/index.html` — sync fire-and-forget, counts discarded, no last-run/error surface (+ destructive `deactivate_missing`) → persist + surface last-run [recommend]
- [POLISH] (Med) `app/web/admin/system_whats_new.py:212-237` + `whats_new/index.html:104` — status→active publishes customer-visible item, no confirm; `?status=invalid` overloads filter param → confirm + proper error flash [defer]
- [CONTROL] (Low) `settings_spec.py:899-906` — geocoding provider locked to nominatim, no rate-limit setting → add `geocode_min_interval_ms` before any non-self-hosted base_url [defer]
- [POLISH] (Low) `system.py:2857-2877` (good) vs bespoke saves — bespoke config saves no validation/feedback → standardize success/error + per-field validation [defer]
