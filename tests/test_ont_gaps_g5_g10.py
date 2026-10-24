"""Focused tests for ONT gap fixes G5-G10 (verify freshness, mgmt IPHOST,
Wi-Fi security floor, silent-ONT recovery, stale OLT readings)."""

from __future__ import annotations

import dataclasses
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from app.models.network import OLTDevice, OntSyncStatus, OntUnit
from app.models.network_operation import (
    NetworkOperation,
    NetworkOperationStatus,
    NetworkOperationTargetType,
    NetworkOperationType,
)
from app.models.ont_observation import OntObservation
from app.models.ont_service_configuration import OntServiceConfigurationRevision
from app.services.genieacs_client import GenieACSError
from app.services.network import ont_service_configuration as lifecycle
from app.services.network.olt_batched_mgmt import (
    BatchedMgmtSpec,
    build_management_command_batch,
)
from app.services.network.reconcile import (
    AcsSetWifiConfig,
    OltIpconfig,
    OltTr069ServerConfig,
    compute_plan,
)
from app.services.network.reconcile.adapters import upsert_ont_observation
from app.services.network.reconcile.applier import ApplyError
from app.services.network.reconcile.observation_refresh import (
    ObservationRefreshPolicy,
    olt_has_active_operation,
    olt_observation_age,
    stale_observation_ont_ids,
)
from app.services.network.reconcile.readers import read_acs_state
from app.tasks.ont_reconcile import refresh_olt_observations
from tests.test_reconcile_planner import (
    _desired,
    _observed,
    _synced_observed,
    _types,
)
from tests.test_reconcile_readers import _device_doc, _StubGenieAcsClient

DEVICE_ID = "00259E-HG8546M-HWTC8535819A"

# ── G5: verify freshness ────────────────────────────────────────────────────


class _RefreshClient(_StubGenieAcsClient):
    def __init__(self, *, devices, refresh_result=None, refresh_raises=None):
        super().__init__(devices=devices)
        self.events: list[tuple] = []
        self._refresh_result = refresh_result if refresh_result is not None else {}
        self._refresh_raises = refresh_raises

    def list_devices(self, query=None, projection=None):
        self.events.append(("list", None))
        return super().list_devices(query, projection)

    def refresh_object(self, device_id, object_path, *, allow_when_pending=False):
        self.events.append(("refresh", object_path))
        if self._refresh_raises is not None:
            raise self._refresh_raises
        return self._refresh_result


def test_g5_refresh_runs_and_the_device_is_re_read_afterwards():
    client = _RefreshClient(devices=[_device_doc()])
    result = read_acs_state(
        client,
        _desired(acs_device_id=DEVICE_ID),
        refresh_paths=("InternetGatewayDevice.WANDevice.1.",),
    )
    assert result.success is True
    assert client.events == [
        ("list", None),
        ("refresh", "InternetGatewayDevice.WANDevice.1."),
        ("list", None),
    ]


def test_g5_no_refresh_without_paths():
    client = _RefreshClient(devices=[_device_doc()])
    read_acs_state(client, _desired(acs_device_id=DEVICE_ID))
    assert all(kind != "refresh" for kind, _ in client.events)


def test_g5_refresh_uses_the_matched_document_id_when_none_is_recorded():
    client = _RefreshClient(devices=[_device_doc()])
    result = read_acs_state(
        client,
        _desired(acs_device_id=None),
        refresh_paths=("InternetGatewayDevice.WANDevice.1.",),
    )
    assert result.success is True
    assert ("refresh", "InternetGatewayDevice.WANDevice.1.") in client.events


@pytest.mark.parametrize(
    "client",
    [
        _RefreshClient(devices=[_device_doc()], refresh_raises=GenieACSError("boom")),
        _RefreshClient(
            devices=[_device_doc()],
            refresh_result={"connectionRequestError": "401 Unauthorized"},
        ),
    ],
)
def test_g5_undelivered_refresh_fails_closed_without_using_stale_copy(client):
    result = read_acs_state(
        client,
        _desired(acs_device_id=DEVICE_ID),
        refresh_paths=("InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.",),
    )
    assert result.success is False
    assert result.unreachable is True
    assert result.observed is None
    assert [kind for kind, _ in client.events] == ["list", "refresh"]


