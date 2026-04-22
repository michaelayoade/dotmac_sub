# OLT / ONT Data Model Proposal V1

**Status:** Draft  
**Date:** 2026-04-22  
**Depends on:** [OLT_ONT_OVERHAUL_V1.md](/opt/dotmac_sub/docs/OLT_ONT_OVERHAUL_V1.md:1)

## Purpose

Translate the overhaul direction into concrete model changes against the current SQLAlchemy schema.

This document answers:

- which existing models remain first-class
- which current models evolve into bundle-based concepts
- which ONT fields should become derived or override-only
- what should be added in the first migration slice

## Current Model Snapshot

Relevant current records:

- `OntUnit` in [app/models/network.py](/opt/dotmac_sub/app/models/network.py:1067)
- `OnuType` in [app/models/network.py](/opt/dotmac_sub/app/models/network.py:2025)
- `OntProvisioningProfile` in [app/models/network.py](/opt/dotmac_sub/app/models/network.py:2109)
- `OntProfileWanService` in [app/models/network.py](/opt/dotmac_sub/app/models/network.py:2256)
- `OntWanServiceInstance` in [app/models/network.py](/opt/dotmac_sub/app/models/network.py:2340)
- `VendorModelCapability` in [app/models/network.py](/opt/dotmac_sub/app/models/network.py:2452)
- `Vlan` in [app/models/network.py](/opt/dotmac_sub/app/models/network.py:396)
- `IpPool` in [app/models/network.py](/opt/dotmac_sub/app/models/network.py:532)

## Key Current Gaps

1. `OntProvisioningProfile` is doing too many jobs at once.
   It is partly a reusable template, partly an OLT bundle, partly a default-service definition, and partly a desired-state bridge to an ONT.

2. `OntUnit` still stores too much desired config directly.
   Examples:
   - `wan_vlan_id`
   - `wan_mode`
   - `config_method`
   - `ip_protocol`
   - `pppoe_username`
   - `mgmt_vlan_id`
   - `mgmt_ip_mode`
   - Wi-Fi defaults

   LAN intent is the current exception: it should remain ONT-local operator
   intent until there is a concrete reusable LAN bundle concept. It should not
   be folded into the OLT-scoped bundle/override model prematurely.

3. The system does not yet distinguish clearly between:
   - assigned desired bundle
   - ONT-level overrides
   - resolved effective config

4. `OnuType` is still too thin to drive capability-based UX and validation.

## Target Model Roles

## 1. Keep As Core Inventory Models

These remain first-class and are not conceptually replaced:

- `OLTDevice`
- `PonPort`
- `OntUnit`
- `OnuType`
- `Vlan`
- `IpPool`
- `SpeedProfile`
- `Tr069AcsServer`
- `Tr069CpeDevice`
- `VendorModelCapability`

## 2. Evolve Existing Models

### `OntProvisioningProfile` -> `OntConfigBundle`

Recommendation:

- keep the existing table initially
- rename the product concept in code and UI first
- delay physical table rename until after cutover

Why:

- it already carries much of the future bundle shape
- it is OLT-scoped
- it already groups WAN, management, speed, and device defaults

What changes conceptually:

- stop treating it as a loose “profile”
- treat it as the primary desired-state bundle attached to an ONT

### `OntProfileWanService`

Keep and reinterpret as a bundle-owned WAN service definition.

This already matches the target model well:

- grouped L2 and L3 behavior
- PPPoE templating
- service identity
- priority ordering

### `OntWanServiceInstance`

Keep, but make its lifecycle more explicit:

- instantiated from bundle service definitions
- owned by ONT desired-state execution context
- may eventually become part of an execution/applied-state layer rather than a user-edited model

## 3. Add New Models

### `OntBundleAssignment`

Purpose:

- explicit assignment record between ONT and bundle
- separates assignment history from the raw `OntUnit.provisioning_profile_id`

Suggested columns:

- `id`
- `ont_unit_id`
- `bundle_id`
- `status`
  - draft
  - planned
  - applying
  - applied
  - drifted
  - failed
  - superseded
- `assigned_by_person_id` or system-user equivalent
- `assigned_reason`
- `created_at`
- `updated_at`
- `applied_at`
- `superseded_at`

Suggested constraints:

- unique partial index on `(ont_unit_id)` where assignment is active

### `OntConfigOverride`

Purpose:

- store explicit per-ONT deviations from bundle defaults
- stop copying bundle fields into `OntUnit`

Suggested columns:

- `id`
- `ont_unit_id`
- `field_name`
- `value_json`
- `source`
  - operator
  - workflow
  - subscriber-data
- `reason`
- `created_at`
- `updated_at`

Suggested constraints:

- unique `(ont_unit_id, field_name)`

This should remain intentionally narrow and only allow overrides for an allowlist of fields.

### `OntConfigPlan`

Purpose:

- persist a generated desired-vs-current execution plan

Suggested columns:

- `id`
- `ont_unit_id`
- `assignment_id`
- `status`
  - draft
  - queued
  - running
  - succeeded
  - failed
  - canceled
- `plan_payload`
- `risk_summary`
- `created_at`
- `started_at`
- `completed_at`

### `OntConfigVerification`

Purpose:

- persist verification results independently of execution

Suggested columns:

- `id`
- `ont_unit_id`
- `assignment_id`
- `status`
  - pending
  - verified
  - drifted
  - failed
- `verification_payload`
- `observed_at`
- `created_at`

## Proposed Mapping From Current Fields

## `OntUnit`

### Keep On `OntUnit`

These are identity, inventory, topology, or observed-state fields and should remain:

- serial / vendor / model identifiers
- status fields
- `olt_device_id`
- `onu_type_id`
- board / port / splitter / topology
- subscriber-facing identity fields
- `provisioning_status`
- `authorization_status`
- observed runtime snapshots
- TR-069 registration fields
- sync metadata

### Move To Bundle Ownership Over Time

These should stop being directly edited as primary desired-state fields on `OntUnit`:

- `wan_vlan_id`
- `wan_mode`
- `config_method`
- `ip_protocol`
- `mgmt_ip_mode`
- `mgmt_vlan_id`
- `mgmt_remote_access`
- `voip_enabled`
- `download_speed_profile_id`
- `upload_speed_profile_id`
- `tr069_acs_server_id`
- `tr069_olt_profile_id`
- Wi-Fi defaults
- LAN defaults

### Move To Overrides Only

These may remain on `OntUnit` temporarily for compatibility, but the target meaning should be “resolved override or cached effective value”, not the primary source of truth:

- `pppoe_username`
- `pppoe_password`
- `mgmt_ip_address`
- `wifi_ssid`
- `wifi_password`
- `lan_gateway_ip`
- `lan_subnet_mask`
- `lan_dhcp_*`

### Recommendation For Transition

Phase the transition like this:

1. leave the columns in place
2. make the resolver treat assignment + overrides as authoritative
3. make legacy fields write-through or read-through compatibility fields
4. deprecate direct writes
5. remove or repurpose selected columns later

## `OnuType`

Current `OnuType` is useful but too small for the new UX.

### Keep Existing Fields

- `name`
- PON / GPON defaults
- port counts
- `capability`
- `allow_custom_profiles`

### Add Capability References

Do not duplicate the entire capability matrix into `OnuType` if `VendorModelCapability` is the true hardware fact table.

Preferred shape:

- `OnuType` references a `VendorModelCapability`
- plus optional UI/policy flags local to DotMac

Suggested additions:

- `vendor_model_capability_id`
- `default_bundle_id` nullable
- `supports_bundle_overrides`
- `notes`

## `VendorModelCapability`

This should become a primary driver for feature gating in the new UI.

Current capability fields are a good start, but the new UI needs more operator-facing gating.

Suggested additions:

- `supports_wifi`
- `supports_lan_dhcp`
- `supports_pppoe_tr069_push`
- `supports_wan_vlan_tr069`
- `supports_mgmt_ip_omci`
- `supports_voip_config`
- `supports_remote_acl`
- `supports_reboot_tr069`
- `supports_reboot_omci`

These can be added gradually or derived through adapters until formalized in schema.

## `OntProvisioningProfile` / Future Bundle

### Keep As Bundle Core

These fields already fit the future bundle model:

- `olt_device_id`
- `name`
- `description`
- `profile_type` -> future `bundle_kind`
- `config_method`
- `onu_mode`
- `ip_protocol`
- speed profiles
- management plane fields
- TR-069 / OLT profile fields
- VoIP defaults
- active/default metadata

