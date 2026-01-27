# CLAUDE.md

This file provides context for Claude Code when working on this project.

## Project Overview

**DotMac Sub** is a multi-tenant subscription management system for ISPs and fiber network operators. It handles subscriber lifecycle, catalog management, billing, network provisioning, and service orders.

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

## Project Structure

```
app/
├── main.py            # FastAPI app, middleware, router registration
├── api/               # REST API endpoints (JSON responses)
├── web/               # Web routes (HTML responses via Jinja2)
│   ├── admin/         # Admin portal (/admin/*)
│   ├── customer/      # Customer portal (/portal/*)
│   ├── reseller/      # Reseller portal (/reseller/*)
│   └── vendor/        # Vendor portal (/vendor/*)
├── models/            # SQLAlchemy ORM models
├── services/          # Business logic (stateless managers)
├── schemas/           # Pydantic request/response models
├── tasks/             # Celery background tasks
├── validators/        # Input validation utilities
└── imports/           # Bulk CSV import handlers

templates/             # Jinja2 templates matching web/ structure
static/                # CSS, JS, images
alembic/               # Database migrations
tests/                 # pytest tests
scripts/               # CLI utilities (seed scripts, etc.)
docs/                  # Documentation
```

## Development Commands

```bash
# Run dev server
docker compose up

# Run tests
pytest
pytest tests/test_specific.py -v

# Run with coverage
pytest --cov=app

# Database migrations
alembic revision --autogenerate -m "description"
alembic upgrade head
alembic downgrade -1

# Celery worker (for background tasks)
celery -A app.celery_app worker --loglevel=info

# Seed admin user
python scripts/seed_admin.py
```

## Code Patterns

### Adding a New Feature

1. **Model** (`app/models/`) - SQLAlchemy model with UUID primary key
2. **Service** (`app/services/`) - Business logic in a manager class
3. **Schema** (`app/schemas/`) - Pydantic models for API validation
4. **API Route** (`app/api/`) - JSON endpoints if needed
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

## Key Models

| Model | Purpose |
|-------|---------|
| `Subscriber` | Unified customer/subscriber entity |
| `Subscription` | Service subscription linked to catalog plan |
| `CatalogOffer` | Service plans and pricing |
| `ServiceOrder` | Provisioning workflow for new/changed services |
| `Invoice`, `Payment` | Billing records |
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
- `alembic/env.py` - Migration configuration
- `templates/layouts/admin.html` - Base admin layout with sidebar
- `docs/DEVELOPER_GUIDE.md` - Detailed implementation guide

## Portal URLs

| Portal | Base URL | Auth URL |
|--------|----------|----------|
| Admin | `/admin` | `/auth/login` |
| Customer | `/portal` | `/portal/auth/login` |
| Reseller | `/reseller` | `/reseller/auth/login` |
| Vendor | `/vendor` | `/vendor/auth/login` |
| API | `/api/v1` | JWT Bearer token |

## Notes

- All API routes are also available without `/api/v1` prefix for backwards compatibility
- CSRF protection applies to `/admin/` and `/web/` paths only
- Audit logging is configurable via `DomainSetting` with domain `audit`
- Settings are seeded on startup from `app/services/settings_seed.py`
- WebSocket support at `/ws` for real-time notifications