def test_g5_absent_device_is_not_refreshed():
    client = _RefreshClient(devices=[])
    result = read_acs_state(
        client,
        _desired(acs_device_id=DEVICE_ID),
        refresh_paths=("InternetGatewayDevice.WANDevice.1.",),
    )
    assert result.success is True
    assert all(kind != "refresh" for kind, _ in client.events)


def _revision(section: str) -> SimpleNamespace:
    return SimpleNamespace(section=section, desired_change_evidence={})


def test_g5_only_the_revision_subtree_is_refreshed_and_only_on_readback():
    assert lifecycle._readback_refresh_paths(_revision("wan"), readback_only=True) == (
        "InternetGatewayDevice.WANDevice.1.",
    )
    assert lifecycle._readback_refresh_paths(_revision("wifi"), readback_only=True) == (
        "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.",
    )
    assert lifecycle._readback_refresh_paths(_revision("lan"), readback_only=True) == ()
    assert (
        lifecycle._readback_refresh_paths(_revision("wan"), readback_only=False) == ()
    )


def test_g5_drift_field_names_never_carry_values():
    drift = SimpleNamespace(
        surface="acs", field="wifi_security_mode", desired="secret", observed="x"
    )
    names = lifecycle._result_drift_fields(SimpleNamespace(drift_after=(drift,)))
    assert names == ["acs:wifi_security_mode"]
    assert "secret" not in "".join(names)


def test_g5_revision_has_drift_fields_column_and_migration_chain():
    assert "drift_fields" in OntServiceConfigurationRevision.__table__.columns
    text = Path("alembic/versions/700_ont_service_config_drift_fields.py").read_text()
    assert (
        'down_revision: str | None = "699_dispatch_reconciliation_resolution"' in text
    )


def test_g5_configure_template_shows_drift_fields():
    html = Path("templates/admin/network/onts/_configure_form.html").read_text()
    assert "lifecycle.drift_fields" in html


# ── G6: management IPHOST ───────────────────────────────────────────────────


def test_g6_missing_management_iphost_on_present_ont_is_drift():
    desired = _desired()
    observed = _synced_observed(desired)
    observed = dataclasses.replace(
        observed,
        olt=dataclasses.replace(observed.olt, olt_mgmt_ip=None, olt_mgmt_vlan=None),
    )
    plan = compute_plan(desired, observed, "sweep")
    assert OltIpconfig in _types(plan)
    assert any(d.field == "olt_mgmt_ip" for d in plan.drifts)


def test_g6_no_iphost_drift_without_desired_management_config():
    desired = _desired(mgmt_ip=None)
    observed = _synced_observed(desired)
    observed = dataclasses.replace(
        observed, olt=dataclasses.replace(observed.olt, olt_mgmt_ip=None)
    )
    assert OltIpconfig not in _types(compute_plan(desired, observed, "sweep"))


def test_g6_management_service_port_index_is_explicit_when_known():
    spec = BatchedMgmtSpec(
        fsp="0/1/3",
        ont_id_on_olt=11,
        mgmt_vlan_tag=201,
        mgmt_gem_index=2,
        mgmt_service_port_index=23,
    )
    command = {d: c for c, d in build_management_command_batch(spec)}[
        "create_mgmt_service_port"
    ]
    assert command.startswith("service-port 23 vlan 201 ")


def test_g6_service_port_command_unchanged_without_index():
    spec = BatchedMgmtSpec(
        fsp="0/1/3", ont_id_on_olt=11, mgmt_vlan_tag=201, mgmt_gem_index=2
    )
    command = {d: c for c, d in build_management_command_batch(spec)}[
        "create_mgmt_service_port"
    ]
    assert command.startswith("service-port vlan 201 ")


