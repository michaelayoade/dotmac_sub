# Custom Fields Center: Source of Truth and Page Contract

Status: local implementation, not deployed

## Decision

`custom_fields.records` is the only writer for new central custom-field definitions and values. Applicability is not administrator-entered data: each eligible entity type is declared by its canonical SOT domain through `DomainSOT.custom_fields` and matched by one closed runtime identity adapter.

The registered targets are `subscriber`, `project`, `support_ticket`, `work_order`, `lead`, `quote`, and `sales_order`, declared by their customer, workforce, support, and sales SOT domains. A value read or write requires both the relevant custom-field permission and the target module's native read or write permission. Work Order access also honors the exact reseller/region scope of the native grant. Unknown targets, scoped targets without an explicit scope adapter, and missing runtime adapters fail closed.

## Persistence

- `custom_field_definitions` stores tenant, target, stable key, type, options, validation, placement, sensitivity, and lifecycle state.
- `custom_field_values` stores one JSONB value for a definition and target identity. The owner normalizes the value according to the definition before persistence.
- Both tables use the operator tenant, composite tenant constraints, forced PostgreSQL RLS, and application-role grants.
- A definition is created as `draft`, explicitly activated, and retired instead of deleted.
- An active definition's structural contract is immutable in both the service and a database trigger. Labels and presentation settings remain editable.
- Definition and value commands stage audit evidence and value-free domain events in the owner transaction.

The JSONB value is a typed storage envelope, not an unvalidated property bag. Reporting/search indexing is deferred; a later projection can add type-specific indexes without changing the owning entity tables.

## Existing subscriber fields

`subscriber_custom_fields` and `/api/v1/subscribers/{id}/custom-fields` remain legacy-owned by `customer.accounts`. This change performs no backfill, reinterpretation, deletion, or dual-write. The hub displays the legacy surface as retained migration debt so operators do not mistake the two stores for one contract.

## Administration page contract

Settings Hub location: `/admin/system/settings-hub`, System category.

Canonical management route: `/admin/custom-fields`

Audience: tenant administrators and delegated configuration operators.

Primary task: understand registered modules, create and review a typed draft, activate it deliberately, and retire it without erasing prior values.

Information hierarchy:

1. Registry health and target counts.
2. Filterable field-definition inventory with lifecycle state and actions.
3. Code-owned module registry.
4. Existing-field ownership and migration status.

States:

- Unauthorized: the route returns 403 and the Settings Hub entry is absent.
- Hub-only: module health is visible, while definitions explain the additional read permission required.
- Empty: the page explains that no central definitions exist and offers a permitted create action.
- Invalid registry: the page presents registry errors and withholds safe authoring.
- Invalid form: the form returns 422 with a plain-language error and no partial write.
- Success: Post/Redirect/Get returns to the inventory with a status message.
- Retired: retained in the inventory, with no edit or activation action.

Responsive behavior: tables scroll horizontally without clipping controls; header actions stack; the form collapses from four/two columns to one; all interactive controls have at least a 40px target and visible keyboard focus.

Accessibility: labels are explicit, status is conveyed by text in addition to color, error/success messages use live semantic roles, destructive retirement is confirmed, and the established light/dark palettes retain WCAG AA contrast.

## Owning record surfaces

Active definitions marked for detail display or record editing appear on the owning subscriber, project, support-ticket, work-order, lead, quote, and sales-order detail pages. The detail flag controls read visibility and the form flag controls inline editing; list placement is retained as a future projection contract and is not rendered in this release. Those pages never create definitions; authorized operators only set or clear values. The shared renderer posts the registered target key and immutable record UUID back to the central owner. Fields are installation-specific database records, so separate deployments have independent definitions without source changes or schema migrations.

## API contract

- `GET /api/v1/custom-fields/{target_type}/{target_id}` returns active definitions and permitted values.
- `PUT /api/v1/custom-fields/{target_type}/{target_id}` sets or clears one definition value.
- Sensitive values are returned as redacted without `custom_fields:sensitive:read` and require `custom_fields:sensitive:write` to mutate.
- Event payloads include definition and target identity but never the custom-field value.

## Future module registration

A module becomes eligible only when one change set adds:

1. `CustomFieldTargetCapability` to its canonical `DomainSOT` declaration.
2. The matching target identity adapter.
3. Native read/write permissions and detail-path contract.
4. Registry, service, and UI integration tests.

No migration is needed merely to register another module or define another field.
