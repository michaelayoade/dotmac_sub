# Project State

*Last updated: 2026-03-14*

This file tracks in-progress work, current priorities, and known issues to provide continuity across development sessions.

---

## In-Progress Features (Uncommitted)

### 1. Support Ticket Module — NEW MODULE
**Status:** Code complete, needs lint fixes and testing
**Files (untracked):**
- `app/models/support.py` — SupportTicket, TicketComment, TicketAttachment models
- `app/services/support.py` — Full CRUD + assignment + escalation logic (~38KB)
- `app/api/support.py` — REST endpoints
- `app/schemas/support.py` — Pydantic schemas
- `app/web/admin/support_tickets.py` — Admin web routes
- `templates/admin/support/tickets/` — index, detail, new, table, components
- `templates/customer/support/` — Customer portal ticket views
- `alembic/versions/c1f4d6a8b9e2_add_support_ticket_module.py` — Migration
- `tests/test_support_services.py` — Service tests
**Known issues:** 2 ruff lint errors (unused imports)
**Depends on:** Nothing — standalone module

### 2. ONT Provisioning Profiles — NEW FEATURE
**Status:** Code complete, needs integration testing
**Files (untracked):**
- `app/services/network/ont_provisioning_profiles.py` — Profile CRUD (~13KB)
- `app/services/network/ont_profile_apply.py` — Apply profiles to ONTs (~8.5KB)
- `app/services/network/vendor_capabilities.py` — Vendor/model capability registry (~10KB)
- `app/services/web_network_ont_provisioning_profiles.py` — Web service
- `app/services/web_network_vendor_capabilities.py` — Web service
- `app/web/admin/network_ont_provisioning_profiles.py` — Admin routes
- `app/web/admin/network_vendor_capabilities.py` — Admin routes
- `app/tasks/ont_provisioning.py` — Background task for profile application
- `templates/admin/network/provisioning-profiles/` — index, form
- `templates/admin/network/vendor-capabilities/` — index, form
- `alembic/versions/d2e5f7a9b1c3_add_ont_provisioning_profiles.py`
- `alembic/versions/e3f6a8b0c2d4_add_vendor_model_capabilities.py`
- `alembic/versions/f4g7b9c1d3e5_add_provisioning_profile_fks.py`
**Depends on:** ONT observed runtime fields migration

### 3. ONT Observed Runtime Fields
**Status:** Migration written, model changes staged
**Files:**
- `alembic/versions/a8b9c0d1e2f3_add_ont_observed_runtime_fields.py` (untracked)
- `app/models/network.py` (modified)
**Depends on:** Nothing

### 4. OLT Polling Enhancements
**Status:** Modified, part of broader network improvements
**Files (modified):**
- `app/services/network/olt_polling.py`
- `app/tasks/olt_polling.py`
- `tests/test_olt_polling_service.py` (new test file)

### 5. Reseller Module Enhancements
**Status:** Modified across routes, services, and templates
**Files (modified):**
- `app/services/web_admin_resellers.py`
- `app/web/admin/resellers.py`
- `templates/admin/resellers/detail.html`
- `templates/admin/resellers/index.html`
- `templates/admin/resellers/reseller_form.html`
- `tests/test_web_admin_resellers_service.py` (new)

### 6. Customer Portal Improvements
**Status:** Modified across multiple files
**Files (modified):**
- `app/services/customer_portal_context.py`
- `app/services/web_customer_actions.py`
- `app/web/customer/auth.py`
- `app/web/customer/routes.py`
- `templates/customer/auth/login.html`
- `templates/customer/billing/arrangement_detail.html`
- `templates/customer/billing/arrangement_form.html`
- `templates/customer/profile/index.html`
- `templates/customer/services/detail.html`
- `templates/customer/services/index.html`
- `templates/layouts/customer.html`
- `tests/test_web_customer_actions.py` (new)

### 7. Subscriber & Catalog Enhancements
**Status:** Modified
**Files (modified):**
- `app/models/catalog.py`, `app/models/provisioning.py`
- `app/services/web_subscriber_actions.py`, `web_subscriber_details.py`, `web_subscriber_forms.py`
- `templates/admin/subscribers/` — detail, form, _table
- `templates/admin/catalog/` — subscription_detail, subscriptions
- `templates/admin/customers/form.html`

