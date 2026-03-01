# DotMac Sub — Claude Agent Guide

FastAPI + SQLAlchemy 2.0 + Jinja2/HTMX/Alpine.js + PostgreSQL. Multi-tenant ISP subscription platform.
3 portals: admin (`/admin`), customer (`/portal`), reseller (`/reseller`).

## Non-Negotiable Rules
- SQLAlchemy 2.0: `select()` + `scalars()`, never `db.query()`
- `db.flush()` in services, NOT `db.commit()` — routes commit
- All routes need `require_permission()` dependency
- Credential encryption: use `credential_crypto.py` (Fernet, `enc:<encrypted>` prefix format) for all sensitive fields
- Manager singleton pattern (same as CRM): static methods + singleton export
- Commands: always `poetry run ruff`, `poetry run mypy`, `poetry run pytest`
- Tailwind v4

## Credential Encryption
```python
from app.services.credential_crypto import encrypt_credential, decrypt_credential

# Store
record.api_key = encrypt_credential(raw_api_key)    # stores as "enc:..."

# Read
raw = decrypt_credential(record.api_key)
```
Never store raw credentials in DB columns.

## Template Rules (same as ERP)
- Single quotes on `x-data` with `tojson`
- `{{ var if var else '' }}` not `{{ var | default('') }}`
- Dict lookup for dynamic Tailwind classes
- `status_badge()`, `empty_state()`, `live_search()` macros — never inline
- CSRF mandatory on every POST form
- `<div id="results-container">` on list pages
- Dark mode: always pair light + dark variants
- POST redirects: `status_code=303`
