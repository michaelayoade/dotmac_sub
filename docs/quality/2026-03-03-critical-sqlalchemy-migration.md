# Critical SQLAlchemy Migration Status (2026-03-03)

## Summary of Work Completed
- Replaced `db.commit()` with `db.flush()` in high-impact service modules.
- Migrated all `db.query(...)` usage in `app/services/web_network_fiber.py` to SQLAlchemy 2.x `select` + `scalars/execute/scalar` patterns.

## Remaining Work (High Priority)
- Convert remaining `db.query(...)` usage in `app/services/collections/_core.py` and `app/services/web_customer_actions.py` to SQLAlchemy 2.x style.
- Address top remaining `db.query(...)` hotspots in services (counts as of 2026-03-03):
  - `app/services/network_monitoring.py` (33)
  - `app/services/collections/_core.py` (28)
  - `app/services/web_customer_actions.py` (24)
  - `app/services/billing/payments.py` (24)
  - `app/services/auth_flow.py` (20)
  - `app/services/web_system_user_mutations.py` (19)
  - `app/services/wireguard.py` (18)
  - `app/services/notification.py` (18)
  - `app/services/fiber_plant_api.py` (18)
  - `app/services/subscriber.py` (16)

## Notes / Risks
- Some remaining `db.query(...)` call sites include mixed entity/column selects; prefer `db.execute(select(...))` with explicit `Row` unpacking.
- For count aggregations, standardize on `db.scalar(select(func.count(...)))` to align with SQLAlchemy 2.x.
- For pagination helpers or dynamic filters, validate that `select()` conversions preserve `.limit()`, `.offset()`, and `.order_by()` behavior.

## Suggested Next Steps
- Convert `app/services/collections/_core.py` and `app/services/web_customer_actions.py` first; both are already touched by this migration effort.
- Add targeted regression checks for services that rely on `.first()`/`.scalar()` behavior and implicit ordering.