### 8. Network / ONT Enhancements
**Status:** Modified
**Files (modified):**
- `app/services/network/olt.py`, `_resolve.py`, `ont_actions.py`, `ont_tr069.py`
- `app/services/web_network_olts.py`, `web_network_ont_charts.py`, `web_network_ont_tr069.py`
- `app/services/web_network_core_devices_inventory.py`, `web_network_core_devices_views.py`
- `app/web/admin/network_olts_onts.py`
- `templates/admin/network/` — olts (index, detail), onts (index, detail, _charts_partial), monitoring, network-devices
- `app/schemas/network.py`
- `tests/test_networking_feature_p0.py`

### 9. Cross-cutting Changes
**Modified shared files:**
- `app/main.py` — Router registration for new modules
- `app/models/__init__.py` — Model imports
- `app/web/admin/__init__.py` — Admin router registration
- `app/tasks/__init__.py` — Task registration
- `app/api/search.py` — Search scope additions
- `app/services/audit_helpers.py` — Audit support
- `app/services/table_config.py` — Table config for new views
- `app/services/typeahead.py` — Typeahead additions
- `app/services/radius_reject.py` — RADIUS reject handling
- `app/services/events/handlers/enforcement.py` — Enforcement changes
- `templates/components/navigation/admin_sidebar.html` — Sidebar links
- `static/js/dynamic-table-config.js`

---

## Known Issues

| Issue | Severity | Location |
|-------|----------|----------|
| 476 mypy warnings/errors (mostly in tests, scripts, migrations) | Low | `mypy_errors.txt` |
| 2 unused imports in support module | Low | `app/api/support.py`, `app/services/support.py` |

---

## Pending Migrations (Not Yet Applied)

| Migration | Description |
|-----------|-------------|
| `a8b9c0d1e2f3` | Add ONT observed runtime fields |
| `c1f4d6a8b9e2` | Add support ticket module |
| `d2e5f7a9b1c3` | Add ONT provisioning profiles |
| `e3f6a8b0c2d4` | Add vendor model capabilities |
| `f4g7b9c1d3e5` | Add provisioning profile FKs |

**Migration order matters:** `a8b9c0d1e2f3` → `d2e5f7a9b1c3` → `e3f6a8b0c2d4` → `f4g7b9c1d3e5` (provisioning chain). `c1f4d6a8b9e2` (support) is independent.

---

## Feature Roadmap Context

From `docs/feature_improvements/00_INDEX.md` — **~834 outstanding items** across 11 sections.

**Current phase:** Phase 1 (Foundation) + Phase 2 (Operational Tools) overlap
- Support tickets → Phase 2 (Helpdesk)
- ONT provisioning profiles → Phase 1 (Network enhancements)
- Customer portal improvements → Phase 1 (Customer detail)
- Reseller enhancements → Phase 1

**Priority features not yet started:**
- Finance dashboard (MRR, ARPU, aging reports)
- Bank statement import with payment pairing
- Mass messaging with recipient targeting
- Full IPAM (IPv4/IPv6 subnet management)
- Network monitoring dashboard KPIs

---

## Uncommitted Artifacts (Non-code)

| File | Purpose |
|------|---------|
| `Smartolt Crud.zip` | Reference screenshots for SmartOLT features |
| `Splynx and Smartolt Dotmacsubs.zip` | Reference screenshots |
| `tariff plans screenshots.zip` | UI reference for tariff plan pages |
| `screenshots/` | Various UI screenshots |
| `uploads/invoices/` | Test invoice uploads |
| `mypy_errors.txt` | Snapshot of mypy output |

These should be `.gitignore`d, not committed.

---

## Recent Commit History (for context)

| Commit | Description |
|--------|-------------|
| `a5ea78a` | Platform updates and testing fixes |
| `4cd7028` | Currency formatting, header overflow, speedtest, error handling |
| `6fb717d` | RADIUS/NAS enforcement: CoA-Update, connection-type provisioning |
| `4fa42ba` | SmartOLT/Splynx features: ONU types, speed profiles, FUP, plan categories |
| `655686a` | Ping latency, uptime/backup maps on device list |
| `b2ff4e3` | Module decomposition, networking features, security hardening |
