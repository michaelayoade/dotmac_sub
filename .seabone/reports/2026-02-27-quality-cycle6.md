# Quality Cycle 6 — Seabone Sentinel Report
**Date:** 2026-02-27
**Scan type:** Quality
**Scope:** Full codebase (`app/`, `tasks/`, `templates/`)

---

## Executive Summary

This sixth scan continued the deep-quality focus started in cycle 2, targeting areas not yet reported: missing observability infrastructure in critical service files, new monolithic functions, silent bulk-operation failures, N+1 patterns in non-enforcement paths, and a structural antipattern where display-layer computed values are stapled onto ORM instances.

The most significant finding is that **four of the largest service files have zero logging infrastructure** — `nas.py` (2,248 lines), `notification.py` (899 lines), `subscriber.py` (802 lines), and `network_monitoring.py` (1,333 lines) — meaning that production errors in core ISP operations are completely invisible.

**Total findings: 16** (0 Critical / 5 High / 7 Medium / 4 Low)

---

## Findings by Severity

### High (5)

| ID | File | Line | Issue | Effort |
|----|------|------|-------|--------|
| quality-c6-1 | `services/subscriber.py` | 1 | No `logger` in 802-line subscriber service — all lifecycle errors invisible | trivial |
| quality-c6-2 | `services/nas.py` | 1 | No `logger` in 2248-line NAS/provisioning service — device errors invisible | small |
| quality-c6-3 | `services/web_billing_invoice_bulk.py` | 38 | 7× `except Exception: continue` with no logging + 3 `db.commit()` in service | small |
| quality-c6-4 | `services/network_map.py` | 25 | `build_network_map_context()` is 323 lines; uses `db.query()` throughout | medium |
| quality-c6-5 | `services/web_network_core_devices_views.py` | 283 | `onts_list_page_data()` is 211 lines — no decomposition | medium |

### Medium (7)

| ID | File | Line | Issue | Effort |
|----|------|------|-------|--------|
| quality-c6-6 | `services/bandwidth.py` | 452 | N+1: `db.get(Subscription)` + lazy-load per metrics result row | small |
| quality-c6-7 | `services/collections/_core.py` | 1143 | N+1: `db.get(Subscriber)` + 2 more queries per overdue account in dunning loop | small |
| quality-c6-8 | `services/web_billing_payments.py` | 556 | ORM mutation antipattern: display fields stapled to ORM instances via `type: ignore[attr-defined]` | small |
| quality-c6-9 | `app/` (codebase-wide) | — | 94 f-string logging calls — eager evaluation wastes CPU when log level is suppressed | trivial |
| quality-c6-10 | `services/notification.py` | 1 | No `logger` in 899-line notification dispatch service | trivial |
| quality-c6-11 | `services/gis.py` et al. | 3 | `import builtins` redundant in 4 files that already have `from __future__ import annotations` | trivial |
| quality-c6-12 | `services/web_billing_overview.py` | 364 | Hard-coded `limit=2000` cap silently truncates AR aging / dashboard data | small |

### Low (4)

| ID | File | Line | Issue | Effort |
|----|------|------|-------|--------|
| quality-c6-13 | `services/web_billing_payments.py` | 443 | `build_payments_list_data()` is 199 lines with 4 nested inner functions | small |
| quality-c6-14 | `tasks/events.py` | 27 | 3 task functions with local imports inside body — import overhead per invocation | trivial |
| quality-c6-15 | `services/web_billing_invoice_bulk.py` | 49 | Direct ORM field mutation bypasses canonical `billing.invoices` service | small |
| quality-c6-16 | `services/web_subscriber_details.py` | 176 | `build_subscriber_detail_snapshot()` is 192 lines — no decomposition | small |

---

## Top 3 Priority Fixes

### 1. Add `logger` to `nas.py` and `subscriber.py` (quality-c6-1, quality-c6-2)
**Why first:** These are the two largest service files in the codebase (2,248 and 802 lines) and handle the most business-critical operations (device provisioning, RADIUS, subscriber lifecycle). Any exception thrown inside them is completely invisible unless it propagates all the way to the top-level error handler. A single `import logging` + `logger = logging.getLogger(__name__)` gives the on-call engineer something to look at when things go wrong. Effort: trivial-to-small.

### 2. Fix silent bulk-invoice failures (quality-c6-3)
**Why second:** `web_billing_invoice_bulk.py` is called from the admin bulk-action UI that lets staff issue, void, and mark-paid multiple invoices at once. With 7 `except Exception: continue` swallowing every failure, an ISP administrator who bulk-issues 50 invoices has no way of knowing if 10 of them silently failed (e.g., due to a DB constraint or status mismatch). The additional `db.commit()` per item in the loop means any one failure leaves the session in an ambiguous state for the remaining items.

