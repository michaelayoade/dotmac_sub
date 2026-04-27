# CLAUDE.md

This file provides context for Claude Code when working on this project.

## Project Overview

**DotMac Sub** is a multi-tenant subscription management system for ISPs and fiber network operators. It handles subscriber lifecycle, catalog management, billing, network provisioning, and service orders.

---

## Behavioral Guidelines (Karpathy Principles)

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

### 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them — don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

### 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

### 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it — don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

### 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

---

## Quick Commands

```bash
# Quality (or use: make check)
make lint                        # ruff check app/
make format                      # ruff format + fix
make type-check                  # mypy app/
make security                    # bandit security scan
make check                       # All quality checks

# Testing (or use: make test)
make test                        # Run test suite
make test-v                      # Verbose
make test-cov                    # With coverage
make test-fast                   # Stop on first failure
pytest tests/path/test_file.py -v  # Specific test

# Database
make migrate                     # alembic upgrade head
make migrate-new msg="desc"      # New migration
make migrate-down                # Rollback one
make migrate-history             # Show history

# Development
make dev                         # uvicorn with reload
make docker-up / docker-down     # Docker lifecycle
make docker-logs                 # Tail logs
make docker-shell                # Shell into app container
make worker                      # Celery worker
make beat                        # Celery beat scheduler

# Credentials
make encrypt-credentials         # Dry run encryption
make encrypt-credentials-execute # Execute encryption
make generate-encryption-key     # Generate new key

# Pre-commit
make pre-commit-install          # Install hooks
make pre-commit-run              # Run on all files
```

## Tech Stack

| Layer | Technology |
|-------|------------|
| Backend | FastAPI (Python 3.11+) |
| Database | PostgreSQL + PostGIS |
| ORM | SQLAlchemy 2.0 |
| Templates | Jinja2 |
| Frontend | HTMX + Alpine.js + Tailwind CSS v4 |
| Task Queue | Celery + Redis |
| Migrations | Alembic |
| Testing | pytest, Playwright (E2E) |

## Architecture

```
app/
├── main.py            # FastAPI app, middleware, router registration
├── api/               # REST API endpoints (thin wrappers → services)
├── web/               # Web routes (thin wrappers → services)
│   ├── admin/         # Admin portal (/admin/*)
│   ├── customer/      # Customer portal (/portal/*)
│   ├── reseller/      # Reseller portal (/reseller/*)
│   ├── auth/          # Shared web auth (/auth/*)
│   └── public/        # Public/legal pages
├── models/            # SQLAlchemy ORM models
├── services/          # ALL business logic lives here
│   ├── billing/       #   Invoices, payments, credit notes, tax, ledger
│   ├── catalog/       #   Offers, subscriptions, add-ons, RADIUS profiles
│   ├── network/       #   OLT, CPE, IP management
│   ├── events/        #   Event dispatcher, types, handler chains
│   ├── web_*.py       #   Web UI context builders (pair with web/admin/)
│   └── *.py           #   Domain services (subscriber, nas, provisioning, ...)
├── schemas/           # Pydantic request/response models
├── tasks/             # Celery background tasks
├── validators/        # Input validation utilities
└── imports/           # Bulk CSV import handlers

templates/             # Jinja2 + Alpine.js + HTMX
static/                # CSS, JS, images
alembic/               # Database migrations
tests/                 # pytest tests
scripts/               # CLI utilities (seed scripts, etc.)
docs/                  # Documentation
```

## Critical Rules

### 1. Service Layer — Routes are THIN WRAPPERS
**IMPORTANT:** Routes (both API and web) MUST NOT contain database queries, business logic, or conditionals. Routes may ONLY: parse request parameters, construct Pydantic schemas, call a single service method, and return the response/redirect. Any aggregation, filtering, sorting, direct ORM mutations, or `db.commit()` calls belong in the service layer.

