# Quality Cycle 2 — Seabone Sentinel Report
**Date:** 2026-02-27
**Scan type:** Quality
**Scope:** Full codebase (`app/`, `tests/`)

---

## Executive Summary

This scan focused on structural quality issues: dead code, missing error handling, type mismatches, function complexity, duplicate logic, and architectural anti-patterns. It did **not** re-scan security (covered by security cycle 1). The codebase is functional but carries **two critical project-wide architectural violations** that affect nearly every service file.

**Total findings: 20** (2 Critical / 8 High / 7 Medium / 3 Low)

---

## Findings by Severity

### Critical (2)

| ID | File | Issue |
|----|------|-------|
| quality-c2-1 | `app/services/` (codebase-wide) | 815+ SQLAlchemy 1.x `db.query()` calls — CLAUDE.md mandates `select()` |
| quality-c2-2 | `app/services/` (codebase-wide) | 570 `db.commit()` in services vs 78 `db.flush()` — services own transactions instead of caller |

These two findings are systemic. Every service file participates in at least one of them. Together they represent the largest maintenance and correctness risk in the codebase.

### High (8)

| ID | File | Line | Issue | Effort |
|----|------|------|-------|--------|
| quality-c2-3 | `scheduler_config.py` | 205 | `build_beat_schedule()` is 513 lines | medium |
| quality-c2-4 | `billing/reporting.py` | 189 | `get_dashboard_stats()` is 484 lines | medium |
| quality-c2-5 | `billing_automation.py` | 253 | `run_invoice_cycle()` is 268 lines | medium |
| quality-c2-6 | `enforcement.py` | 316 | N+1 query: `db.get(AccessCredential)` per session in loop | small |
| quality-c2-7 | `api/billing.py` | 943 | `async def` handlers with sync SQLAlchemy Session (paystack/flutterwave) | small |
| quality-c2-8 | `web/admin/provisioning.py` | 231 | `async def` handlers with sync DB session (bulk activate) | small |
| quality-c2-9 | `enforcement.py` | 748 | `create_engine()` never followed by `engine.dispose()` — connection pool leak | trivial |
| quality-c2-10 | `usage.py` | 582 | `except Exception: pass` silently swallows usage charge posting failures | trivial |

### Medium (7)

| ID | File | Line | Issue | Effort |
|----|------|------|-------|--------|
| quality-c2-11 | `tasks/notifications.py` | 61 | `db.commit()` inside per-notification loop — 1 DB round-trip per message | small |
| quality-c2-12 | `services/web_catalog_settings.py` | 255 | 5× bulk-delete functions silently debug-log all failures; 5× code duplication | small |
| quality-c2-13 | `api/nas.py`, `api/provisioning.py` | 50+ | 12+ endpoints use `response_model=dict` — no response validation or OpenAPI docs | medium |
| quality-c2-14 | `api/catalog.py` | 577 | Business-logic parameter aliasing duplicated in two route handlers | trivial |
| quality-c2-15 | `app/models/network.py` et al. | 226 | 148 ORM relationships missing `back_populates` — risk of DetachedInstanceError | large |
| quality-c2-16 | `app/tasks/` (20+ files) | various | 20+ local imports inside function bodies — import overhead on every call | small |
| quality-c2-17 | `services/collections/_core.py` | 1106 | Two `run()` methods of 201 and 181 lines each; uses legacy `db.query()` | medium |

### Low (3)

| ID | File | Line | Issue | Effort |
|----|------|------|-------|--------|
| quality-c2-18 | `web/admin/nas.py` | 367 | `device_update()` has 55 Form params (166 lines) — needs Pydantic form model | small |
| quality-c2-19 | `services/subscriber.py` | 445 | `import calendar` and datetime imports inside function body | trivial |
| quality-c2-20 | `services/web_billing_dunning.py` | 106 | `except Exception: continue` in bulk dunning — failures invisible | trivial |

---

## Top 3 Priority Fixes

### 1. `db.commit()` → `db.flush()` in services (quality-c2-2)
**Why first:** 570 call sites. Every route that calls a service and then conditionally raises an exception can accidentally commit partial state. This is the most dangerous correctness issue. Start with the most-called services: `auth.py`, `subscriber.py`, `billing/payments.py`, `billing/invoices.py`.

### 2. N+1 query in `enforcement.py:316` (quality-c2-6)
**Why second:** This runs on every CoA update — triggered by plan changes, suspensions, and renewals. With 100+ active subscribers each having multiple sessions, this multiplies DB queries by 100x. It's a single-file, single-function fix with immediate measurable impact.

### 3. `engine.dispose()` leak in `enforcement.py:748` (quality-c2-9)
**Why third:** Every call to `_delete_users_from_external_radius()` (triggered on subscription cancellation) leaks a PostgreSQL connection pool. This can exhaust the external RADIUS DB connection limit over time. It's a trivial fix.

---

## Comparison with Previous Scans

| Cycle | Date | Focus | Findings | Health |
|-------|------|-------|----------|--------|
| security-c1 | 2026-02-27 | Security | 34 (2C/11H/13M/3L/5 already-fixed) | 42/100 |
| quality-c2 | 2026-02-27 | Quality | 20 (2C/8H/7M/3L) | 52/100 |

Security cycle 1 found 34 issues; approximately 34 have been fixed (per the "Already Fixed" list in memory). The security posture has meaningfully improved. This quality scan reveals the codebase has substantial **architectural tech debt** that was not addressed by the security fixes.

**New findings this cycle:** All 20 are new quality issues not previously reported.
**Still open from security cycle 1:** quality-c2-7 (async route pattern) was latent before; security items like c1-31, c1-33 (rate limiting) may still be open pending PM assignment.

---

## Codebase Health Score: 52/100

**Rationale:**
- (+) Solid test coverage: 95+ test files covering most service domains
- (+) Security cycle 1 fixes have been applied; auth, encryption, SSRF largely remediated
- (+) Type hints present on most new code; Pydantic v2 patterns used in schemas
- (-) Critical: 815 legacy SQLAlchemy 1.x query calls (-10)
- (-) Critical: 570 `db.commit()` in services violating transaction ownership (-10)
- (-) High: 4 monolithic functions (200–513 lines) reducing testability (-8)
- (-) High: async/sync DB mixing in route handlers (-5)
- (-) Medium: 148 relationships missing `back_populates` (-5)
- (-) Medium: Widespread `response_model=dict` removing API contract guarantees (-5)
- (-) Low: Scattered silent exception swallowing (-5)

**Trend: Stable** (security improved, quality tech debt newly quantified but unchanged from before)

---

## Methodology

Files examined:
- `app/services/` — all 80+ files via grep + AST analysis
- `app/tasks/` — all 20 task files
- `app/api/` — all API route files
- `app/web/` — web route files
- `app/models/` — all model files
- `tests/` — directory listing and spot checks

Tools used: ripgrep for pattern matching, Python AST analysis for function line counts.
