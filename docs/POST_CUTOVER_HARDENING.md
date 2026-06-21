# Post-Cutover Hardening

Production is fully cut over from Splynx. Splynx data may remain as historical
import metadata or a read-only alias, but Splynx is no longer an authority,
writer, scheduler, provisioning source, balance source, or identity source.

This project removes migration scaffolding without changing unrelated launch
states. In particular, billing data authority and billing automation are
separate concerns:

- Billing data authority is local. Splynx deposits are import history only.
- Billing automation remains a separate go-live decision. Enabling prepaid
  drawdown is a launch with revenue impact, not a cleanup step.

## Guardrails

Every slice needs an exit invariant and a guard test. A slice is not done when a
flag is renamed or deleted; it is done when the build fails if the migration
gate comes back.

The first guardrails are:

- import boundaries forbid imports from retired Splynx migration runtime modules;
- enabled scheduled tasks must not target Splynx task names;
- new source-of-truth gates such as `trust_*`, `*_cutover_enabled`,
  `shadow_write_*`, or `use_splynx_*` are blocked unless explicitly allow-listed
  as known remaining cutover debt.

## Slices

### Billing Data Hardening

Remove Splynx deposit fallbacks, dual-run comments, and shadow reconcilers from
active billing paths. Do not flip billing automation as part of this slice.

Exit invariant: local ledger/account transactions are the only active prepaid
balance source.

Guard test: active billing paths cannot read a Splynx deposit field as the
available balance.

### IP Authority

Verify assignment drift is effectively zero, then make desired IP reconciliation
derive unconditionally from `IPAssignment`. Delete `trust_ipam` once the
invariant is live.

Exit invariant: `IPAssignment` is the desired IP authority for provisioning and
RADIUS projection.

Guard test: provisioning cannot branch on `trust_ipam`.

Parallel track: fix asymmetric release of subscriber IP assignments. That leak
is a lifecycle bug, not a Splynx cleanup task, and it can regrow drift while the
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

Treat local subscriber and subscription IDs as canonical. Splynx IDs may remain
read-only aliases for imported records, not defaults for new pushes or routing.
Imported-deleted-row filters belong in the same slice because they are the same
identity/provenance problem.

Exit invariant: new CRM writes and normal subscriber queries do not depend on
Splynx as the external identity authority.

Guard test: no new CRM push defaults to Splynx as the external system.

### Network Monitoring And Topology

If Zabbix/local inventory is canonical, Splynx monitoring identifiers become
legacy metadata only.

Exit invariant: topology reconciliation does not treat Splynx monitoring data as
an active source.

Guard test: no scheduled or import path can refresh topology from Splynx.