def test_g6_commissioning_plan_carries_the_index_to_the_adapter_spec():
    from app.services.network.ont_commissioning import _CommissioningManagementPlan

    plan = _CommissioningManagementPlan(
        fsp=SimpleNamespace(value="0/1/3"),  # type: ignore[arg-type]
        ont_id_on_olt=11,
        mgmt_vlan_tag=201,
        mgmt_gem_index=2,
        ip_mode="dhcp",
        ip_address=None,
        subnet_mask=None,
        gateway=None,
        ip_priority=0,
        tr069_profile_id=2,
        mgmt_service_port_index=31,
    )
    assert plan.to_adapter_spec().mgmt_service_port_index == 31


# ── G7: Wi-Fi security floor ────────────────────────────────────────────────


def _wifi_observed(desired, *, security, root="InternetGatewayDevice"):
    observed = _synced_observed(desired)
    return dataclasses.replace(
        observed,
        acs=dataclasses.replace(
            observed.acs,
            acs_data_model_root=root,
            acs_observed_wifi_security_mode=security,
        ),
    )


@pytest.mark.parametrize("configured", [None, "", "default", "Default", "none", "open"])
def test_g7_default_unset_or_open_enforces_wpa2_floor_when_weaker(configured):
    desired = _desired(
        wifi_security_mode=configured, tr069_data_model_root="InternetGatewayDevice"
    )
    plan = compute_plan(desired, _wifi_observed(desired, security="None"), "sweep")
    actions = [a for a in plan.actions if isinstance(a, AcsSetWifiConfig)]
    assert len(actions) == 1
    assert actions[0].security_mode == "11i"


def test_g7_floor_never_rewrites_a_network_already_at_or_above_it():
    desired = _desired(
        wifi_security_mode="default", tr069_data_model_root="InternetGatewayDevice"
    )
    plan = compute_plan(desired, _wifi_observed(desired, security="11i"), "sweep")
    assert AcsSetWifiConfig not in _types(plan)


def test_g7_unread_security_is_not_guessed_as_drift():
    desired = _desired(
        wifi_security_mode=None, tr069_data_model_root="InternetGatewayDevice"
    )
    plan = compute_plan(desired, _wifi_observed(desired, security=None), "sweep")
    assert AcsSetWifiConfig not in _types(plan)


def test_g7_tr181_floor_is_wpa2_personal():
    desired = _desired(wifi_security_mode="default", tr069_data_model_root="Device")
    plan = compute_plan(
        desired, _wifi_observed(desired, security="None", root="Device"), "sweep"
    )
    action = next(a for a in plan.actions if isinstance(a, AcsSetWifiConfig))
    assert action.security_mode == "WPA2-Personal"


def test_g7_explicit_strong_mode_is_still_honoured():
    desired = _desired(
        wifi_security_mode="WPA2-Personal",
        tr069_data_model_root="InternetGatewayDevice",
    )
    plan = compute_plan(desired, _wifi_observed(desired, security="WPA"), "sweep")
    action = next(a for a in plan.actions if isinstance(a, AcsSetWifiConfig))
    assert action.security_mode == "11i"


def test_g7_applier_refuses_an_open_security_write():
    from app.services.network.reconcile import AcsSetWifiConfig as Action
    from app.services.network.reconcile.applier import ApplyContext, _execute
    from app.services.network.reconcile.state import Tr069WifiParameterPaths

    paths = Tr069WifiParameterPaths(
        enabled="InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.Enable",
        ssid="InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.SSID",
        psk_path="InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.PreSharedKey.1.KeyPassphrase",
        channel="InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.Channel",
        security_mode="InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.BeaconType",
    )
    client = MagicMock()
    ctx = ApplyContext(
        olt_adapter=MagicMock(),
        acs_client=client,
        resolve_secret=lambda ref: ref,
    )
    action = Action(device_id=DEVICE_ID, paths=paths, security_mode="None")
    with pytest.raises(ApplyError):
        _execute(action, ctx)
    client.set_parameter_values.assert_not_called()


