# Router control plane (`network.router_control`), PR1

Status: **PR1 of the router control plane. Read-only.** There is no storage, no
owner command, no settings, no task, no UI, no migration and no router contact.
PR1 delivers a generic composition engine, three read-only modules (`snmp`,
`ntp`, `services-hardening`) and a drift report. The design decisions D1-D8 were
approved by Michael on 2026-10-14 (default profiles as proposed; Michael the sole
approver with self-approval recorded; REST-only writes later; RouterOS 7.20.8 as a
floor with one canary router as the 7.23.8 test; admin tiers; unidentified hosts removed;
user management in the last phase only; retire the NAS bootstrap generator later).
The ADR for the full programme (ADR-0019) is PR2's.

## Goal

Routers are standardised by purpose. A router's desired state is **composed** from
weighted, tiered layers, and drift is the difference between that composed state
and the latest stored `/export`. Roles are not fixed: they are default profiles
(bundles of modules) that a router can combine.

## Layout

| Path | Role |
|---|---|
| `app/services/composition/` | the generic engine: pure, standard library plus pydantic, no `app` imports (import-linter contract and `tests/architecture/test_router_control_boundaries.py`). Built to be lifted into Starter unchanged |
| `app/services/router_control/` | the router plug-in: tiers, criteria dimensions, subject builder, modules, PROPOSED seed layers, report |
| `app/services/router_control/modules/` | `snmp`, `ntp`, `services_hardening` (code, never DB-authored templates) |
| `scripts/network/router_control_report.py` | the read-only CLI adapter |

## Running the report

```
python -m scripts.network.router_control_report [--router NAME ...]
    [--layers FILE] [--exceptions FILE] [--assignments FILE] [--sites FILE]
    [--today YYYY-MM-DD] [--max-age-hours 48] [--json]
```

It opens one `REPEATABLE READ, READ ONLY` session (`read_only_snapshot_session`),
reads active routers and each router's latest `RouterConfigSnapshot`, composes the
desired state and compares. It never contacts a router, never writes and never
prints export text, community names or secrets.

* Without `--layers` the desired state is the in-code seed in
  `seed_layers.py`: **PROPOSED, synthetic, not authoritative** (documentation
  ranges and invented site names). Real values belong in a private file passed with
  `--layers`; that file replaces the seed, it is not merged with it.
* Without `--assignments` a router's profile is its current health role from
  `network.router_health_policy` and its site comes from `--sites` (a
  `{router name: site}` JSON). With `--assignments`
  (`{"routers": {name: {site, profiles, tags, hardware_class, lifecycle,
  loopback_v4, loopback_v6}}}`) the listed routers use it. `tags` are **policy**
  tags; the inventory column `routers.tags` is never read.
* `--layers` takes `{"layers": [{id, tier, name, weight, criteria, kind, active,
  revision, document}]}`; `--exceptions` takes `{"exceptions": [{id, module, kind,
  reason, created_on, until, router | criteria, path, patch, scope, mode,
  approved, revoked}]}`. A `widening` key in the file is ignored: widening is
  computed (see Exceptions).
* Output per router: assigned profiles and the applicable layers in order; the
  effective state of each module with its origin (tier, layer, revision, op) and
  shadowed values; drift items; proposals (widening flagged); conflicts; inert
  paths; lapsed exceptions; per-module and router effective hashes.

Exit codes: **0** every evaluated module conforms; **1** drift; **2** refused
(invalid input, composition conflict, invalid desired state, an unparseable
export, or any unexpected error: a failure is never reported as drift);
**3** nothing could be evaluated (for example no snapshots).

* "Today" is the date in the application timezone (Africa/Lagos), not UTC, so an
  exception lapses at Lagos midnight.
* Drift in a module in `observe` mode, or on an `observe_only` router, is not
  enforced but it is still drift: it is labelled "observe-only" in the text and
  JSON (`observe_only_drift`) and it exits **1**. The design does not exempt
  observe mode from "drift", so this is the documented choice.
* The `ntp` self-reference check needs the router's loopback. Without
  `loopback_v4`/`loopback_v6` (always the case for a default assignment) the report
  says `NOT CHECKED` for that router and prints one note on stderr.

A production run is read-only and only on a host Michael names.

## Composition model (normative, from the design §1.4-1.8)

**Tiers**, applied in this order, later wins: `global` (rank 0) -> `site` (1) ->
`profile` (2; role weights 100-400, feature weights 500-900) -> `group` (3; policy
tags, profiles, hardware classes, sites) -> `router` (4; weight fixed 0) ->
exceptions (5). Layers are sorted by `(rank, weight, name, id)`.

**Matching** is NetBox's: a layer applies when, for every non-empty criteria
dimension, the subject's values intersect it (AND across dimensions, OR within
one). Inactive layers are skipped. The subject comes only from human-declared
bindings, never from observed values.

**Layer document**: `{"schema": 1, "modules": {"<key>": {"module_version": N,
"mode": "enforce|observe|off", "params": {...}}}}`. A module is enrolled when any
applicable layer sets its `mode` (a union; a layer cannot un-enrol, only set
`off`). Parameters of an unenrolled module are inert: ignored, listed, not
validated. A `module_version` that differs from the code gives
`invalid_desired(stale_layer)`.

**Merge rules**

* M1 objects: `{"$delete": true}` removes a key and records a tombstone; an empty
  object is a no-op.
* M2 scalars: replace; `null` only on nullable fields (an explicit unset).
* M3 `replace` lists replace wholesale.
* M4 `union` lists: a plain list is `$add`; `{"$add": [], "$remove": []}`;
  `{"$replace": []}`; canonical equality (CIDRs, host names).