### 3. Remove `limit=2000` ceiling from AR aging / overview (quality-c6-12)
**Why third:** The AR aging report and billing overview dashboard are used for financial decision-making. An ISP with >2,000 open invoices (perfectly possible for large deployments) will silently see incomplete data with no indication of truncation. This is a data correctness issue with real financial consequences.

---

## Comparison with Previous Scans

| Finding | Status |
|---------|--------|
| SQLAlchemy 1.x `db.query()` (quality-c2-1) | Still open — `network_map.py:25` (quality-c6-4) is a new instance |
| `db.commit()` in services (quality-c2-2) | Still open — `web_billing_invoice_bulk.py` (quality-c6-3) adds 3 more confirmed instances |
| Monolithic functions (quality-c2-3/4/5/17) | Still open — three new functions discovered: 323/211/199/198/192 lines |
| N+1 queries (quality-c2-6) | Still open — two new N+1 sites discovered in `bandwidth.py` and `collections/_core.py` |
| Silent exception swallowing (quality-c2-10/12/20) | Partially addressed — `web_billing_invoice_bulk.py` is a new 7-instance cluster |
| Async/sync route mixing (quality-c2-7/8) | Status unknown — not re-verified this cycle |

**New findings this cycle (not previously reported):**
- Missing logger in `nas.py`, `subscriber.py`, `notification.py` (quality-c6-1/2/10)
- ORM mutation antipattern for display fields (quality-c6-8)
- f-string logging antipattern, 94 instances (quality-c6-9)
- Redundant `import builtins` in files with `from __future__ import annotations` (quality-c6-11)
- Hard-coded `limit=2000` silently truncating financial reports (quality-c6-12)
- Local imports inside Celery task bodies in `tasks/events.py` (quality-c6-14)
- Business logic bypassed via direct ORM mutation in invoice bulk service (quality-c6-15)

---

## Codebase Health Score: 54/100

**Rationale (vs. 52/100 in cycle 2):**
- (+2) Security fixes have been merged and validated; auth, encryption, rate-limiting improved
- (+0) Quality tech debt unchanged — major quality findings from cycle 2 remain open
- (-) Two critical service files still have zero logging (nas.py, subscriber.py)
- (-) Silent bulk-invoice failures add new visibility risk
- (-) N+1 patterns in dunning and metrics paths not yet addressed
- (-) display-layer ORM mutation antipattern found in 2 more service files
- (-) 94 f-string logging instances degrade observability efficiency

**Trend: Stable** — Security posture is meaningfully better than cycle 1 baseline (42/100), but structural quality debt from cycle 2 remains unaddressed. No regression detected, but the rate of quality improvement is slower than security improvement.

---

## Key Patterns Observed

### Missing Logger — Scale of Problem
Of the ~231 files in `app/services/`, only 99 have `logger = logging.getLogger(__name__)` (~43%). The largest and most critical service files are disproportionately represented among those missing it, including `nas.py` (2,248 lines), `network_monitoring.py` (1,333 lines), `auth_flow.py` (1,044 lines), `notification.py` (899 lines), and `subscriber.py` (802 lines). The CLAUDE.md rule "Every service file: `logger = logging.getLogger(__name__)`" is violated by the majority of the codebase.

### ORM Mutation Antipattern
Service files add computed display attributes to SQLAlchemy ORM objects at runtime using `# type: ignore[attr-defined]`, making ORM objects implicitly carry presentation-layer state. Affected: `web_billing_payments.py` (3 attributes), `web_network_speed_profiles.py` (1 attribute), `web_admin_resellers.py` (1 attribute). The fix is a lightweight DTO dataclass or TypedDict.

### Hard-Coded Pagination Ceilings
At least 3 locations use `limit=2000` when fetching all records for aggregation/reporting, silently returning incomplete data for large deployments. This class of bug is insidious because it works perfectly in development (where data is small) but fails silently in production.

---

## Methodology

Files examined:
- `app/services/` — all 231+ files via grep + AST function-length analysis
- `app/tasks/` — all Celery task files
- `app/web/admin/` — web route files for exception handling patterns
- Templates for ORM attribute usage verification

New tools used vs. previous cycles:
- AST-based function length analysis (Python `ast` module)
- Module-level attribute scan (`import logging` + `logger =` presence)
- f-string logging detection via grep pattern matching
- Template scanning to confirm ORM mutation antipattern
