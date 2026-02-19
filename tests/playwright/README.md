# Playwright E2E Suite

## Recommended Structure

- `tests/playwright/conftest.py` - Playwright/pytest fixtures and storageState setup
- `tests/playwright/helpers/` - API helpers, auth setup, test data factories
- `tests/playwright/pages/` - Page Objects (admin, customer, reseller, auth)
- `tests/playwright/e2e/` - E2E specs (smoke, lifecycle, permissions)
- `tests/playwright/.auth/` - Generated storageState files (ignored by git)

## Prerequisites

1. Run the app locally (default expects `http://localhost:8000`).
2. Seed RBAC roles and an admin user if needed:

```bash
poetry run python scripts/seed_rbac.py --admin-email admin@example.com
poetry run python scripts/seed_admin.py \
  --email admin@example.com \
  --first-name Admin \
  --last-name User \
  --username admin \
  --password 'AdminPass123!'
```

## Environment Variables

Required:

- `E2E_ADMIN_USERNAME`
- `E2E_ADMIN_PASSWORD`

Optional:

- `PLAYWRIGHT_BASE_URL` (default: `http://localhost:8000`)
- `PLAYWRIGHT_BROWSER` (default: `firefox`)
- `PLAYWRIGHT_HEADLESS` (default: `true`)
- `PLAYWRIGHT_TIMEOUT_MS` (default: `10000`)
- `PLAYWRIGHT_NAV_TIMEOUT_MS` (default: `15000`)
- `PLAYWRIGHT_EXPECT_TIMEOUT_MS` (default: `10000`)
- `PLAYWRIGHT_LINK_CHECK_MAX` (default: `200`) - cap on pages scanned in link smoke tests
- `E2E_AGENT_USERNAME` (default: `e2e.agent`)
- `E2E_AGENT_PASSWORD` (default: `AgentPass123!`)
- `E2E_USER_USERNAME` (default: `e2e.user`)
- `E2E_USER_PASSWORD` (default: `UserPass123!`)

The suite will create agent/user identities via the admin API when they do not exist.

## Running the Suite

```bash
PLAYWRIGHT_BROWSER=firefox \
E2E_ADMIN_USERNAME=admin \
E2E_ADMIN_PASSWORD='AdminPass123!' \
poetry run pytest tests/playwright/e2e
```

## What’s Covered

- Authentication for admin/agent/user via `/api/v1/auth/login`
- Role-based access checks (admin-only APIs, impersonation permission)
- Portal smoke coverage for admin, customer, reseller, and auth pages
- Smoke coverage for internal links in admin, customer portal, and public pages

## Notes

- MFA must be disabled for E2E users.
- Storage states are generated at runtime under `tests/playwright/.auth/`.
- The admin portal does not enforce web auth yet, so the suite uses API tokens plus storageState to model login sessions.
- Some legacy journey specs still reference page objects that are not currently implemented; keep documentation and tests in sync before enabling them in CI.
