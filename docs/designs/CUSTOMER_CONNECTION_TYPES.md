# Managed Customer Connection Types

Status: implemented

## Purpose

Customer connection type is the explicit business classification used to group
customers by their last-mile medium. Installations may need classifications
beyond Wireless and Wired, including Both. The classification is never inferred
from PPPoE, DHCP, IPoE, NAS, or credential settings because those facts do not
prove the physical last-mile medium.

## Authority

`customer.connection_types` owns reusable type definitions. Each definition has
an immutable code, editable display name and description, display order, and an
active state. `customer.accounts` owns the selected code on each Subscriber and
accepts only a definition recognized by the configuration owner.

Definitions already assigned to customers are deactivated rather than deleted.
Deactivation removes the definition from new selections while preserving its
label and every existing customer assignment. A customer may also have no
classification, displayed as Not specified.

## Admin page contract

- Screen: `/admin/system/customer-connection-types`, setup/editor page.
- Audience: administrators with `system:settings:read`; mutations require
  `system:settings:write`.
- Job: review usage, add a reusable type, edit its presentation, activate it, or
  deactivate it.
- Read owner and command owner: `customer.connection_types`.
- First viewport: configured types, active state, assigned customer count, and
  the add/edit form.
- Empty state: explain that no types are configured and expose the add form to
  authorized administrators.
- Destructive action: deactivation only. Existing assignments remain readable.
- Audit evidence: each create, update, or deactivate command stages
  `customer.connection_type_changed` through the durable event dispatcher.

The Settings Hub exposes this page in the System section as **Setup Customer
Connection Types**.

## Customer form and detail contract

Customer create and edit forms show active definitions plus Not specified. When
an existing customer has an inactive definition, that current value remains
visible until an administrator selects another active value or clears it.

The admin customer detail page displays the configured name. Administrators
with `customer:write` can click the value, including Not specified, to open the
customer edit form at the connection-type field.

## Reporting

Regional performance reads the configured definitions on each request and
produces one column per definition plus Unspecified. Inactive definitions remain
in the report so historical customer assignments are not hidden. Unknown codes
are treated as drift and counted under Unspecified; the database foreign key
prevents new unknown assignments.

## Migration and repair

Migration `671_customer_connection_type_definitions` creates the definition
table, seeds Wireless and Wired, expands `subscribers.connection_type`, removes
the old two-value check constraint, and installs a foreign key to the stable
definition code. Existing values are preserved. Re-running the migration is
idempotent for a fresh baseline created from current model metadata.

Migration `683_field_lifecycle_connection_types_merge` joins this history with
the field lifecycle revisions without renumbering either branch or changing
data. Migration `684_customer_connection_type_defaults` then makes the existing
`sort_order=0` and `is_active=true` server defaults explicit on every installation.
An earlier metadata bootstrap could create the table without those defaults
before migration 671 saw the existing table. The ORM declaration now agrees
with the migration-owned defaults; the connection-type command owner continues
to validate and audit configured definitions.

The convergence alters column defaults only, without rewriting existing values
or customer assignments. Its downgrade preserves the defaults because revision
671 already declares them and the previous installation path cannot be inferred
from the current schema. Fresh and real predecessor migration rehearsals compare
the resulting schema, and a native PostgreSQL insert proves omitted values use
the defaults without depending on ORM insert behavior.
