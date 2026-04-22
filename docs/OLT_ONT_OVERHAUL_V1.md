# OLT / ONT Overhaul V1

**Status:** Draft  
**Date:** 2026-04-22  
**Audience:** Product, backend, frontend, provisioning, operations

## Goal

Rebuild the OLT and ONT sections around the operator mental model of:

- OLT-scoped resources
- reusable configuration bundles
- one-time assignment of a bundle to an ONT
- clear separation of desired state, observed state, and execution state

The target is not a cosmetic redesign. The target is a simpler and stricter operating model than the current mix of standalone CRUD screens and field-level edits.

## Primary Lessons From SmartOLT

The useful lessons are structural, not visual.

1. Operators think in OLT-local bundles, not in globally-scattered CRUD records.
2. VLANs, management IPs, uplink tagging, TR-069 profiles, and service-port behavior belong to one OLT configuration domain.
3. ONT pages should summarize effective state and operational state, not just expose many unrelated form fields.
4. Every editable network resource should show usage and blast radius before change.
5. “Set config once” means assigning a reusable bundle, not manually setting 15 fields per ONT.

## Problems In The Current Model

Current pain points in the codebase:

- Desired state and observed state are mixed in the same surfaces.
- Configuration writes happen through multiple fragmented service paths.
- Operators still need to mentally compose VLANs, pools, profiles, ONT type behavior, and provisioning settings.
- The UI exposes too much field-level editing before there is a clear effective configuration model.
- Some resources are shown as generic CRUD objects even when they are operationally OLT-local.

## Product Principles

The rebuilt sections should follow these rules:

1. OLT-scoped resources are managed primarily from the owning OLT.
2. ONTs receive one active configuration bundle plus optional overrides.
3. The UI always distinguishes:
   - desired config
   - observed config
   - execution status
4. Capability limits are explicit.
   Unsupported settings are hidden or marked unsupported by ONT type and management plane.
5. All change screens show usage before destructive edits.
6. All config application flows produce a plan, not direct scattered mutations.

## Domain Model

### 1. Inventory Layer

These records describe what exists physically or logically in the access network.

- `OLTDevice`
- `PonPort`
- `OntUnit`
- `OntType`
- `VendorCapability`
- `Tr069CpeDevice`
- observed runtime entities from SNMP, SSH, OMCI, TR-069

### 2. OLT-Scoped Resource Layer

These records belong to one OLT and are not treated as global operator abstractions.

- `Vlan`
- `IpPool`
- service-port pool / allocation policy
- OLT line profile
- OLT service profile
- OLT TR-069 profile
- remote ACL
- VoIP profile
- uplink tagging policy

Rule:

- if a resource is used by ONT provisioning and differs by OLT, it should be modeled as OLT-scoped

### 3. Bundle Layer

Introduce a first-class `OntConfigBundle` model.

A bundle is the operator-facing “set this once” unit.

Suggested fields:

- `id`
- `name`
- `description`
- `olt_device_id`
- `ont_type_id` nullable
- `is_active`
- `priority` nullable
- `bundle_kind`
  - residential
  - business
  - voice
  - bridge
  - custom
- `execution_policy`
  - preferred channel
  - fallback behavior
  - verification expectations

Bundle sections:

- `service_plane`
  - wan mode
  - wan vlan id
  - config method
  - ip protocol
  - PPPoE source strategy
  - service-port defaults
  - speed profile ids

- `management_plane`
  - mgmt ip mode
  - mgmt vlan id
  - mgmt ip pool id
  - tr069 acs server id
  - tr069 olt profile id
  - remote access defaults

- `device_plane`
  - wifi enabled
  - wifi templates / defaults
  - LAN defaults
  - DHCP defaults
  - VoIP defaults

- `constraints`
  - supported ONT types
  - required capabilities
  - required OLT vendor / model if needed

### 4. Assignment Layer

Introduce a first-class `OntBundleAssignment` or make the current ONT provisioning profile reference evolve into this concept.

Suggested fields:

- `ont_unit_id`
- `bundle_id`
- `assigned_at`
- `assigned_by`
- `status`
  - draft
  - planned
  - applying
  - applied
  - drifted
  - failed