def test_g7_wpa2_floor_write_pins_aes_and_psk():
    from app.services.network.reconcile.applier import ApplyContext, _execute
    from app.services.network.reconcile.state import Tr069WifiParameterPaths

    wlan = "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1"
    paths = Tr069WifiParameterPaths(
        enabled=f"{wlan}.Enable",
        ssid=f"{wlan}.SSID",
        psk_path=f"{wlan}.PreSharedKey.1.KeyPassphrase",
        channel=f"{wlan}.Channel",
        security_mode=f"{wlan}.BeaconType",
    )
    client = MagicMock()
    client.set_parameter_values.return_value = {}
    ctx = ApplyContext(
        olt_adapter=MagicMock(), acs_client=client, resolve_secret=lambda ref: ref
    )
    _execute(
        AcsSetWifiConfig(device_id=DEVICE_ID, paths=paths, security_mode="11i"), ctx
    )
    params = client.set_parameter_values.call_args.args[1]
    assert params[f"{wlan}.BeaconType"] == "11i"
    assert params[f"{wlan}.IEEE11iEncryptionModes"] == "AESEncryption"
    assert params[f"{wlan}.IEEE11iAuthenticationMode"] == "PSKAuthentication"


# ── G8: silent ONT ──────────────────────────────────────────────────────────


def test_g8_rebind_is_a_management_change_that_alters_no_desired_config():
    change = lifecycle.ManagementConfigurationChange(
        ip_mode=None, ip_address=None, remote_access=False, rebind_tr069=True
    )
    updates, evidence = lifecycle._change_updates(
        MagicMock(),
        MagicMock(),
        uuid.uuid4(),
        lifecycle.OntConfigurationSection.management,
        change,
    )
    assert updates == {}
    assert evidence == {"management.rebind_tr069": True}


def test_g8_rebind_only_requested_for_a_management_write_pass():
    rev = SimpleNamespace(
        section="management", desired_change_evidence={"management.rebind_tr069": True}
    )
    assert lifecycle._tr069_rebind_requested(rev, readback_only=False) is True
    assert lifecycle._tr069_rebind_requested(rev, readback_only=True) is False
    other = SimpleNamespace(section="wifi", desired_change_evidence={})
    assert lifecycle._tr069_rebind_requested(other, readback_only=False) is False


def test_g8_planner_forces_the_bind_only_when_asked():
    desired = _desired()
    observed = _synced_observed(desired)
    observed = dataclasses.replace(
        observed,
        olt=dataclasses.replace(
            observed.olt, olt_tr069_profile_id=desired.tr069_profile_id
        ),
    )
    fields = frozenset({"tr069_profile_id"})
    unforced = compute_plan(desired, observed, "sync", proposed_fields=fields)
    forced = compute_plan(
        desired, observed, "sync", proposed_fields=fields, forced_fields=fields
    )
    verify = compute_plan(
        desired,
        observed,
        "sync",
        proposed_fields=fields,
        forced_fields=fields,
        force_proposed_writes=False,
    )
    assert OltTr069ServerConfig not in _types(unforced)
    assert OltTr069ServerConfig in _types(forced)
    assert OltTr069ServerConfig not in _types(verify)


def test_g8_ui_exposes_rebind_through_the_lifecycle_route():
    html = Path("templates/admin/network/onts/_configure_form.html").read_text()
    assert "Re-bind TR-069 (OLT)" in html
    assert "/configure/rebind-tr069" in html
    route = Path("app/web/admin/network_onts.py").read_text()
    assert "ManagementConfigurationChange(" in route and "rebind_tr069=True" in route
    assert not re.search(r"bind_tr069_server_profile", route)


def _reboot_request():
    return SimpleNamespace(state=SimpleNamespace(auth={}), headers={})


