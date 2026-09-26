# Product Context

## Platform

DotMac Sub is a multi-tenant web application. Tenant administrators configure governed platform capabilities, while operators work with customer and operational records through module-specific pages.

## Custom Fields Center

The Custom Fields Center lets authorized tenant administrators define typed custom fields for explicitly registered entity types. Operators fill those fields from the owning entity experience, subject to both custom-field permissions and the entity module's native permissions.

Custom-field definitions and values are centrally owned. Entity applicability is declared in code through the system-of-truth registry; administrators cannot point a field at arbitrary database tables or columns. Adding or changing a field does not require a schema migration.

## Confirmed Scope

- Custom Fields is a permission-gated destination in the System category of Settings Hub.
- Registered targets are subscribers/customers, projects, support tickets, work orders, leads, quotes, and sales orders.
- Definitions have an explicit draft, active, and retired lifecycle.
- Values are validated against the active definition before persistence.
- Access is tenant-isolated, audited, and gated by granular definition and value permissions plus the target module's native permissions.
- Existing `subscriber_custom_fields` data and APIs remain legacy-owned. This feature does not migrate, reinterpret, or dual-write legacy values.
- Sensitive fields are explicitly marked and are not exposed to automation or general projections by default.
- The interface follows the established DotMac administration design, responsive behavior, dark mode, and WCAG AA expectations.

## Deferred

- Migrating legacy subscriber custom fields.
- Automation triggers, conditions, and actions based on custom fields.
- Further module registration beyond the current seven target types.
- Reporting/search indexing of custom-field values.
- Opening or updating a pull request for this local branch.

## Product Evidence

- `DESIGN.md`
- `docs/designs/AUTOMATION_CENTER_SOT.md`
- `docs/SOT_RELATIONSHIP_MAP.md`
- `app/models/subscriber.py`
- `app/migration_source/surfaces.py`
