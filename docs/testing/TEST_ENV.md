# DotMac Sub — Disposable Test Stack (`dotmac_test`)

A self-contained environment for driving **edge-case tests per module** through the
real UI/API with Playwright (MCP or scripted), **without ever touching the live
`dotmac_sub` database**.

> **Isolation guarantee.** The live app runs on **:8001** against the `dotmac_sub`
> database and Redis **db 0**. This test stack runs a *second* app instance on
> **:8010** against a *separate* `dotmac_test` database and Redis **db 5**. They
> share the same Postgres/Redis *containers* but no data. The seed script also
> refuses to run unless the target DB name contains `test`.

---

## TL;DR

```bash
# one-shot: create DB + migrate + seed + start app on :8010
scripts/testing/test_stack.sh bootstrap

# everyday:
scripts/testing/test_stack.sh up        # (re)start test app
scripts/testing/test_stack.sh seed      # re-seed fixtures (idempotent)
scripts/testing/test_stack.sh psql "select * from subscribers"
scripts/testing/test_stack.sh reset     # DROP + rebuild from scratch
scripts/testing/test_stack.sh status
```

Test app URL: **http://127.0.0.1:8010**

---

## Topology

| Piece            | Live (prod-on-host)        | Test stack (this)                  |
| ---------------- | -------------------------- | ---------------------------------- |
| App container    | `dotmac_sub_app` :8001     | `dotmac_test_app` :8010            |
| Database         | `dotmac_sub`               | `dotmac_test`                      |
| Postgres host    | `dotmac_pg_local` (:9001)  | same container, different DB       |
| Redis            | `dotmac_redis_local` db 0  | same container, **db 5**           |
| `APP_ENV`        | `production`               | `development` (http cookies work)  |
| Docker network   | `dotmac_sub_default`       | same                               |

Both app instances bind-mount the working tree (`app/`, `templates/`, `static/`),
so **code/template edits are picked up on restart** of the test container — no
rebuild needed. The Docker image (`dotmac_sub-app`) is only used for its Python
deps + baked `alembic/`; we mount the working-tree `alembic/` over it so the test
DB always migrates to the current tree head.

### Why `APP_ENV=development`?
Production mode sets `Secure` cookies (HTTPS-only) and stricter security headers,
which break login over plain `http://127.0.0.1:8010`. Development relaxes those so
the browser can hold a session.

---

## Seeded fixtures

Created by `scripts/seed/seed_test_fixtures.py` (idempotent). **Password for every
seeded login: `TestPass123!`**

### Staff / system users — log in at `/auth/login`
| Login                 | Role             | Use for                                   |
| --------------------- | ---------------- | ----------------------------------------- |
| `admin@test.local`    | `admin` (`*`)    | Full admin, settings, network, RBAC       |
| `support@test.local`  | `support`        | Staff-gate / least-privilege edge cases   |
| `finance@test.local`  | `finance_manager`| Billing-only permission boundary          |

### Customers — log in at `/portal/auth/login`
| Login                          | Reseller | State / billing            | Edge cases it exercises                          |
| ------------------------------ | -------- | -------------------------- | ------------------------------------------------ |
| `active.customer@test.local`   | House    | active, postpaid, **paid** | happy path; paid invoice + succeeded payment     |
| `overdue.customer@test.local`  | House    | **delinquent**, overdue    | dunning, arrangement eligibility, blocked pay    |
| `prepaid.customer@test.local`  | E2E      | active, **prepaid**        | invoice-in-advance, top-up/bundle, change-plan   |
| `suspended.customer@test.local`| E2E      | **suspended** sub          | login-while-suspended, captive/blocked access    |
| `new.customer@test.local`      | House    | **new**, no subscription   | onboarding, empty-state pages                    |

### Reseller — log in at `/reseller/auth/login`
| Login                  | Reseller     | Notes                                             |
| ---------------------- | ------------ | ------------------------------------------------- |
| `reseller@test.local`  | E2E Reseller | has `reseller_users` link; sub-accounts: prepaid + suspended customers |

### Catalog offers
`Prepaid Fibre 10/2` (active, prepaid) · `Postpaid Fibre 20/5` (active, postpaid,
12-mo) · `Archived Legacy 5/1` (archived) · `Inactive Draft 50/10` (inactive).
The archived/inactive offers exist to probe the change-plan instant-path /
offer-scoping edge cases.

> The seed is intentionally a **baseline**. Add targeted rows for a specific edge
> case directly (`scripts/testing/test_stack.sh psql "..."`) or by extending
> `seed_test_fixtures.py`, then note the fixture in `EDGE_CASE_MATRIX.md`.

---

## Driving the browser with Playwright MCP

The `playwright` MCP server is registered in `.mcp.json` (project scope,
`--headless --no-sandbox`). **It loads only after Claude Code is restarted.** Once
loaded, `browser_*` tools are available. Typical loop:

1. `browser_navigate` → `http://127.0.0.1:8010/auth/login`
2. `browser_snapshot` (accessibility tree — preferred over screenshots for
   driving), fill the form, submit.
3. Walk the module, try the edge case, capture `browser_take_screenshot` for the
   record, and log the result in `EDGE_CASE_MATRIX.md`.

Screenshots/output land in `.playwright-mcp/` (gitignored-by-convention; don't
commit).

### Scripted alternative (no restart needed)
The repo already has a Python Playwright suite under `tests/playwright/` (run
against this stack by setting `PLAYWRIGHT_BASE_URL=http://127.0.0.1:8010` plus the
`E2E_ADMIN_*` vars). Use it for regression; use MCP for exploratory edge-case work.

---

## Lifecycle / housekeeping

- **Persistent** by design — survives restarts so you can iterate. `reset` wipes it.
- The test app does **not** run Celery/beat — scheduled billing/dunning/FUP jobs do
  not fire here. Trigger those code paths directly (service call / API) when testing.
- To stop everything: `scripts/testing/test_stack.sh down` (app only; DB persists).
- This stack is **local-only** (`127.0.0.1`). Never point Playwright at
  `selfcare.dotmac.io` — the conftest prod-guard exists for that reason.

---

## Gotchas (learned while building this)

- The baked image's `alembic/` lagged the tree (148 vs 149). The script mounts the
  working-tree `alembic/`, so `migrate` always reaches the true head.
- Customer portal uses a **local email `UserCredential`** (provider `local`,
  `subscriber_id` set) for `/portal/auth/login`; PPPoE/RADIUS is separate. The seed
  creates these local creds so browser login works without RADIUS.
- Admin `/admin/*` requires a **`SystemUser`** principal, not a Subscriber. The seed
  creates real `system_users` + `system_user_roles`, not subscriber-bound creds.
- Redis **db 5** keeps the test app's settings-cache and sessions from polluting the
  live app's db 0 (a real incident in the past).