### Fields To Reconsider

- `owner_subscriber_id`
  This looks like a special ownership scope not aligned with the new bundle model.
  Recommendation: keep for now, but do not make subscriber-owned bundles central to V1.

- `is_default`
  Replace long term with an explicit default-assignment policy model.

### Suggested New Bundle Columns

Add incrementally to the current `ont_provisioning_profiles` table:

- `bundle_kind`
- `ont_type_id` nullable
- `execution_policy_json`
- `required_capabilities_json`
- `supports_manual_override`
- `cloned_from_bundle_id`

## Immediate Schema Strategy

Do not rename the current `ont_provisioning_profiles` table in the first phase.

Instead:

1. Keep `ont_provisioning_profiles` as the physical table.
2. Introduce service aliases in code:
   - `OntConfigBundleService`
   - `bundle` terminology in UI and docs
3. Add assignment and override tables.
4. Move new UI and new write paths to the new services first.

This avoids a high-risk rename while the old routes still exist.

## Recommended First Migration Slice

If implementation starts now, the first schema migration should add:

1. `ont_bundle_assignments`
2. `ont_config_overrides`
3. new columns on `ont_provisioning_profiles` for bundle semantics
4. optional `vendor_model_capability_id` on `onu_types`

Do not remove or rename current `OntUnit` desired-state columns yet.

## Proposed First Migration Tables

### `ont_bundle_assignments`

Suggested indexes:

- `(ont_unit_id, status)`
- `(bundle_id, status)`
- unique partial active-assignment index on `ont_unit_id`

### `ont_config_overrides`

Suggested indexes:

- unique `(ont_unit_id, field_name)`
- `(field_name)`

### `ont_config_plans`

Suggested indexes:

- `(ont_unit_id, status)`
- `(assignment_id, status)`

### `ont_config_verifications`

Suggested indexes:

- `(ont_unit_id, status)`
- `(assignment_id, observed_at desc)`

## Service Contract Changes

Once the first migration exists, introduce these service APIs:

### `bundle_service`

- `list_bundles(olt_id, ont_type_id=None, active_only=True)`
- `get_bundle(bundle_id)`
- `create_bundle(payload)`
- `clone_bundle(bundle_id, payload)`
- `update_bundle(bundle_id, payload)`
- `bundle_usage(bundle_id)`

### `bundle_assignment_service`

- `assign_bundle(ont_id, bundle_id, assigned_by, reason=None)`
- `supersede_assignment(assignment_id)`
- `active_assignment_for_ont(ont_id)`
- `assignment_history_for_ont(ont_id)`

### `ont_override_service`

- `set_override(ont_id, field_name, value, source, reason=None)`
- `clear_override(ont_id, field_name)`
- `list_overrides(ont_id)`

### `effective_ont_config_resolver`

- `resolve(ont_id)`
- returns sectioned config plus source attribution

## Read Model Proposal

The new ONT page should not read dozens of columns directly from `OntUnit` and related tables in templates.

Instead, build one read model:

- `EffectiveOntConfigView`

Sections:

- identity
- assignment
- capabilities
- service plane
- management plane
- device plane
- observed state
- drift summary

This becomes the contract for the new ONT UI.

## Backward Compatibility Plan

During the transition:

1. Existing UI continues to read legacy `OntUnit` fields.
2. New bundle assignment writes can optionally mirror selected resolved fields back to `OntUnit` for compatibility.
3. The resolver becomes the source of truth for new pages.
4. Legacy field writes are gradually blocked or rewritten into overrides.

This prevents a flag day migration.

## What Not To Do In Phase 1

- do not remove `OntUnit.provisioning_profile_id` yet
- do not drop direct desired-state columns yet
- do not rename `OntProvisioningProfile` table immediately
- do not rewrite all provisioning execution to use only plans before read models exist

## Recommended Next Implementation Step

The most practical next move is:

1. add `ont_bundle_assignments`
2. add `ont_config_overrides`
3. add bundle semantics to `ont_provisioning_profiles`
4. implement `EffectiveOntConfigResolver`

That creates the backbone needed for the new OLT/ONT UI without breaking current operations.