Optional overrides:

- `OntConfigOverride`
  - only stores fields that differ from the assigned bundle
  - avoids copying the whole bundle onto the ONT

### 5. Observed State Layer

Desired config must not be overwritten by runtime discoveries.

Observed state stays in:

- `tr069_last_snapshot`
- `olt_observed_snapshot`
- dedicated observed-state tables if needed later

Observed state is read-only from the UI except for sync actions.

## Hard Invariants

These should be enforced in schema or shared validation services.

1. VLANs used by an ONT must belong to the ONT’s OLT.
2. IP pools used by an ONT or bundle must belong to the same OLT.
3. A bundle must reference only resources from its owning OLT.
4. An ONT may have at most one active bundle assignment.
5. Overrides must only override fields allowed by capability and bundle policy.
6. Observed state cannot directly change desired state.

## Effective Config Resolution

Introduce one resolver service:

- `EffectiveOntConfigResolver`

It returns the final desired ONT config with source attribution for every field.

Resolution order:

1. OLT defaults
2. ONT type defaults
3. assigned bundle
4. ONT overrides

Observed state is not part of desired-state resolution. It is compared after resolution.

Resolver output should include:

- final value
- source
  - olt_default
  - ont_type_default
  - bundle
  - override
- support status
  - supported
  - unsupported by type
  - unsupported by channel

## Execution Model

Do not let pages mutate many fields directly and hope downstream services reconcile them.

Introduce these boundaries:

### 1. Planner

`OntConfigPlanner`

Input:

- effective desired config
- current observed / known applied state

Output:

- list of operations
- risk summary
- dependencies
- verification expectations

### 2. Executor

`OntConfigExecutor`

Responsibilities:

- dispatch SSH / OMCI / TR-069 / Celery work
- record step execution
- expose progress

### 3. Verifier

`OntConfigVerifier`

Responsibilities:

- compare desired state against observed state
- mark:
  - verified
  - pending observation
  - drifted
  - failed

## Capability Model

ONT type and management channel should drive what operators can set.

Suggested capability matrix:

- supports_wifi
- supports_lan_dhcp
- supports_pppoe_push_tr069
- supports_wan_vlan_tr069
- supports_mgmt_ip_omci
- supports_voip
- supports_remote_acl
- supports_reboot_tr069
- supports_reboot_omci

This should drive:

- visible form sections
- validation
- plan generation
- verification expectations

## Information Architecture

## OLT Section

Primary tabs:

1. `Overview`
   - health
   - core status
   - ONT counts
   - quick actions
   - network resources summary

2. `Network Resources`
   - VLANs
   - uplink tagging
   - IP pools
   - remote ACLs
   - TR-069 profiles
   - VoIP profiles

3. `Bundles`
   - bundle list
   - usage counts
   - compatibility
   - create/edit/clone

4. `ONT Inventory`
   - configured
   - unconfigured
   - autofind
   - filters by ONT type, bundle, VLAN, signal, status

5. `Operations`
   - CLI
   - backups
   - sync
   - task history
   - events

## ONT Section

Primary tabs:

1. `Summary`
   - identity
   - OLT / PON
   - subscriber
   - ONT type
   - online / signal / alarms

2. `Effective Config`
   - resolved bundle + overrides
   - source attribution per section

3. `Observed State`
   - runtime WAN
   - TR-069
   - OLT-side state
   - last inform / last verification

4. `Drift`
   - desired vs observed diff
   - suggested actions

5. `Actions`
   - assign bundle
   - reprovision
   - reboot
   - resync
   - selective push

6. `History`
   - assignment history
   - config change log
   - provisioning execution log

## ONT Type Section

Primary tabs:

1. `Identity`
2. `Capabilities`
3. `Recommended Bundles`
4. `Compatibility`
5. `Provisioning Notes`

## Bundle Section

Primary tabs:

1. `Overview`
2. `Service Plane`
3. `Management Plane`
4. `Device Plane`
5. `Compatibility`
6. `Usage`
7. `History`

## Key Workflows

### Workflow 1: Prepare OLT

Operator task:

- define OLT-local VLANs
- bind IP pools
- configure TR-069 profile
- review uplink and management readiness

Expected outcome:

- OLT network resource page shows green readiness for provisioning

### Workflow 2: Create Bundle Once

Operator task:

- create one bundle for a given OLT and ONT type

Expected outcome:

- bundle becomes assignable to all compatible ONTs on that OLT

### Workflow 3: Assign Bundle To ONT

Operator task:

- select ONT
- select bundle
- preview plan
- apply

Expected outcome:

- ONT gets one active assignment
- planner generates steps
- executor runs steps
- verifier tracks result

### Workflow 4: Change Shared Resource Safely

Operator task:

- open VLAN or bundle
- inspect usage
- understand blast radius
- edit or clone

Expected outcome:

- no surprise live impact

## CRUD Policy

Not every object gets equal-weight standalone CRUD.

### Full First-Class CRUD

- OLTs
- ONTs
- ONT Types
- Bundles
- OLT-scoped VLANs
- OLT-scoped IP pools

### Embedded / Scoped CRUD

Manage mostly inside owning OLT:

- uplink tagging
- remote ACLs
- VoIP profiles
- OLT TR-069 profiles
- service-port pool policies

### Read-Mostly / Controlled Mutation

- observed runtime state
- last snapshots
- drift records

## UX Rules

1. Every OLT-scoped resource page shows:
   - owner OLT
   - ONT usage
   - bundle usage
   - IP pool or VLAN dependencies where relevant

2. ONT pages show sections, not flat forms.

3. “Edit effective config” should normally mean:
   - change assigned bundle
   - or add explicit override
   not mutate many raw ONT columns directly

4. Preview before apply.

5. Prefer clone-over-edit for high-impact shared bundles.

## Backend Refactor Targets

Before or during the UI overhaul, introduce these services:

- `olt_network_resources_service`
- `ont_bundle_service`
- `effective_ont_config_resolver`
- `ont_config_planner`
- `ont_config_executor`
- `ont_config_verifier`
- `network_dependency_service`

These services should become the only allowed path for new OLT/ONT UI writes.

## Migration Strategy

Use a strangler migration, not a big-bang replacement.

### Phase 1: Domain Freeze

- finalize bundle model
- finalize invariants
- finalize resolver precedence

### Phase 2: Read Model

- build new OLT network resources read service
- build effective ONT config read service
- build dependency summaries

### Phase 3: New Bundle Model

- add bundle schema
- add assignment model
- add overrides model if needed

### Phase 4: New OLT UI

- ship OLT Overview
- ship OLT Network Resources
- ship OLT Bundles

### Phase 5: New ONT UI

- ship Summary
- ship Effective Config
- ship Observed State
- ship Drift

### Phase 6: New Write Paths

- assign bundle
- reprovision
- selective override
- deprecate fragmented legacy writes

### Phase 7: Cutover

- old routes redirect to new views
- legacy forms become read-only or removed

## Suggested Initial Deliverables

If implementation starts immediately, build in this order:

1. `OLT Network Resources` page
2. `Bundle` model and CRUD
3. `EffectiveOntConfigResolver`
4. `New ONT detail page`
5. `Assign bundle` flow with preview and execution tracking

This order gives the highest operational value early and prevents the UI from outrunning the domain model.

## Out Of Scope For V1

- complete visual redesign of all network pages
- replacing every legacy model in one migration
- advanced multi-bundle composition
- per-subscriber dynamic bundle templating
- fully generic rules engine

## Open Questions

These need explicit decisions before full implementation:

1. Should an ONT be allowed exactly one active bundle, or a base bundle plus additive overlays?
2. Should Wi-Fi defaults live inside the bundle or in a separate device template attached to the bundle?
3. Should PPPoE credentials be operator-entered overrides or linked from subscriber/service records only?
4. Which fields are legal ONT-level overrides versus bundle-only fields?
5. Should OLT defaults be explicit records or inferred from the most common bundle/resource set?

## Recommendation

For V1, keep the operating model strict:

- one active bundle per ONT
- OLT-scoped resources only
- explicit overrides only for a small allowlist
- resolver-driven effective config
- planner/executor/verifier split for all new writes

That is the most practical way to deliver a complete overhaul without rebuilding the current complexity in a cleaner-looking shell.
