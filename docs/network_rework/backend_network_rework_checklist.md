# Backend Network Rework Checklist

Last updated: 2026-05-25

## Scope

This checklist tracks the backend-only ONT/OLT/TR-069 provisioning rework.

Rollback scope is limited to the network slice:

- `app/services/network/**`
- network API routes
- network web routes/templates
- network-specific tests
- network-specific migrations
- explicit shared files touched for the network state model

## Phase 0 Baseline

- Reference commit SHA: `591f45ef3165062b9682b3a5844a926db737e4e9`
- Network slice manifest: `docs/network_rework/phase0_network_slice_manifest.txt`
- Network slice patch snapshot: `docs/network_rework/phase0_network_slice_snapshot.patch`
- Snapshot notes:
  - The manifest is intentionally broad and includes the current backend network surface plus related provisioning, ACS, TR-069, OMCI, OLT, and config-pack files.
  - The patch snapshot captures the pre-existing dirty worktree for the scoped network slice so later rollback decisions can distinguish this rework from unrelated app work.

### Pre-existing Dirty Files In Scope

These files were already modified or untracked before this rework began and must be treated carefully during selective rollback:

- `app/services/network/olt_dependency_preflight.py`
- `app/services/network/olt_ssh_ont/tr069.py`
- `app/services/network/ont_action_wan.py`
- `app/services/network/ont_crud.py`
- `app/services/network/ont_olt_context.py`
- `app/services/network/ont_provision_steps.py`
- `app/services/network/ont_provisioning/context.py`
- `app/services/network/ont_scope.py`
- `app/services/network/reconcile/__init__.py`
- `app/services/network/reconcile/actions.py`
- `app/services/network/reconcile/adapters.py`
- `app/services/network/reconcile/applier.py`
- `app/services/network/reconcile/core.py`
- `app/services/network/reconcile/planner.py`
- `app/services/network/reconcile/readers/acs_reader.py`
- `app/services/network/reconcile/state.py`
- `templates/admin/network/olts/detail.html`
- `templates/admin/network/onts/index.html`
- `tests/test_auth_dependencies.py`
- `tests/test_celery_tasks.py`
- `tests/test_customer_portal_gaps.py`
- `tests/test_genieacs_service_actions.py`
- `tests/test_log_regressions.py`
- `tests/test_network_monitoring_services.py`
- `tests/test_network_olts_inventory_scope.py`
- `tests/test_olt_dependency_preflight.py`
- `tests/test_ont_status_service.py`
- `tests/test_reconcile_adapters.py`
- `tests/test_reconcile_applier.py`
- `tests/test_reconcile_planner.py`
- `tests/test_reconcile_readers.py`
- `tests/test_reconcile_state.py`
- `tests/test_tr069_binding_readback.py`
- `tests/test_tr069_gaps.py`
- `tests/test_web_system_settings_hub.py`
- `alembic/versions/103_add_admin_whats_new_items.py`
- `alembic/versions/104_add_topup_intents.py`
- `tests/test_admin_whats_new.py`
- `tests/test_customer_portal_topup_flow.py`
- `tests/test_feature_surface_smoke.py`

## Provisioning Authority Chain

1. `config_pack` resolution
2. OLT apply
3. management path apply
4. OMCI/WAN/TR-069 apply
5. OLT/OMCI readback verify
6. ACS verify

## Canonical Failure Classes

- `config_pack_missing`
- `config_pack_mismatch`
- `config_pack_incomplete`
- `regional_pack_resolution_failure`
- `service_port_conflict`
- `management_path_missing`
- `omci_apply_failure`
- `omci_readback_failure`
- `tr069_binding_readback_miss`
- `acs_bootstrap_timeout`
- `olt_state_conflict`
- `vendor_capability_gap`
- `transport_timeout`

## Current Confirmed Mismatches

- Python enum includes `pending_acs_registration` and `pending_service_config`.
- Phase 1 worktree updates now add pending-state transition support in `app/services/network/ont_status.py`.
- Phase 1 worktree updates now add `alembic/versions/105_add_pending_ont_provisioning_statuses.py` for the live DB enum.
- Phase 1 worktree updates now align `alembic/versions/squashed_schema.sql` with `partial` plus the pending states.
- Phase 2 worktree updates now add an explicit config-pack resolution stage and per-run snapshot persistence through provisioning events.
- Phase 3 worktree updates now preserve domain-level outcomes for apply and verify stages even when the overall provisioning step fails later in the flow.
- Phase 4 worktree updates now classify delayed ACS/TR-069 verification outcomes as pending states instead of hard failures.

## Phase Checklist

- [x] Phase 0: rollback baseline and provisioning contract
- [x] Phase 1: provisioning status model alignment
- [x] Phase 2: explicit config-pack resolution stage
- [x] Phase 3: split provisioning into domains
- [x] Phase 4: convert false hard failures into pending states
- [ ] Phase 5: async verification workers
- [ ] Phase 6: regional config-pack validation and drift checks
- [ ] Phase 7: OMCI-aware recovery logic
- [ ] Phase 8: OLT + region policy profiles
- [ ] Phase 9: ONT model capability profiles
- [ ] Phase 10: fleet hygiene and historical intelligence

## Phase Log

### Phase 0

- Status: complete
- Test status: not applicable
- Deliverables:
  - network slice manifest frozen
  - network slice patch snapshot frozen
  - rollback scope and pre-existing dirty files recorded
  - provisioning authority chain documented
  - canonical failure classes documented
- Rollback note:
  - Phase 0 is documentation-only. No runtime rollback required.

### Phase 1