def test_g8_auto_reboot_does_not_omci_reboot_when_acs_reboot_is_queued(monkeypatch):
    from app.web.admin import network_onts_actions as actions

    monkeypatch.setattr(actions, "_ensure_ont_write_scope", lambda *a, **k: None)
    monkeypatch.setattr(actions, "_acs_reboot_already_queued", lambda db, ont_id: True)
    svc = actions.web_network_ont_actions_service
    monkeypatch.setattr(
        svc,
        "execute_reboot",
        lambda *a, **k: SimpleNamespace(
            success=False, waiting=False, message="CR failed"
        ),
    )
    omci = MagicMock(return_value=(True, "sent"))
    monkeypatch.setattr(svc, "execute_omci_reboot", omci)

    response = actions.ont_reboot(
        _reboot_request(), "ont-1", source="auto", db=MagicMock()
    )

    omci.assert_not_called()
    assert response.status_code == 200
    assert b"queued in ACS" in response.body


def test_g8_auto_reboot_still_falls_back_when_nothing_is_queued(monkeypatch):
    from app.web.admin import network_onts_actions as actions

    monkeypatch.setattr(actions, "_ensure_ont_write_scope", lambda *a, **k: None)
    monkeypatch.setattr(actions, "_acs_reboot_already_queued", lambda db, ont_id: False)
    svc = actions.web_network_ont_actions_service
    monkeypatch.setattr(
        svc,
        "execute_reboot",
        lambda *a, **k: SimpleNamespace(
            success=False, waiting=False, message="CR failed"
        ),
    )
    omci = MagicMock(return_value=(True, "sent"))
    monkeypatch.setattr(svc, "execute_omci_reboot", omci)

    actions.ont_reboot(_reboot_request(), "ont-1", source="auto", db=MagicMock())
    omci.assert_called_once()


def test_g8_pending_reboot_lookup(monkeypatch):
    from app.services.network import ont_turnup_gaps as gaps

    monkeypatch.setattr(
        gaps,
        "_linked_cpe",
        lambda db, ont_id: SimpleNamespace(
            genieacs_device_id=DEVICE_ID, acs_server_id=uuid.uuid4()
        ),
    )
    db = MagicMock()
    db.get.return_value = SimpleNamespace(base_url="http://acs.invalid")
    client = MagicMock()
    monkeypatch.setattr(
        "app.services.genieacs_client.create_genieacs_client", lambda url: client
    )
    ont = SimpleNamespace(id=uuid.uuid4())
    client.get_pending_tasks.return_value = [{"name": "reboot"}]
    assert gaps.acs_pending_reboot_task(db, ont) is True
    client.get_pending_tasks.return_value = [{"name": "refreshObject"}]
    assert gaps.acs_pending_reboot_task(db, ont) is False
    client.get_pending_tasks.side_effect = RuntimeError("down")
    assert gaps.acs_pending_reboot_task(db, ont) is None


# ── G10: stale OLT observations ─────────────────────────────────────────────


def _factory(db_session):
    from contextlib import contextmanager

    @contextmanager
    def _session():
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    return _session


