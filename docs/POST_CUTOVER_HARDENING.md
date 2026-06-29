# Post-Cutover Hardening

Production is fully cut over from the legacy billing platform. Imported data may
remain as historical metadata or a read-only alias, but it is no longer an authority,
writer, scheduler, provisioning source, balance source, or identity source.

This project removes migration scaffolding without changing unrelated launch
states. In particular, billing data authority and billing automation are
separate concerns:

- Billing data authority is local. Imported deposits are history only.
- Billing automation remains a separate go-live decision. Enabling invoice
  generation, dunning, or autopay is a launch with revenue impact, not a
  cleanup step.

## Guardrails

Every slice needs an exit invariant and a guard test. A slice is not done when a
flag is renamed or deleted; it is done when the build fails if the migration
gate comes back.

The first guardrails are:

- import boundaries forbid imports from retired migration runtime modules;
- enabled scheduled tasks must not target retired migration task names;
- new source-of-truth gates such as `trust_*`, `*_cutover_enabled`,
  `shadow_write_*`, or `use_legacy_*` are blocked unless explicitly allow-listed
  as known remaining cutover debt.

## Slices

### Billing Data Hardening

Remove imported-deposit fallbacks, dual-run comments, and shadow reconcilers from
active billing paths. Do not flip billing automation as part of this slice.

Exit invariant: local ledger/account transactions are the only active prepaid
balance source.

Guard test: active billing paths cannot read an imported deposit field as the
available balance.

### IP Authority

Verify assignment drift is effectively zero, then make desired IP reconciliation
derive unconditionally from `IPAssignment`. Delete `trust_ipam` once the
invariant is live.

Exit invariant: `IPAssignment` is the desired IP authority for provisioning and
RADIUS projection.

Guard test: provisioning cannot branch on `trust_ipam`.

Parallel track: fix asymmetric release of subscriber IP assignments. That leak
is a lifecycle bug, not a legacy cleanup task, and it can regrow drift while the
hardening slices are in flight.

### Access Enforcement

This has two sub-cutovers:

- prove group routing by shadow-compare, then make group routing canonical;
- prove desired-IP projection from `IPAssignment`, then make that projection
  canonical.

Only remove `_shadow_write_access_state` after the direct path has proven it
matches production behavior.

Exit invariant: access enforcement has one canonical RADIUS projection path.

Guard test: no new shadow access-state writer can be added.

### CRM And Imported Identity

Treat local subscriber and subscription IDs as canonical. Imported IDs may remain
read-only aliases for imported records, not defaults for new pushes or routing.
Imported-deleted-row filters belong in the same slice because they are the same
identity/provenance problem.

Exit invariant: new CRM writes and normal subscriber queries do not depend on
the retired platform as the external identity authority.

Guard test: no new CRM push defaults to the retired external system.

### Network Monitoring And Topology

If Zabbix/local inventory is canonical, imported monitoring identifiers become
legacy metadata only.

Exit invariant: topology reconciliation does not treat imported monitoring data as
an active source.

Guard test: no scheduled or import path can refresh topology from retired systems.