```python
# CORRECT
@router.post("/subscriptions")
def create_subscription(data: SubscriptionCreate, db: Session = Depends(get_db)):
    return subscription_service.subscriptions.create(db, data)

# WRONG — logic in route
@router.post("/subscriptions")
def create_subscription(data: SubscriptionCreate, db: Session = Depends(get_db)):
    subscription = Subscription(**data.dict())  # NO — direct ORM construction
    db.add(subscription)                        # NO — direct DB mutation

# WRONG — business logic in route
@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db)):
    orders = service.list(db)
    pending = sum(1 for o in orders if o.status == "pending")  # NO — aggregation
    return {"pending": pending}

# WRONG — direct model mutation in route
@router.post("/orders")
def create_order(db: Session = Depends(get_db)):
    order = service.create(db, payload)
    order.some_field = value  # NO — direct ORM mutation
    db.commit()              # NO — db.commit() in route

# CORRECT — add a service method instead
@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db)):
    stats = service.dashboard_stats(db)  # Aggregation lives in service
    return stats
```

### 1a. ONT Configuration — Single Source of Truth

For ONT provisioning config, use `resolve_effective_ont_config()` as the **single source of truth**. Do NOT add fallback chains like:
```python
# WRONG — don't double-check after the resolver
value = effective_values.get("field") or getattr(ont, "field", None)

# CORRECT — trust the resolver
value = effective_values.get("field")
```

**Config priority (handled by resolver):**
1. `OntUnit.desired_config` JSON (highest)
2. `OltConfigPack` from OLT defaults (lowest)

If you need a value, call `resolve_effective_ont_config()` once and use the result.

### 1b. Adapter Boundaries — Register and Return Shared Results
Adapters in `app/services/*adapter*.py` should declare a stable `name` and register singleton instances with `app.services.adapters.adapter_registry` when they are safe to import without network I/O. Operation-style adapters should return or convert through `AdapterResult` from `app.services.adapters.base` so success, queued, warning, skipped, and error semantics stay consistent.

```python
from app.services.adapters import adapter_registry

class ExampleAdapter:
    name = "example"

example_adapter = ExampleAdapter()
adapter_registry.register(example_adapter)
```

### 2. Multi-tenancy — Filter Data Appropriately
For reseller/organization-scoped data, ensure proper filtering to prevent data leaks across tenants.

### 3. SQLAlchemy 2.0 — Use select(), Not db.query()
```python
stmt = select(Subscriber).where(Subscriber.organization_id == org_id)
subscribers = db.scalars(stmt).all()
```

### 4. Pydantic v2 — Use ConfigDict, Not orm_mode
```python
class SubscriberRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
```

### 5. API Authorization — All Endpoints Need Permission Checks
```python
@router.get("/subscribers", dependencies=[Depends(require_permission("subscriber:read"))])
def list_subscribers(...):
    ...
```

### 6. Credential Security — Encrypt Sensitive Data
Use `app/services/credential_crypto.py` for storing credentials:
```python
from app.services.credential_crypto import encrypt_credential, decrypt_credential

# Before storage
encrypted = encrypt_credential(plaintext_password)

# Before use
plaintext = decrypt_credential(stored_value)
```

### 7. Migrations — Must Be Idempotent
Check before creating: `inspector.has_table()`, column existence, enum existence.

### 8. Route Handlers Are Sync
SQLAlchemy sessions are sync. Use `def`, not `async def`. Background work goes to Celery.

## Code Style

- Type hints on ALL functions (mypy must pass)
- Every service file: `logger = logging.getLogger(__name__)`
- Imports: stdlib → third-party → local (absolute imports)
- Line length: 88 chars (ruff)
- Use `flush()` not `commit()` in services — caller controls transaction

## Code Patterns

### Adding a New Feature

1. **Model** (`app/models/`) - SQLAlchemy model with UUID primary key
2. **Service** (`app/services/`) - Business logic in a manager class
3. **Schema** (`app/schemas/`) - Pydantic models for API validation
4. **API Route** (`app/api/`) - JSON endpoints with permission checks
5. **Web Route** (`app/web/admin/`) - HTML routes for admin UI
6. **Templates** (`templates/admin/`) - Jinja2 templates

