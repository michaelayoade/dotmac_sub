"""Turn-up gaps: WAN service port (G1), CR failure waiting/resume (G3),
RADIUS proof (G9). One focused test per behaviour."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import select

from app.models.catalog import AccessCredential
from app.models.network import OLTDevice, OntAssignment, OntUnit
from app.models.network_operation import (
    NetworkOperationDispatch,
    NetworkOperationStatus,
)
from app.models.ont_service_configuration import OntServiceConfigurationPhase
from app.models.radius_active_session import RadiusActiveSession
from app.services.db_session_adapter import db_session_adapter
from app.services.network import ont_turnup_gaps
from app.services.network.ont_service_configuration import (
    ExecuteOntServiceConfigurationCommand,
    execute_ont_service_configuration,
)
from app.services.network.reconcile import (
    AcsAddObject,
    AcsSetManagementServer,
    AcsSetPppoe,
    OltCreateServicePort,
    OltDeleteServicePort,
    compute_plan,
)
from app.services.network.reconcile.applier import apply_plan
from app.services.owner_commands import CommandContext
from app.services.tr069 import _resume_apply_after_inform
from tests.test_ont_service_configuration import _lifecycle
from tests.test_reconcile_applier import _ctx, _plan, _StubOltAdapter
from tests.test_reconcile_planner import (
    _desired,
    _synced_observed,
)


def _sp(index, vlan=203, gem=1, ont_id=11):
    return {
        "index": index,
        "vlan_id": vlan,
        "ont_id": ont_id,
        "gem_index": gem,
        "fsp": "0/1/3",
        "tag_transform": "translate",
        "state": "up",
    }


def _with_ports(desired, ports):
    observed = _synced_observed(desired)
    return dataclasses.replace(
        observed, olt=dataclasses.replace(observed.olt, olt_service_ports=ports)
    )


# ── G1 ──────────────────────────────────────────────────────────────────────


def _olt_and_ont(db_session):
    olt = OLTDevice(
        name="Turnup OLT",
        vendor="Huawei",
        model="MA5608T",
        ssh_username="admin",
        ssh_password="x",
    )
    db_session.add(olt)
    db_session.flush()
    ont = OntUnit(
        serial_number=f"TURNUP-{uuid.uuid4().hex[:8]}",
        is_active=True,
        olt_device_id=olt.id,
    )
    db_session.add(ont)
    db_session.flush()
    return olt, ont


def test_wan_service_port_index_is_allocated_and_persisted(db_session):
    from app.services.network.service_port_allocator import get_allocations_for_ont

    _olt, ont = _olt_and_ont(db_session)

    index = ont_turnup_gaps.ensure_wan_service_port_index(
        db_session, ont, wan_vlan=203, gem_index=1
    )

    assert index is not None
    assert ont.desired_config["olt"]["wan_service_port_index"] == index
    allocations = get_allocations_for_ont(db_session, ont.id)
    assert [(a.service_type, a.vlan_id, a.gem_index) for a in allocations] == [
        ("internet", 203, 1)
    ]
    # Recorded index is returned unchanged, no second allocation.
    assert (
        ont_turnup_gaps.ensure_wan_service_port_index(
            db_session, ont, wan_vlan=203, gem_index=1
        )
        == index
    )
    assert len(get_allocations_for_ont(db_session, ont.id)) == 1


def test_wan_service_port_allocation_fails_closed_when_ambiguous(db_session):
    from app.services.network.service_port_allocator import allocate_service_port

    olt, ont = _olt_and_ont(db_session)
    for _ in range(2):
        allocate_service_port(
            db_session,
            olt.id,
            ont.id,
            service_type="internet",
            vlan_id=203,
            gem_index=1,
        )

    assert (
        ont_turnup_gaps.ensure_wan_service_port_index(
            db_session, ont, wan_vlan=203, gem_index=1
        )
        is None
    )
    assert "olt" not in (ont.desired_config or {})


def test_missing_internet_service_port_is_planned_with_traffic_tables():
    desired = _desired(wan_traffic_table_inbound=11, wan_traffic_table_outbound=12)
    plan = compute_plan(desired, _with_ports(desired, ()), "sync")

    creates = [a for a in plan.actions if isinstance(a, OltCreateServicePort)]
    wan = [a for a in creates if a.slot == "wan"]
    assert len(wan) == 1
    assert (wan[0].vlan, wan[0].gem_index) == (203, 1)
    assert (wan[0].traffic_table_inbound, wan[0].traffic_table_outbound) == (11, 12)
    assert any(d.field == "olt_service_ports[wan]" for d in plan.drifts)


def test_create_service_port_passes_traffic_tables_to_olt():
    olt = _StubOltAdapter()
    action = OltCreateServicePort(
        fsp="0/1/3",
        ont_id=11,
        service_port_index=7,
        vlan=203,
        gem_index=1,
        slot="wan",
        traffic_table_inbound=11,
        traffic_table_outbound=12,
    )
    assert apply_plan(_plan(action), _ctx(olt_adapter=olt)).success is True
    method, _args, kwargs = olt.calls[0]
    assert method == "create_service_port"
    assert kwargs["traffic_table_inbound"] == 11
    assert kwargs["traffic_table_outbound"] == 12


def test_existing_service_ports_are_never_touched():
    desired = _desired()
    other = _sp(99, vlan=300, gem=3)
    # Matching port exists at a different index: no create, no delete.
    moved = _sp(desired.wan_service_port_index + 50)
    plan = compute_plan(
        desired,
        _with_ports(desired, (other, moved)),
        "sync",
        proposed_fields=frozenset({"wan_mode"}),
    )
    assert not [
        a
        for a in plan.actions
        if isinstance(a, OltCreateServicePort) and a.slot == "wan"
    ]
    assert not [
        a
        for a in plan.actions
        if isinstance(a, OltDeleteServicePort)
        and a.service_port_index in {99, moved["index"]}
    ]

    # Ambiguous (two matching ports): fail closed, unrepairable drift, no create.
    ambiguous = _with_ports(desired, (_sp(60), _sp(61)))
    plan = compute_plan(
        desired, ambiguous, "sync", proposed_fields=frozenset({"wan_mode"})
    )
    assert not [
        a
        for a in plan.actions
        if isinstance(a, OltCreateServicePort) and a.slot == "wan"
    ]
    assert any(
        d.field == "olt_service_ports[wan]" and not d.repairable for d in plan.drifts
    )


# ── G3 ──────────────────────────────────────────────────────────────────────


def test_cr_credential_repair_is_planned_before_other_acs_writes():
    desired = _desired(wan_pppoe_wcd_index=1)
    observed = _synced_observed(desired)
    observed = dataclasses.replace(
        observed,
        acs=dataclasses.replace(
            observed.acs,
            acs_observed_cr_username="wrong",
            acs_observed_pppoe_username="someone-else",
        ),
    )
    plan = compute_plan(desired, observed, "sync")
    acs_actions = [a for a in plan.actions if a.surface == "acs"]
    assert any(isinstance(a, AcsSetPppoe) for a in acs_actions)
    assert isinstance(acs_actions[0], AcsSetManagementServer)


def _cr_waiting_setup(db_session, monkeypatch, *, queued):
    ont = OntUnit(serial_number=f"CRW-{uuid.uuid4().hex[:8]}", is_active=True)
    db_session.add(ont)
    db_session.flush()
    assignment = OntAssignment(ont_unit_id=ont.id, active=True)
    db_session.add(assignment)
    db_session.flush()
    head, revision, operation = _lifecycle(
        db_session,
        ont,
        assignment,
        phase=OntServiceConfigurationPhase.readback_pending,
        suffix="cr-wait",
        desired_change_evidence={"wan.mode": "pppoe"},
    )
    head.waiting_reason = "awaiting_acs_task_drain"
    operation.status = NetworkOperationStatus.waiting
    operation.input_payload = {
        "ont_id": str(ont.id),
        "configuration_head_id": str(head.id),
        "configuration_revision": revision.revision,
    }
    db_session.commit()
    monkeypatch.setattr(
        "app.services.network.reconcile.core.reconcile_ont",
        lambda *_a, **_k: SimpleNamespace(
            success=False,
            sync_status="out_of_sync",
            drift_after=("x",),
            failure=SimpleNamespace(
                reason="acs_cr_failed", message="401", evidence=None
            ),
        ),
    )
    monkeypatch.setattr(
        ont_turnup_gaps, "acs_queued_task_count", lambda *_a, **_k: queued
    )
    ids = (ont.id, operation.id, head.id, revision.revision)
    db_session_adapter.release_read_transaction(db_session)
    return head, operation, ids


def _execute(db_session, ids, attempt=3):
    ont_id, operation_id, head_id, rev = ids
    return execute_ont_service_configuration(
        db_session,
        ExecuteOntServiceConfigurationCommand(
            context=CommandContext.system(
                actor="test:worker",
                scope="network:ont:execute",
                reason="t",
                command_id=uuid.uuid4(),
                correlation_id=operation_id,
                idempotency_key=f"turnup-{uuid.uuid4().hex}",
            ),
            ont_unit_id=ont_id,
            operation_id=operation_id,
            configuration_head_id=head_id,
            revision=rev,
            verification_attempt=attempt,
        ),
    )


def test_cr_failure_with_queued_task_waits_instead_of_failing(db_session, monkeypatch):
    head, operation, ids = _cr_waiting_setup(db_session, monkeypatch, queued=2)
    outcome = _execute(db_session, ids)
    assert outcome.phase is OntServiceConfigurationPhase.readback_pending
    assert head.waiting_reason == ont_turnup_gaps.WAITING_INFORM_CR_FAILED
    assert head.failure_code is None
    assert operation.status is NetworkOperationStatus.waiting


def test_cr_failure_without_queued_task_is_still_terminal(db_session, monkeypatch):
    head, operation, ids = _cr_waiting_setup(db_session, monkeypatch, queued=0)
    outcome = _execute(db_session, ids)
    assert outcome.phase is OntServiceConfigurationPhase.failed
    assert head.failure_code == "acs_cr_failed"


def _park(db_session, monkeypatch, *, queued):
    head, operation, ids = _cr_waiting_setup(db_session, monkeypatch, queued=2)
    _execute(db_session, ids)  # -> awaiting_inform_cr_failed
    ont = db_session.get(OntUnit, ids[0])
    operation.updated_at = datetime.now(UTC) - timedelta(minutes=5)
    ont.acs_last_inform_at = datetime.now(UTC)
    db_session.commit()
    monkeypatch.setattr(
        ont_turnup_gaps, "acs_queued_task_count", lambda *_a, **_k: queued
    )
    return head, operation, ids


def _dispatches(db_session, operation_id):
    return list(
        db_session.scalars(
            select(NetworkOperationDispatch).where(
                NetworkOperationDispatch.operation_id == operation_id,
                NetworkOperationDispatch.dispatch_key.like("resume:%"),
            )
        )
    )


def test_waiting_cr_head_resumes_apply_on_next_inform(db_session, monkeypatch):
    head, operation, ids = _park(db_session, monkeypatch, queued=0)
    assert (
        _resume_apply_after_inform(
            db_session, ont_id=ids[0], inform_at=datetime.now(UTC)
        )
        is True
    )
    rows = _dispatches(db_session, ids[1])
    assert len(rows) == 1
    assert rows[0].command_name == "ont_service_config_apply.v1"
    assert operation.input_payload["explicit_repair"] is True  # a write pass
    assert head.phase is OntServiceConfigurationPhase.queued


def test_resume_never_queues_duplicates(db_session, monkeypatch):
    head, operation, ids = _park(db_session, monkeypatch, queued=3)
    # ACS still holds tasks: nothing staged.
    assert (
        _resume_apply_after_inform(
            db_session, ont_id=ids[0], inform_at=datetime.now(UTC)
        )
        is False
    )
    assert _dispatches(db_session, ids[1]) == []
    # Queue drains: one resume; a second Inform stages nothing more.
    monkeypatch.setattr(ont_turnup_gaps, "acs_queued_task_count", lambda *_a, **_k: 0)
    now = datetime.now(UTC)
    assert _resume_apply_after_inform(db_session, ont_id=ids[0], inform_at=now)
    assert not _resume_apply_after_inform(
        db_session, ont_id=ids[0], inform_at=now + timedelta(seconds=30)
    )
    assert len(_dispatches(db_session, ids[1])) == 1


def test_pending_wcd_create_runs_next_apply_step_after_inform(db_session, monkeypatch):
    # Head waiting on a queued WCD addObject (pending marker), no CR failure.
    head, operation, ids = _park(db_session, monkeypatch, queued=0)
    head.waiting_reason = "awaiting_fresh_device_readback"
    ont = db_session.get(OntUnit, ids[0])
    ont.tr069_last_snapshot = {
        "capabilities": {"pending_wcd_add": datetime.now(UTC).isoformat()}
    }
    db_session.commit()
    assert (
        _resume_apply_after_inform(
            db_session, ont_id=ids[0], inform_at=datetime.now(UTC)
        )
        is True
    )
    assert len(_dispatches(db_session, ids[1])) == 1

    # And the write pass that follows plans the PPP child plus its params,
    # adopting the WCD that the queued addObject created (no new addObject
    # for the WCD itself).
    desired = _desired(wan_pppoe_wcd_index=None)
    base = _synced_observed(desired)
    observed = dataclasses.replace(
        base,
        acs=dataclasses.replace(
            base.acs,
            acs_observed_pppoe_username=None,
            acs_observed_wan_ppp_locations=(),
        ),
    )
    plan = compute_plan(desired, observed, "sweep")
    kinds = [type(a) for a in plan.actions]
    assert AcsSetPppoe in kinds
    assert any(isinstance(a, AcsAddObject) for a in plan.actions)


def test_failed_mismatch_with_pending_create_is_retried_as_write_pass(
    db_session, monkeypatch
):
    head, operation, ids = _park(db_session, monkeypatch, queued=0)
    ont = db_session.get(OntUnit, ids[0])
    ont.tr069_last_snapshot = {
        "capabilities": {"pending_wcd_add": datetime.now(UTC).isoformat()}
    }
    head.phase = OntServiceConfigurationPhase.failed
    head.failure_code = "verification_mismatch"
    operation.status = NetworkOperationStatus.failed
    operation.completed_at = datetime.now(UTC) - timedelta(minutes=5)
    db_session.commit()
    assert (
        _resume_apply_after_inform(
            db_session, ont_id=ids[0], inform_at=datetime.now(UTC)
        )
        is True
    )
    db_session.refresh(head)
    assert head.phase is OntServiceConfigurationPhase.queued
    assert head.latest_operation_id != ids[1]  # a fresh explicit-repair operation


# ── B1: resume loop / storm guards ─────────────────────────────────────────


def test_pending_wcd_marker_is_cleared_when_wcd_is_adopted():
    from app.services.network.reconcile import AcsAddObject

    state = {"pending_wcd_add": datetime.now(UTC).isoformat()}
    wcd_path = "InternetGatewayDevice.WANDevice.1.WANConnectionDevice."

    class Acs:
        def list_devices(self, query=None, projection=None):
            blank = {
                "WANIPConnectionNumberOfEntries": {"_value": 0},
                "WANPPPConnectionNumberOfEntries": {"_value": 0},
            }
            return [
                {
                    "InternetGatewayDevice": {
                        "WANDevice": {"1": {"WANConnectionDevice": {"2": blank}}}
                    }
                }
            ]

        def refresh_object(self, *a, **k):
            return {}

        def extract_parameter_value(self, device, path):
            return None

        def add_object(self, *a, **k):
            raise AssertionError("must adopt the existing WCD, not add another")

    from app.services.network.reconcile.applier import SELECT_OR_CREATE_WCD

    ctx = _ctx(acs_client=Acs())
    ctx.state_get = state.get
    ctx.state_set = state.__setitem__
    action = AcsAddObject(
        device_id="dev", object_path=wcd_path, wcd_index=SELECT_OR_CREATE_WCD
    )
    assert apply_plan(_plan(action), ctx).success is True
    assert not state["pending_wcd_add"]


def test_pending_create_marker_honours_ttl():
    fresh = SimpleNamespace(
        tr069_last_snapshot={
            "capabilities": {"pending_wcd_add": datetime.now(UTC).isoformat()}
        }
    )
    old = SimpleNamespace(
        tr069_last_snapshot={
            "capabilities": {
                "pending_wcd_add": (
                    datetime.now(UTC) - timedelta(minutes=11)
                ).isoformat()
            }
        }
    )
    assert ont_turnup_gaps.has_pending_create_marker(fresh) is True
    assert ont_turnup_gaps.has_pending_create_marker(old) is False


def test_resume_is_capped_per_revision_then_ends_as_acs_cr_failed(
    db_session, monkeypatch
):
    head, operation, ids = _park(db_session, monkeypatch, queued=0)
    monkeypatch.setattr(ont_turnup_gaps, "acs_queued_task_count", lambda *_a, **_k: 0)
    for n in range(ont_turnup_gaps.MAX_RESUMES_PER_REVISION):
        now = datetime.now(UTC) + timedelta(minutes=n)
        assert _resume_apply_after_inform(db_session, ont_id=ids[0], inform_at=now)
        # The resumed apply again parks on a CR failure (3 prior attempts).
        db_session.refresh(operation)
        head.phase = OntServiceConfigurationPhase.readback_pending
        head.waiting_reason = ont_turnup_gaps.WAITING_INFORM_CR_FAILED
        operation.status = NetworkOperationStatus.waiting
        operation.updated_at = now
        ont = db_session.get(OntUnit, ids[0])
        ont.acs_last_inform_at = now + timedelta(seconds=30)
        db_session.commit()
    assert operation.input_payload["resume_count"] == 3
    later = datetime.now(UTC) + timedelta(hours=1)
    ont.acs_last_inform_at = later
    db_session.commit()
    assert (
        _resume_apply_after_inform(db_session, ont_id=ids[0], inform_at=later) is False
    )
    db_session.refresh(head)
    assert head.phase is OntServiceConfigurationPhase.failed
    assert head.failure_code == "acs_cr_failed"


def test_non_wan_failed_head_is_not_resumed(db_session, monkeypatch):
    head, operation, ids = _park(db_session, monkeypatch, queued=0)
    from app.models.ont_service_configuration import OntServiceConfigurationRevision

    rev = db_session.scalar(
        select(OntServiceConfigurationRevision).where(
            OntServiceConfigurationRevision.head_id == head.id
        )
    )
    rev.section = "lan"
    ont = db_session.get(OntUnit, ids[0])
    ont.tr069_last_snapshot = {
        "capabilities": {"pending_wcd_add": datetime.now(UTC).isoformat()}
    }
    head.phase = OntServiceConfigurationPhase.failed
    head.failure_code = "verification_mismatch"
    operation.status = NetworkOperationStatus.failed
    operation.completed_at = datetime.now(UTC) - timedelta(minutes=5)
    db_session.commit()
    assert not _resume_apply_after_inform(
        db_session, ont_id=ids[0], inform_at=datetime.now(UTC)
    )
    db_session.refresh(head)
    assert head.phase is OntServiceConfigurationPhase.failed


# ── B2: inform hot path ─────────────────────────────────────────────────────


def test_inform_hook_enqueues_delayed_task_and_never_calls_acs(db_session, monkeypatch):
    from app.services.tr069 import _queue_resume_apply_after_inform

    head, operation, ids = _park(db_session, monkeypatch, queued=0)

    def boom(*_a, **_k):
        raise AssertionError("NBI must not be called on the inform hot path")

    monkeypatch.setattr(ont_turnup_gaps, "acs_queued_task_count", boom)
    sent = []
    monkeypatch.setattr(
        "app.services.queue_adapter.enqueue_task",
        lambda name, **kw: sent.append((name, kw)) or SimpleNamespace(queued=True),
    )
    assert _queue_resume_apply_after_inform(
        db_session, ont_id=ids[0], inform_at=datetime.now(UTC)
    )
    assert sent[0][0] == "app.tasks.tr069.resume_ont_service_apply_after_inform"
    assert sent[0][1]["countdown"] == 60


def test_wan_only_pass_repairs_cr_settings_but_never_acs_endpoint():
    desired = _desired(
        acs_url="https://acs.example.invalid", acs_username="u", acs_password_ref="r"
    )
    base = _synced_observed(desired)
    observed = dataclasses.replace(
        base,
        acs=dataclasses.replace(
            base.acs, acs_observed_cr_username="wrong", acs_observed_url="other"
        ),
    )
    plan = compute_plan(
        desired, observed, "sync", proposed_fields=frozenset({"wan_mode"})
    )
    mgmt = [a for a in plan.actions if isinstance(a, AcsSetManagementServer)]
    assert len(mgmt) == 1
    assert mgmt[0].acs_url is None and mgmt[0].acs_password_ref is None


# ── G9 / B3 ─────────────────────────────────────────────────────────────────


def _radius_exec(
    db_session,
    monkeypatch,
    subscriber,
    subscription,
    *,
    session_age_min=None,
    attempt=0,
    first_clean_min=None,
    onu_login="100000001",
    applied_min=10,
):
    ont = OntUnit(serial_number=f"RAD-{uuid.uuid4().hex[:8]}", is_active=True)
    db_session.add(ont)
    db_session.flush()
    assignment = OntAssignment(
        ont_unit_id=ont.id,
        subscriber_id=subscriber.id,
        subscription_id=subscription.id,
        active=True,
    )
    db_session.add(assignment)
    db_session.flush()
    cred = AccessCredential(
        subscriber_id=subscriber.id,
        subscription_id=subscription.id,
        username="100000001",
        is_active=True,
    )
    db_session.add(cred)
    db_session.flush()
    head, revision, operation = _lifecycle(
        db_session,
        ont,
        assignment,
        phase=OntServiceConfigurationPhase.queued,
        suffix="radius",
        desired_change_evidence={"wan.mode": "pppoe"},
    )
    now = datetime.now(UTC)
    payload = {
        "ont_id": str(ont.id),
        "configuration_head_id": str(head.id),
        "configuration_revision": revision.revision,
        "applied_at": (now - timedelta(minutes=applied_min)).isoformat(),
    }
    if first_clean_min is not None:
        payload["first_clean_readback_at"] = (
            now - timedelta(minutes=first_clean_min)
        ).isoformat()
    operation.input_payload = payload
    if session_age_min is not None:
        db_session.add(
            RadiusActiveSession(
                access_credential_id=cred.id,
                subscription_id=subscription.id,
                username="100000001",
                acct_session_id=uuid.uuid4().hex,
                session_start=now - timedelta(minutes=session_age_min),
            )
        )
    db_session.commit()
    monkeypatch.setattr(
        "app.services.network.reconcile.core.reconcile_ont",
        lambda *_a, **_k: SimpleNamespace(
            success=True,
            sync_status="synced",
            drift_after=(),
            failure=None,
            actions_applied=(),
        ),
    )
    monkeypatch.setattr(
        "app.services.network.reconcile.adapters.desired_from_ont_unit",
        lambda *_a, **_k: SimpleNamespace(
            wan_mode="pppoe",
            wan_pppoe_username=onu_login,
            wan_service_port_index=7,
            wan_vlan=203,
            wan_gem_index=1,
        ),
    )
    ids = (ont.id, operation.id, head.id, revision.revision)
    db_session_adapter.release_read_transaction(db_session)
    return head, operation, _execute(db_session, ids, attempt=attempt)


def test_wan_verified_only_with_live_radius_session(
    db_session, monkeypatch, subscriber, subscription
):
    # attempt>0 = readback-only pass, so the seeded applied_at stays in force.
    head, _op, outcome = _radius_exec(
        db_session, monkeypatch, subscriber, subscription, session_age_min=1, attempt=1
    )
    assert outcome.phase is OntServiceConfigurationPhase.verified


def test_radius_session_older_than_the_apply_cannot_verify(
    db_session, monkeypatch, subscriber, subscription
):
    head, op, outcome = _radius_exec(
        db_session, monkeypatch, subscriber, subscription, session_age_min=60, attempt=1
    )
    assert outcome.phase is OntServiceConfigurationPhase.readback_pending
    assert head.waiting_reason == "awaiting_radius_session"
    staged = db_session.scalar(
        select(NetworkOperationDispatch).where(
            NetworkOperationDispatch.operation_id == op.id,
            NetworkOperationDispatch.dispatch_key == "verify:2",
        )
    )
    assert staged.command_name == "ont_service_config_verify.v1"


def test_onu_username_not_canonical_login_holds_with_explicit_code(
    db_session, monkeypatch, subscriber, subscription
):
    head, _op, outcome = _radius_exec(
        db_session,
        monkeypatch,
        subscriber,
        subscription,
        session_age_min=1,
        attempt=1,
        onu_login="someone-else",
    )
    assert outcome.phase is OntServiceConfigurationPhase.delivered_unverified
    assert head.failure_code == "pppoe_login_mismatch"


def test_radius_wait_is_anchored_on_first_clean_readback(
    db_session, monkeypatch, subscriber, subscription
):
    # The operation is old, but the first clean readback was only 1 minute ago.
    head, _op, outcome = _radius_exec(
        db_session, monkeypatch, subscriber, subscription, attempt=1, first_clean_min=1
    )
    assert outcome.phase is OntServiceConfigurationPhase.readback_pending


def test_radius_wait_times_out_after_first_clean_readback_window(
    db_session, monkeypatch, subscriber, subscription
):
    head, _op, outcome = _radius_exec(
        db_session, monkeypatch, subscriber, subscription, attempt=1, first_clean_min=30
    )
    assert outcome.phase is OntServiceConfigurationPhase.delivered_unverified
    assert head.failure_code == "radius_session_not_observed"


def test_wan_allocation_marked_provisioned_only_after_applied_create_and_readback(
    db_session,
):
    from app.services.network.service_port_allocator import get_allocations_for_ont

    _olt, ont = _olt_and_ont(db_session)
    ont_turnup_gaps.ensure_wan_service_port_index(
        db_session, ont, wan_vlan=203, gem_index=1
    )
    applied = (SimpleNamespace(field="service_port[wan]"),)

    def result(**kw):
        base = dict(
            success=True, sync_status="synced", drift_after=(), actions_applied=applied
        )
        return SimpleNamespace(**{**base, **kw})

    ont_turnup_gaps.mark_wan_service_port_provisioned(
        db_session, ont, result(drift_after=("olt_service_ports[wan]",))
    )
    assert get_allocations_for_ont(db_session, ont.id)[0].provisioned_at is None
    ont_turnup_gaps.mark_wan_service_port_provisioned(db_session, ont, result())
    assert get_allocations_for_ont(db_session, ont.id)[0].provisioned_at is not None