* M5 `keyed(key)` lists merge element by key; `{"key": k, "$delete": true}`
  removes one element; `$replace` replaces all; an optional `order` field sorts.
* M6 directives are validated on write; M7 a module cannot be deleted.
* M8 every write is tracked per `(path, rank, weight)`. Two layers at the same
  rank that disagree are a **conflict** listing every layer involved (three-way
  ties name all three): the NetBox result (later name) is shown for display only,
  and planning is refused. Conflicts are: different scalar or `$replace` values;
  `$replace` against `$add`/`$remove` (non-restrictive lists); `$add` against
  `$remove` of one element (non-restrictive); keyed `$replace` against another
  keyed write; and **delete against set** for any container (scalar, replace
  list, union list, keyed element or list, or a new child of a deleted object).
  `tie: restrictive` fields never conflict and resolve **independently of layer
  names**: scalars take the more secure value; allow lists resolve to the
  intersection of every `$replace` list and of the `$add`/`$remove` result
  (`$remove` beats `$add`); deny lists resolve to the union (`$add` beats
  `$remove`). Layers of different rank or weight never conflict.
* M9 each enrolled module's result is validated against its strict schema and
  invariants. M10 the result is identical for any input order; hashes are over
  canonical JSON. M11 merging a layer twice equals once.

**Provenance**: every effective leaf has one origin (`tier, layer, revision,
weight, op`) plus the shadowed stack; defaults and derived values have their own
origin. **Derived fields** (for example `snmp.src_address` from the binding
loopback) are pure functions of other modules' effective values or subject facts,
never of observations; they cannot be set by a layer or exception.

**Exceptions** are dated (at most 180 days), reasoned (10 characters or more)
overrides, mode overrides, suppressions or annotations. Active while `today <=
until`; a widening exception also needs approval. `widening` is **computed** by
`compose()` by comparing the module before and after the exception, never read
from the input; an unapproved widening exception is not active
(`pending_approval`). After `until` the value returns and `exception_lapsed` is
reported through `until` + 7 days (the same window as the router-health policy).
Overrides cannot target derived fields.

**Widening** (what needs the approver once PR2 adds activation): a larger allow
list, a smaller deny list, a secure boolean flipped to insecure, or a security
module mode relaxed (`enforce > observe > off`). NTP server changes are not
widening. Detection walks objects, maps (by member key) and keyed lists (by
element key), so per-element ACLs are covered: a new element is compared with
"nothing granted" and a removed element loses its protections.

## Drift

Per module: `conform`, `drift`, `excepted`, `observe_only`, `off`, or
`not_evaluated(reason)` with reason `no_snapshot`, `stale`, `unparseable`,
`invalid_desired` or `blocked_by_version`. **Unobserved is unknown, never absent**:
no, stale or unparseable snapshots never produce `missing`. A field unsupported on
the router's version is `not_applicable` (for example `reverse-proxy` before RouterOS
7.22), not drift. Only fields present in the effective desired state are compared.

## Modules in PR1

| Module | Surface | Notes |
|---|---|---|
| `snmp@1` | `/snmp`, `/snmp community` | enabled, trap target, allowed sources (union allow list), write access, derived source address. Community names are never read or printed. Exposure items agree with the `snmp-exposed` health check |
| `ntp@1` | `/system ntp client`, `servers`, `/system ntp server` | client enabled, ordered server list (replace), optional server enablement. Invariant: the server list must not contain the router's own loopback. Observed extras become non-widening proposals |
| `services-hardening@1` | `/ip service` (telnet, ftp, www, api-ssl, reverse-proxy), `/ip dns`, `/ip proxy`, `/ip socks`, `/ip upnp`, `/tool bandwidth-server`, optional RoMON and MAC-Winbox | defaults for keys a compact export omits are marked [I] in code until real exports prove them. Agrees with `reverse-proxy-enabled`. `disabled_services` accepts only the known services; unknown names are refused, `winbox` is refused as owned by the later mgmt-access module, and `ssh`, `api` and `www-ssl` are refused as the management transport (lockout guard; PR1 has no per-router transport fact, so they are always treated as the transport) |

## Not in PR1

Storage, owner commands, approvals, activation, the compiled health policy, UI,
conformance projection, change sets, rendering and apply, the remaining modules
(management ACLs, input firewall, routing, tunnels, users, version), and ADR-0019.
These are PR2 onwards.

## Recorded debt

* **`ntp.self_srcnat`** (design section 1.2, an M-risk optional element of `ntp@1`) is
  not implemented. It is the router-side source-NAT element that lets a router
  reach its own NTP server and only matters once Sub renders and applies. Resolves
  when: the rendering phase lands the element model for `ntp`; until then drift
  never mentions it.
* **Rendering** (design section 2.1 element model, plans, approvals, apply) is
  deferred entirely. PR1 only observes. Resolves when: a render-only operator
  `.rsc` renderer and its tests land, with the first apply phase gated as in
  design section 2.7.
* The seed profiles in `seed_layers.py` are PROPOSED and synthetic; real values
  arrive in a private `--layers` file until PR2 adds reviewed storage.

## Guards

`tests/architecture/test_router_control_boundaries.py`: the composition core is
independent (guard 8, with the `Composition core must not import Sub` import-linter
contract), the plug-in is a read-only resolver with no router transport, no
inventory-tag reads (guard 11), a single `/export` parser (guard 5), complete field
specs and a consistent registry (guard 10, claims). The spec vectors T1-T25 are in
`tests/services/composition/test_spec_vectors.py`.