### Service Layer Pattern

Services use manager classes with CRUD methods:

```python
# app/services/example.py
class ExampleManager(ListResponseMixin):
    def list(self, db: Session, **filters) -> list[Model]:
        ...
    def get(self, db: Session, id: UUID) -> Model | None:
        ...
    def create(self, db: Session, data: dict) -> Model:
        ...
    def update(self, db: Session, id: UUID, data: dict) -> Model | None:
        ...
    def delete(self, db: Session, id: UUID) -> bool:
        ...

# Singleton instance
example = ExampleManager()
```

### Web Route Pattern

```python
# app/web/admin/example.py
router = APIRouter(prefix="/example", tags=["Example"])

@router.get("", response_class=HTMLResponse)
def list_items(request: Request, db: Session = Depends(get_db)):
    items = service.example.list(db)
    return templates.TemplateResponse("admin/example/index.html", {
        "request": request,
        "items": items,
        "active_page": "example",
    })

@router.post("", response_class=HTMLResponse)
def create_item(...):
    service.example.create(db, data)
    return RedirectResponse(url="/admin/example", status_code=303)
```

### Celery Task Pattern

```python
# app/tasks/example.py
from app.celery_app import celery_app
from app.db import SessionLocal

@celery_app.task(name="app.tasks.example.process_items")
def process_items():
    session = SessionLocal()
    try:
        # do work
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
```

### Template Pattern

Templates extend layouts and use components:

```html
{% extends "layouts/admin.html" %}

{% block breadcrumbs %}
<a href="/admin/dashboard">Dashboard</a>
<span class="mx-2">/</span>
<span>Current Page</span>
{% endblock %}

{% block content %}
<!-- Page content -->
{% endblock %}
```

## Verification Workflow

**IMPORTANT:** Before declaring any task complete, run verification:

**For Python changes:**
```bash
ruff check app/path/to/changed/files.py              # Must pass
mypy app/path/to/changed/files.py --ignore-missing-imports  # Must pass
pytest tests/path/to/relevant/tests.py -v            # Must pass
```

**For template changes, also verify:**
- Every `<form method="POST">` includes CSRF token field
- No `| safe` on user-submitted content
- Dark mode variants on all color classes

**For migrations:**
- Idempotent (safe to run multiple times)
- Has both `upgrade()` and `downgrade()`

## Agent Workflow

### Explore Before Implementing
ALWAYS read existing code in the same directory before writing new code. Match the patterns you find — import style, type hints, error handling, docstrings.

### Use Plan Mode for Multi-File Changes
For changes touching 3+ files, use plan mode first. Explore the codebase, identify all files that need changes, then present a plan before implementing.

### Verify Your Own Work
After implementing, run the verification workflow above. If tests fail, fix them before reporting completion. If mypy fails, fix type errors. Never skip verification.

### Common Mistakes to Avoid

**Technical:**
- Using `db.query()` instead of `select()` (SQLAlchemy 1.x vs 2.0)
- Using `| safe` on user content (XSS vulnerability)
- Using bare `except:` (catch specific exceptions)
- Putting business logic in routes (must be in services)
- Using `async def` for route handlers (sessions are sync)
- Missing permission checks on API endpoints
- Storing credentials in plaintext (use credential_crypto)
- String interpolation in Tailwind classes (gets purged — use dict lookup)
- Double quotes on Alpine.js `x-data` with `tojson` (use single quotes)
- Fragile regex for parsing (use proper parsers)

**Overengineering (Karpathy anti-patterns):**
- Adding fallback chains like `value or getattr(obj, "field", None)` — pick ONE source of truth
- Creating multiple entry points for the same operation (e.g., `do_x()`, `do_x_sync()`, `do_x_resilient()`)
- Keeping legacy field name mappings indefinitely — migrate and delete
- Adding `_first_present(a, b, c)` without documenting which source wins
- Leaving dead code "for backwards compatibility" — delete it or document why it's needed
- Creating abstractions for single-use code paths