- Status: complete
- Test status: passed
- Deliverables:
  - provisioning transition map updated to cover `drift_detected`, `pending_acs_registration`, and `pending_service_config`
  - live DB enum migration added in `alembic/versions/105_add_pending_ont_provisioning_statuses.py`
  - squashed schema reference updated for `ontprovisioningstatus`
  - targeted transition tests added in `tests/test_ont_provisioning_status_transitions.py`
- Rollback note:
  - revert only the status enum migration, transition logic, and status-writing changes

### Phase 2

- Status: complete
- Test status: passed
- Deliverables:
  - explicit config-pack resolution helper added in `app/services/network/config_pack_resolution.py`
  - `apply_authorization_baseline` now persists a `resolve_effective_config_pack` event before preflight or OLT writes
  - per-run snapshot now records sanitized raw pack data, resolved pack data, effective values, and validation details
  - incomplete or mismatched config packs now stop the baseline before preflight/write execution
  - targeted tests added in `tests/test_config_pack_resolution.py`
- Rollback note:
  - revert only config-pack snapshot persistence and explicit preflight stage changes

### Phase 3

- Status: complete
- Test status: passed
- Deliverables:
  - provisioning results now carry `domain_outcomes` for config-pack resolution, OLT L2 apply, management path apply, OMCI WAN apply, TR-069 bind apply, OLT/OMCI readback verify, and ACS bootstrap verify
  - later verify failures now preserve earlier successful apply-domain evidence instead of flattening everything into one undifferentiated failure payload
  - targeted assertions added for the existing TR-069 binding readback failure and recovery paths
- Rollback note:
  - revert only the domain result model and preserve pre-existing provisioning semantics

### Phase 4

- Status: complete
- Test status: passed
- Deliverables:
  - authorization baseline now lands in `pending_acs_registration` instead of `provisioned` while ACS bootstrap verification is still pending
  - TR-069 binding readback misses after a successful bind apply now return `waiting=True` with `pending_verification` domain outcomes instead of hard-failing the provisioning run
  - ACS bootstrap timeout now returns a waiting result and preserves `pending_acs_registration`
  - saved ACS/TR-069 service config now updates provisioning status to `pending_service_config`, `provisioned`, or `failed` based on the actual apply outcome
- Rollback note:
  - revert only the failure classification logic and preserve new persistence scaffolding

### Phase 5

- Status: not started
- Test status: pending
- Rollback note:
  - disable only scheduled verification tasks if background jobs misbehave

### Phase 6

- Status: not started
- Test status: pending
- Rollback note:
  - validation is read-heavy and should be independently reversible

### Phase 7

- Status: not started
- Test status: pending
- Rollback note:
  - keep detection, disable only mutating recovery actions if too aggressive

### Phase 8

- Status: not started
- Test status: pending
- Rollback note:
  - policies should remain independently reversible from core state-model changes

### Phase 9

- Status: not started
- Test status: pending
- Rollback note:
  - model profiles are capability metadata and policy behavior, not core schema

### Phase 10

- Status: not started
- Test status: pending
- Rollback note:
  - keep sweeps and reporting loosely coupled so they can be disabled independently

## Verification Log

- Phase 0: baseline artifacts created; no runtime tests required
- Phase 1:
  - `./.venv/bin/pytest -q tests/test_ont_provisioning_status_transitions.py`
  - `./.venv/bin/pytest -q tests/test_ont_desired_config_direct.py::test_authorization_baseline_updates_provisioning_status tests/test_ont_desired_config_direct.py::test_authorization_baseline_marks_failed_status tests/test_ont_desired_config_direct.py::test_authorization_baseline_blocks_before_olt_write_when_preflight_fails tests/test_ont_desired_config_direct.py::test_authorization_baseline_continues_when_acs_inform_is_warning`
- Phase 2:
  - `./.venv/bin/pytest -q tests/test_config_pack_resolution.py`
  - `./.venv/bin/pytest -q tests/test_ont_desired_config_direct.py::test_authorization_baseline_updates_provisioning_status tests/test_ont_desired_config_direct.py::test_authorization_baseline_marks_failed_status tests/test_ont_desired_config_direct.py::test_authorization_baseline_blocks_before_olt_write_when_preflight_fails tests/test_ont_desired_config_direct.py::test_authorization_baseline_continues_when_acs_inform_is_warning`
- Phase 3:
  - `./.venv/bin/pytest -q tests/test_config_pack_resolution.py tests/test_tr069_binding_readback.py`
  - `./.venv/bin/pytest -q tests/test_ont_desired_config_direct.py::test_authorization_baseline_updates_provisioning_status tests/test_ont_desired_config_direct.py::test_authorization_baseline_marks_failed_status tests/test_ont_desired_config_direct.py::test_authorization_baseline_blocks_before_olt_write_when_preflight_fails tests/test_ont_desired_config_direct.py::test_authorization_baseline_continues_when_acs_inform_is_warning`
- Phase 4:
  - `./.venv/bin/pytest -q tests/test_tr069_binding_readback.py`
  - `./.venv/bin/pytest -q tests/test_ont_desired_config_direct.py::test_authorization_baseline_updates_provisioning_status tests/test_ont_desired_config_direct.py::test_authorization_baseline_marks_failed_status tests/test_ont_desired_config_direct.py::test_authorization_baseline_blocks_before_olt_write_when_preflight_fails tests/test_ont_desired_config_direct.py::test_authorization_baseline_continues_when_acs_inform_is_warning tests/test_ont_desired_config_direct.py::test_apply_saved_service_config_skips_wan_when_wan_mode_absent tests/test_ont_desired_config_direct.py::test_apply_saved_service_config_pushes_dhcp_enable_defensively_when_lan_unset`
  - `./.venv/bin/pytest -q tests/test_config_pack_resolution.py`