@pytest.fixture
def olt(db_session):
    row = OLTDevice(
        name="OLT-G10", mgmt_ip="192.0.2.30", vendor="huawei", is_active=True
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    db_session.commit()
    return SimpleNamespace(id=row.id)


def _make_ont(db_session, olt, index, *, observed_age_hours):
    from app.services.network.ont_reconcile_eligibility import (
        AdmissionSpec,
        admit_reconcile_cohort_member,
    )
    from app.services.owner_commands import CommandContext

    ont = OntUnit(
        serial_number=f"HWTC0000G10{index}",
        olt_device_id=olt.id,
        board="0/1",
        port=str(index + 1),
        external_id=str(index),
        is_active=True,
        sync_status=OntSyncStatus.synced,
    )
    db_session.add(ont)
    db_session.commit()
    ont_id = ont.id
    observed = _observed()
    upsert_ont_observation(
        db_session,
        ont_id,
        observed,
        observed_surfaces=frozenset({"olt", "acs"}),
        olt_read_status="present",
    )
    row = db_session.scalars(
        select(OntObservation).where(OntObservation.ont_unit_id == ont_id)
    ).one()
    row.olt_observed_at = datetime.now(UTC) - timedelta(hours=observed_age_hours)
    db_session.commit()
    explanation = "G10 pytest reviewed admission."
    admit_reconcile_cohort_member(
        db_session,
        spec=AdmissionSpec(
            ont_unit_id=ont_id,
            cohort_key="pytest-g10",
            reason_code="pytest_g10",
            explanation=explanation,
            reviewer="pytest-reviewer@dotmac",
            expires_at=datetime.now(UTC) + timedelta(days=1),
        ),
        context=CommandContext.system(
            actor="pytest-operator@dotmac",
            scope=f"ont:{ont_id}",
            reason=explanation,
            idempotency_key=f"pytest-g10-admit-{ont_id}",
        ),
    )
    return SimpleNamespace(id=ont_id)


def test_g10_selects_only_stale_oldest_first_and_bounded(db_session, olt):
    fresh = _make_ont(db_session, olt, 0, observed_age_hours=1)
    old = _make_ont(db_session, olt, 1, observed_age_hours=30)
    older = _make_ont(db_session, olt, 2, observed_age_hours=48)
    cutoff = datetime.now(UTC) - timedelta(hours=6)
    ids = stale_observation_ont_ids(db_session, olt.id, older_than=cutoff, limit=5)
    assert ids == [older.id, old.id]
    assert fresh.id not in ids
    assert stale_observation_ont_ids(
        db_session, olt.id, older_than=cutoff, limit=1
    ) == [older.id]


def test_g10_active_operation_on_olt_or_its_ont_blocks_the_olt(db_session, olt):
    ont = _make_ont(db_session, olt, 0, observed_age_hours=30)
    assert olt_has_active_operation(db_session, olt.id) is False
    op = NetworkOperation(
        operation_type=NetworkOperationType.ont_service_config,
        target_type=NetworkOperationTargetType.ont,
        target_id=ont.id,
        status=NetworkOperationStatus.running,
    )
    db_session.add(op)
    db_session.commit()
    assert olt_has_active_operation(db_session, olt.id) is True
    op.status = NetworkOperationStatus.succeeded
    db_session.commit()
    assert olt_has_active_operation(db_session, olt.id) is False


def test_g10_refresh_is_bounded_read_only_and_updates_only_olt_age(db_session, olt):
    onts = [_make_ont(db_session, olt, i, observed_age_hours=30 + i) for i in range(4)]
    before = {
        o.id: db_session.scalars(
            select(OntObservation).where(OntObservation.ont_unit_id == o.id)
        ).one()
        for o in onts
    }
    prior_reconciled = {k: v.last_reconciled_at for k, v in before.items()}
    reads: list[object] = []

    def fake_read(adapter, desired):
        reads.append(desired.serial_number)
        return SimpleNamespace(status="present", observed=_observed().olt, error=None)

    outcome = refresh_olt_observations(
        _factory(db_session),
        olt.id,
        ObservationRefreshPolicy(max_age=timedelta(hours=6), per_olt_limit=2),
        adapter_factory=lambda db, o: MagicMock(),
        read_fn=fake_read,
    )
    assert outcome.refreshed == 2
    assert len(reads) == 2
    db_session.expire_all()
    rows = {
        o.id: db_session.scalars(
            select(OntObservation).where(OntObservation.ont_unit_id == o.id)
        ).one()
        for o in onts
    }
    recent = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=5)
    refreshed = [
        r for r in rows.values() if r.olt_observed_at.replace(tzinfo=None) > recent
    ]
    assert len(refreshed) == 2
    for ont_id, row in rows.items():
        assert row.last_reconciled_at == prior_reconciled[ont_id]
    for ont in onts:
        assert db_session.get(OntUnit, ont.id).sync_status == OntSyncStatus.synced


