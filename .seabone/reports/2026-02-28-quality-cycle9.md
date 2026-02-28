# Quality Scan — Cycle 9
**Date:** 2026-02-28
**Type:** quality
**Files scanned:** ~531 Python files in `app/`
**Health Score:** 53/100 (stable vs cycle 6: 54/100)
**Trend:** Stable

---

## Summary

10 new formal findings across 3 HIGH, 4 MEDIUM, and 1 LOW severity categories (plus 2 MEDIUM). No critical findings. The issues discovered are systemic patterns — resource leaks, missing retry logic, blocking sleeps, and silent exception swallowing — rather than newly introduced bugs. The codebase is not degrading, but these long-standing issues warrant structured remediation.

---

## Findings Breakdown

| ID | Severity | Effort | File | Issue |
|----|----------|--------|------|-------|
| quality-c9-1 | HIGH | small | `radius.py:283,361` | `create_engine()` without `engine.dispose()` — connection pool leak on every provisioning call |
| quality-c9-2 | HIGH | medium | `tasks/snmp.py` et al (7 tasks) | Celery network tasks have no `autoretry_for` — transient errors cause permanent failure |
| quality-c9-3 | HIGH | medium | `snmp_discovery.py:233` | `time.sleep(1)` in function called from sync web handlers — blocks worker thread 1s/request |
| quality-c9-4 | MEDIUM | trivial | `web_billing_dunning.py:106` | `except Exception: continue` without logging silently swallows dunning action failures |
| quality-c9-5 | MEDIUM | trivial | `usage.py:582` | `except Exception: pass` silently drops usage charge posting failures |
| quality-c9-6 | MEDIUM | trivial | `web_catalog_settings.py:255-291` | 5 bulk-delete exception handlers log at `DEBUG` only — invisible in production |
| quality-c9-7 | MEDIUM | trivial | `vpn_cache.py:319` | Bare `# type: ignore` on cache return after JSON decode failure — incorrectly-typed return value |
| quality-c9-8 | MEDIUM | medium | `app/services/` (12+ files) | 51 service functions have untyped `db` parameter — breaks mypy inference codebase-wide |
| quality-c9-9 | MEDIUM | small | `web_billing_ledger.py:36`, `web_billing_overview.py:108` | Inner functions suppress mypy with `# type: ignore[no-untyped-def]` instead of proper annotations |
| quality-c9-10 | LOW | trivial | `app/api/`, `app/schemas/` | Only two `app/` subdirs missing `__init__.py` — inconsistency with all other packages |

---

## Comparison with Previous Findings

### What's New (Cycle 9)
- **`radius.py` resource leak** — `create_engine()` called without `engine.dispose()` in two separate RADIUS provisioning functions. Previously only `enforcement.py` was called out with this pattern; this is a third location.
- **Celery retry gap** — Formally captured for the first time: 7 network-intensive tasks have zero retry configuration. Only `webhooks.py` correctly uses `autoretry_for`.
- **Blocking `time.sleep(1)` in web path** — `snmp_discovery.py:233` is called from sync HTTP route handlers in `web_network_core_devices_forms.py`, tying up a worker thread.
- **Silent exception swallowing (formal findings)** — `usage.py:582`, `web_billing_dunning.py:106`, and `web_catalog_settings.py:255-291` were noted in cycle 6 memory observations but not converted to formal JSON findings until now.
- **51 untyped `db` parameters** — First time formally tracked; spans 12+ service files.

### Still Open from Previous Cycles (Not Yet Fixed)
- **815+ `db.query()` calls** (cycle 2 / quality-c2) — SQLAlchemy 1.x pattern, no migration progress detected
- **570 `db.commit()` in services** (cycle 2) — Transaction ownership violation
- **Missing loggers in large files** (cycle 6): `nas.py`, `notification.py`, `subscriber.py`, `auth_flow.py` — all confirmed still zero loggers
- **`enforcement.py:748` `create_engine()` without dispose** (cycle 2) — Still present (now joined by `radius.py`)
- **N+1 query patterns** (cycle 6: `bandwidth.py:452`, `collections/_core.py:1143`) — Still present
- **ORM display-attribute mutation antipattern** (cycle 6: `web_billing_payments.py:556-558`) — Still present
- **Hardcoded `limit=2000`** (cycle 6: `web_billing_overview.py:65,364`, `web_billing_payments.py:601`) — Still present
- **`async def` route handlers with sync DB session** (cycle 2: `api/billing.py:943,957`, `web/admin/provisioning.py:231,250`) — Still present

### Confirmed Fixed Since Last Scan
- No quality findings were confirmed fixed in this cycle; security fixes from cycle 8 are tracked separately.

---

## Top 3 Priority Fixes

### 1. `radius.py` Engine Leak (quality-c9-1) — Small Effort, HIGH Impact
Every RADIUS provisioning event creates a new connection pool via `create_engine()` and never disposes it. In a busy ISP environment with frequent provisioning, this is a silent resource exhaustion vector. Fix: add `try/finally: engine.dispose()` wrapper. Affects 2 call sites.

### 2. Celery Task Retry Logic (quality-c9-2) — Medium Effort, HIGH Reliability Gain
Seven background tasks (SNMP polling, OLT backup, NAS provisioning, integrations, VPN, billing RADIUS) will permanently fail on any transient network error with no retry. This silently drops scheduled work. Adding `autoretry_for=(OSError, httpx.RequestError, httpx.TimeoutException)` with `max_retries=3` to each task decorator takes ~5 minutes per file.

### 3. Silent Exception Swallowing (quality-c9-4, c9-5, c9-6) — Trivial, HIGH Observability Gain
Three locations drop exceptions without any log output. `usage.py:582` hides billing failures, `web_billing_dunning.py:106` hides dunning failures, and `web_catalog_settings.py:255-291` hides delete failures. Each is a one-line change from `pass`/`continue` to `logger.warning(..., exc_info=True)`.

---

## Codebase Health Score

**Score: 53/100**

| Dimension | Score | Notes |
|-----------|-------|-------|
| Exception handling | 55/100 | Broad `except Exception` pervasive; 3 silent-swallow locations newly found |
| Resource management | 50/100 | 3 `create_engine()` sites without dispose; enforcement.py still open |
| Type safety | 60/100 | 51 untyped `db` params; 2 `# type: ignore[no-untyped-def]` suppressors |
| Observability (logging) | 45/100 | `nas.py`, `notification.py`, `subscriber.py`, `auth_flow.py` all missing loggers |
| Reliability (task retries) | 40/100 | Only 1 of 8 network tasks has retry logic |
| Code complexity | 50/100 | Multiple 200-500 line functions; still unaddressed |
| ORM patterns | 45/100 | 815+ `db.query()` calls; 570 `db.commit()` in services |

---

## Trend: Stable

The codebase is neither improving nor degrading. New findings (cycle 9) are systemic issues that existed in previous cycles but were not formally captured, not newly introduced code problems. The backlog of open quality issues from cycles 2 and 6 remains large, indicating that quality remediation work has not been prioritized. The security remediation work (cycles 1 and 8) is actively progressing.

**Recommendation:** Schedule a dedicated quality sprint targeting the top 10 highest-effort-to-impact items from cycles 2, 6, and 9 — particularly the SQLAlchemy 1.x migration (`db.query()` → `select()`) and the `db.commit()` transaction ownership violations, which are the most architecturally significant.