## Key Models

| Model | Purpose |
|-------|---------|
| `Subscriber` | Unified customer/subscriber entity |
| `Subscription` | Service subscription linked to catalog plan |
| `CatalogOffer` | Service plans and pricing |
| `ServiceOrder` | Provisioning workflow for new/changed services |
| `Invoice`, `Payment` | Billing records |
| `NasDevice` | Network Access Server configuration |
| `OLT`, `ONT`, `CPE` | Network equipment |
| `RadiusAccount` | RADIUS authentication credentials |
| `User`, `Role`, `Permission` | RBAC for admin users |

## Conventions

### Naming
- Models: PascalCase singular (`Subscriber`, `Invoice`)
- Tables: snake_case plural (`subscribers`, `invoices`)
- Services: snake_case module with manager class (`subscriber.py` with `SubscriberManager`)
- API routes: kebab-case URLs (`/api/v1/subscriptions`)

### Database
- UUIDs for all primary keys
- `created_at`, `updated_at` timestamps on all tables
- Soft delete via `is_active` boolean where needed
- Enums stored as PostgreSQL enum types

### Forms & HTMX
- POST-Redirect-GET pattern for form submissions
- HTMX partials in `_partial.html` files for dynamic updates
- Alpine.js for client-side interactivity
- CSRF protection via double-submit cookie pattern

### Error Handling
- Return 404 with `admin/errors/404.html` for missing resources
- Flash messages via `HX-Trigger` headers for HTMX
- Validation errors displayed inline in forms

## Security Patterns

### Credential Encryption
NAS device credentials are encrypted at rest using Fernet encryption:
- Environment variable: `CREDENTIAL_ENCRYPTION_KEY`
- Encrypted fields: `shared_secret`, `ssh_password`, `ssh_key`, `api_password`, `api_token`, `snmp_community`
- Format: `enc:<encrypted>` for encrypted, `plain:<value>` for unencrypted, or legacy (no prefix)

### API Authorization
All API endpoints require authentication and permission checks:
```python
from app.services.auth_dependencies import require_permission

@router.get("/resource", dependencies=[Depends(require_permission("resource:read"))])
@router.post("/resource", dependencies=[Depends(require_permission("resource:write"))])
```

### CSRF Protection
- Applies to `/admin/` and `/web/` paths
- Double-submit cookie pattern
- Multipart forms parsed with `email.parser` (not regex)

## Testing

```python
# tests/test_example.py
import pytest
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_list_example(test_db):
    response = client.get("/api/v1/example")
    assert response.status_code == 200
```

Use `test_db` fixture from `conftest.py` for database tests.

## Important Files

- `app/main.py` - App initialization, middleware, all router registration
- `app/db.py` - Database session management, `get_db` dependency
- `app/config.py` - Environment configuration via pydantic-settings
- `app/errors.py` - Global error handlers
- `app/services/auth_dependencies.py` - Permission checking dependencies
- `app/services/credential_crypto.py` - Credential encryption utilities
- `alembic/env.py` - Migration configuration
- `templates/layouts/admin.html` - Base admin layout with sidebar
- `docs/DEVELOPER_GUIDE.md` - Detailed implementation guide

## Portal URLs

| Portal | Base URL | Auth URL |
|--------|----------|----------|
| Admin | `/admin` | `/auth/login` |
| Customer | `/portal` | `/portal/auth/login` |
| Reseller | `/reseller` | `/reseller/auth/login` |
| API | `/api/v1` | JWT Bearer token |

## Environment Variables

Required:
- `DATABASE_URL` - PostgreSQL connection string
- `SECRET_KEY` - Application secret for sessions/tokens
- `REDIS_URL` - Redis connection for Celery