def test_g10_refresh_skips_an_olt_with_a_running_operation(db_session, olt):
    ont = _make_ont(db_session, olt, 0, observed_age_hours=30)
    db_session.add(
        NetworkOperation(
            operation_type=NetworkOperationType.olt_ont_sync,
            target_type=NetworkOperationTargetType.olt,
            target_id=olt.id,
            status=NetworkOperationStatus.running,
        )
    )
    db_session.commit()
    read = MagicMock()
    outcome = refresh_olt_observations(
        _factory(db_session),
        olt.id,
        ObservationRefreshPolicy(max_age=timedelta(hours=6), per_olt_limit=5),
        adapter_factory=lambda db, o: MagicMock(),
        read_fn=read,
    )
    assert outcome.skipped_olt_busy is True
    read.assert_not_called()
    assert ont.id


def test_g10_unavailable_read_keeps_the_previous_observation(db_session, olt):
    ont = _make_ont(db_session, olt, 0, observed_age_hours=30)
    old = (
        db_session.scalars(
            select(OntObservation).where(OntObservation.ont_unit_id == ont.id)
        )
        .one()
        .olt_observed_at
    )
    outcome = refresh_olt_observations(
        _factory(db_session),
        olt.id,
        ObservationRefreshPolicy(max_age=timedelta(hours=6), per_olt_limit=5),
        adapter_factory=lambda db, o: MagicMock(),
        read_fn=lambda a, d: SimpleNamespace(
            status="unavailable", observed=None, error="timeout"
        ),
    )
    assert outcome.failed == 1 and outcome.refreshed == 0
    db_session.expire_all()
    after = (
        db_session.scalars(
            select(OntObservation).where(OntObservation.ont_unit_id == ont.id)
        )
        .one()
        .olt_observed_at
    )
    assert after == old


def test_g10_observation_age_helper(db_session, olt):
    ont = _make_ont(db_session, olt, 0, observed_age_hours=30)
    observed_at, age = olt_observation_age(db_session, ont.id)
    assert observed_at is not None
    assert 29.9 <= age <= 30.2
    assert olt_observation_age(db_session, uuid.uuid4()) == (None, None)


def test_g10_settings_defaults_and_schedule_registered():
    from app.models.domain_settings import SettingDomain
    from app.services.settings_spec import SETTINGS_SPECS  # noqa: F401

    specs = {
        s.key: s.default
        for s in SETTINGS_SPECS
        if s.domain == SettingDomain.network_monitoring
    }
    assert specs["ont_olt_observation_max_age_hours"] == 6
    assert specs["ont_olt_observation_refresh_per_olt"] == 5
    assert specs["ont_olt_observation_refresh_interval_seconds"] >= 300
    scheduler = Path("app/services/scheduler_config.py").read_text()
    assert "app.tasks.ont_reconcile.refresh_stale_olt_observations" in scheduler


def test_g10_overview_shows_the_olt_observation_age():
    html = Path("templates/admin/network/onts/_tab_overview.html").read_text()
    assert "olt_observation_age_hours" in html


def test_g10_refresh_skips_the_olt_when_the_limiter_reports_busy(
    db_session, olt, monkeypatch
):
    from app.services.network.reconcile import observation_refresh as mod

    _make_ont(db_session, olt, 0, observed_age_hours=30)
    _make_ont(db_session, olt, 1, observed_age_hours=31)
    monkeypatch.setattr(mod, "read_reports_olt_busy", lambda error: True)
    calls = []

    def busy_read(adapter, desired):
        calls.append(desired.serial_number)
        return SimpleNamespace(status="unavailable", observed=None, error="busy")

    outcome = refresh_olt_observations(
        _factory(db_session),
        olt.id,
        ObservationRefreshPolicy(max_age=timedelta(hours=6), per_olt_limit=5),
        adapter_factory=lambda db, o: MagicMock(),
        read_fn=busy_read,
    )
    assert outcome.skipped_olt_busy is True
    assert outcome.refreshed == 0
    assert len(calls) == 1  # stopped after the first busy answer


def test_g10_background_scope_sets_background_priority():
    from app.services.network.olt_ssh_limiter import OltSshPriority, current_priority
    from app.services.network.reconcile.observation_refresh import (
        background_olt_scope,
    )

    with background_olt_scope():
        assert current_priority() is OltSshPriority.BACKGROUND