Security (optional but recommended):
- `CREDENTIAL_ENCRYPTION_KEY` - Fernet key for NAS credential encryption

## CI/CD Pipeline

### GitHub Actions Workflow (`.github/workflows/ci.yml`)

The CI pipeline runs on push to `main`/`develop` and on PRs:

| Job | Description |
|-----|-------------|
| `lint` | Ruff linting and format check |
| `type-check` | Mypy type checking |
| `test` | pytest with coverage |
| `security` | Bandit security scan |
| `pre-commit` | Pre-commit hooks |
| `docker-build` | Build and health check |
| `integration-test` | PostgreSQL integration tests |
| `publish-image` | Push to GHCR (main only) |

### Pre-commit Hooks (`.pre-commit-config.yaml`)

Install with `make pre-commit-install`. Runs on every commit:
- **ruff** - Linting and formatting
- **trailing-whitespace** - Remove trailing spaces
- **end-of-file-fixer** - Ensure newline at EOF
- **check-yaml/toml** - Validate config files
- **check-added-large-files** - Block files > 500KB
- **debug-statements** - No pdb/breakpoint()
- **detect-private-key** - Catch accidental key commits
- **bandit** - Security static analysis
- **detect-secrets** - Find hardcoded secrets

### Docker Image Tags

On push to main, images are published to GHCR with tags:
- `latest` - Most recent main build
- `sha-<short>` - Git commit SHA
- `YYYY-MM-DD` - Date-based tag

### Local Verification Before PR

```bash
make check              # lint + type-check + security
make test               # Run test suite
make pre-commit-run     # Run all pre-commit hooks
```

## Notes

- All API routes are also available without `/api/v1` prefix for backwards compatibility
- CSRF protection applies to `/admin/` and `/web/` paths only
- Audit logging is configurable via `DomainSetting` with domain `audit`
- Settings are seeded on startup from `app/services/settings_seed.py`
- WebSocket support at `/ws` for real-time notifications

## Design Context

Source of truth: `DESIGN.md` at the repo root (machine-readable tokens + design rationale in Google Stitch format). Quick summary:

- **Users**: NOC technicians first (diagnose / configure / monitor under time pressure). Secondary: admins, resellers, customers on their own portals.
- **Personality**: *Precise, confident, quiet*. Trust through accuracy and speed, not through loud UI.
- **Aesthetic**: SmartOLT's information density and page patterns, rendered in the current dotmac Tailwind aesthetic. Never generic SaaS purple-gradient, never Stripe/Linear minimalism (wastes NOC screen real estate), never vendor-UI utilitarianism.
- **Tokens**: Outfit (display) + Plus Jakarta Sans (body). Primary teal/cyan `#06b6d4`, accent warm orange `#f97316`. Semantic colors are a contract: emerald=healthy, amber=warning, rose=critical, blue=network, violet=people, slate=neutral.
- **Principles**: Status first (color + shape + position, never color alone). Density with breathing room (`py-2.5`, `text-sm` tables, clear section borders). Actions confirm themselves inline (HTMX, no full reloads). Dark mode is mandatory at author time. Typography carries hierarchy (`tabular-nums` for every number). A11y target is WCAG AA.
- **New macros**: go in `templates/components/<group>/`. Dynamic color classes must be safelisted in `tailwind.config.js`.
- **Validation**: Run `npx design-md lint` to check DESIGN.md structure and WCAG contrast ratios.

## OLT SSH Credentials

| OLT | Username | Password |
|-----|----------|----------|
| BOI Asokoro | dotmac | Dotmac246 |
| Jabi OLT | dotmac | Dotmac246 |
| Karsana OLT | dotmac | Dotmac@1 |
| Gwarimpa OLT | dotmac | Dotmac246 |
| SPDC OLT | spdcolt | Dotmac246 |
| Garki OLT | garkiolt | Dotmac246 |
| Gudu OLT | guduolt | Dotmac246 |
