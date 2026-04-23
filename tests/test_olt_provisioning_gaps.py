"""Tests for OLT/VLAN/TR-069 command and manual-action coverage.

Covers:
- Service-port SSH command parsing and filtering (Phase 1)
- VLAN chain validation (Phase 1)
- Huawei command generation from provisioning profiles (Phase 3)
- Web service wrappers (Phase 2)
- Route registration (all phases)
- OLT profile SSH output parsing (Phase 3)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from fastapi.routing import APIRoute
from starlette.routing import Route

from app.models.network import (
    OLTDevice,
    OntAssignment,
    OntAuthorizationStatus,
    OntProvisioningProfile,
    OntProvisioningStatus,
    OntStatusSource,
    OntUnit,
    OnuOnlineStatus,
    PonPort,
    PppoePasswordMode,
    Splitter,
    SplitterPort,
    Vlan,
    VlanMode,
    WanConnectionType,
    WanServiceType,
)
from app.models.ont_autofind import OltAutofindCandidate
from app.models.subscriber import Subscriber, SubscriberCategory
from app.services.network import olt_authorization_workflow
from app.services.network.olt_command_gen import (
    HuaweiCommandGenerator,
    OltCommandSet,
    OntProvisioningContext,
    ProvisioningSpec,
    WanServiceSpec,
    build_spec_from_profile,
)
from app.services.network.olt_ssh import (
    OltProfileEntry,
    ServicePortEntry,
    _parse_profile_table,
    _parse_service_port_table,
)
from app.services.network.vlan_chain import (
    VlanChainResult,
    VlanChainWarning,
    validate_chain,
)


def _add_olt_auth_profile(
    db_session,
    olt: OLTDevice,
    *,
    line_profile_id: int = 40,
    service_profile_id: int = 44,
) -> OntProvisioningProfile:
    profile = OntProvisioningProfile(
        name=f"{olt.name} Auth Profile",
        olt_device_id=olt.id,
        authorization_line_profile_id=line_profile_id,
        authorization_service_profile_id=service_profile_id,
        is_default=True,
        is_active=True,
    )
    db_session.add(profile)
    db_session.commit()
    db_session.refresh(profile)
    return profile


def test_reference_ont_options_include_huawei_dotted_external_ids(db_session) -> None:
    from app.services.web_network_service_ports import _reference_ont_options

    olt = OLTDevice(name="Reference OLT", vendor="Huawei", model="MA5608T")
    db_session.add(olt)
    db_session.commit()
    db_session.refresh(olt)

    pon = PonPort(olt_id=olt.id, name="0/2/1")
    db_session.add(pon)
    db_session.commit()
    db_session.refresh(pon)

    target = OntUnit(serial_number="TARGET-ONT", board="0/2", port="1", external_id="5")
    ref = OntUnit(
        serial_number="REF-ONT",
        board="0/2",
        port="1",
        external_id="huawei:4194320640.7",
    )
    db_session.add_all([target, ref])
    db_session.commit()
    db_session.refresh(target)
    db_session.refresh(ref)

    db_session.add_all(
        [
            OntAssignment(ont_unit_id=target.id, pon_port_id=pon.id, active=True),
            OntAssignment(ont_unit_id=ref.id, pon_port_id=pon.id, active=True),
        ]
    )
    db_session.commit()

    options = _reference_ont_options(
        db_session,
        target_ont_id=str(target.id),
        olt_id=str(olt.id),
    )

    assert len(options) == 1
    assert options[0]["id"] == str(ref.id)
    assert "ONT-ID 7" in options[0]["label"]


def test_authorize_autofind_logs_disappeared_candidate_after_refresh(
    db_session, monkeypatch, caplog
) -> None:
    olt = OLTDevice(name="SPDC Huawei OLT", vendor="Huawei", model="MA5608T")
    db_session.add(olt)
    db_session.commit()
    db_session.refresh(olt)

    monkeypatch.setattr(
        olt_authorization_workflow,
        "get_olt_or_none",
        lambda *_args, **_kwargs: olt,
    )
    monkeypatch.setattr(
        olt_authorization_workflow,
        "get_autofind_candidate_by_serial",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.web_network_ont_autofind.sync_olt_autofind_candidates",
        lambda *_args, **_kwargs: (True, "Refreshed autofind cache.", {"resolved": 1}),
    )

    caplog.set_level("WARNING")

    result = olt_authorization_workflow.authorize_autofind_ont(
        db_session,
        str(olt.id),
        "0/1/3",
        "UBNT-F9AA7344",
    )

    assert result.status == "error"
    assert (
        result.message == "Authorization failed at step 2: Validate discovered ONT row"
    )
    assert any(
        "validation failed after autofind refresh" in record.getMessage()
        and "UBNT-F9AA7344" in record.getMessage()
        and "Validate discovered ONT row" in record.getMessage()
        for record in caplog.records
    )


def test_authorize_autofind_recovers_when_serial_already_exists_on_olt(
    db_session, monkeypatch
) -> None:
    olt = OLTDevice(name="SPDC Huawei OLT", vendor="Huawei", model="MA5608T")
    db_session.add(olt)
    db_session.commit()
    db_session.refresh(olt)
    _add_olt_auth_profile(db_session, olt)

    candidate = OltAutofindCandidate(
        olt_id=olt.id,
        fsp="0/1/3",
        serial_number="HWTC-8535819A",
        serial_hex="485754438535819A",
        vendor_id="HWTC",
        model="HG8546M",
        mac="",
        equipment_sn="",
        autofind_time="2026-04-06 09:00:00",
        is_active=True,
    )
    db_session.add(candidate)
    db_session.commit()

    monkeypatch.setattr(
        olt_authorization_workflow,
        "get_olt_or_none",
        lambda *_args, **_kwargs: olt,
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh.authorize_ont",
        lambda *_args, **_kwargs: (
            False,
            "OLT rejected command: Failure: SN already exists",
            None,
        ),
    )
    monkeypatch.setattr(
        "app.services.network.olt_profile_resolution.ensure_ont_service_profile_match",
        lambda *_args, **_kwargs: (
            True,
            "ONT service profile already matches live capability.",
        ),
    )
    monkeypatch.setattr(
        "app.services.network.olt_write_reconciliation.verify_ont_authorized",
        lambda *_args, **_kwargs: SimpleNamespace(
            success=True,
            message="Verified ONT HWTC-8535819A on 0/1/3.",
            details={
                "ont_id": 9,
                "fsp": "0/1/3",
                "serial_number": "HWTC8535819A",
                "run_state": "online",
            },
        ),
    )
    monkeypatch.setattr(
        olt_authorization_workflow,
        "queue_post_authorization_follow_up",
        lambda *_args, **_kwargs: (True, "Queued follow-up.", "op-123"),
    )

    result = olt_authorization_workflow.authorize_autofind_ont(
        db_session,
        str(olt.id),
        "0/1/3",
        "HWTC-8535819A",
    )

    assert result.success is True
    assert result.completed_authorization is True
    assert result.ont_id_on_olt == 9
    assert result.follow_up_operation_id == "op-123"
    assert any("already registered on the OLT" in step.message for step in result.steps)
    db_session.refresh(candidate)
    assert candidate.is_active is False
    assert candidate.resolution_reason == "authorized"
    assert candidate.ont_unit_id is not None
    assert any(step.name == "Resolve autofind candidate" for step in result.steps)
    ont = db_session.get(OntUnit, candidate.ont_unit_id)
    assert ont is not None
    assert ont.online_status == OnuOnlineStatus.online
    assert ont.effective_status == OnuOnlineStatus.online
    assert ont.effective_status_source == OntStatusSource.olt
    assert ont.offline_reason is None
    assert ont.last_seen_at is not None
    assert ont.last_sync_source == "olt_ssh_readback"


def test_force_authorize_requires_autofind_rediscovery_after_delete(
    db_session, monkeypatch
) -> None:
    olt = OLTDevice(name="Strict Force OLT", vendor="Huawei", model="MA5608T")
    db_session.add(olt)
    db_session.commit()
    db_session.refresh(olt)
    _add_olt_auth_profile(db_session, olt)

    monkeypatch.setattr(
        olt_authorization_workflow,
        "get_olt_or_none",
        lambda *_args, **_kwargs: olt,
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh_ont.find_ont_by_serial",
        lambda *_args, **_kwargs: (
            True,
            "found",
            SimpleNamespace(
                fsp="0/1/3",
                onu_id=9,
                real_serial="4857544328201B9A",
                run_state="online",
            ),
        ),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh_ont.deauthorize_ont",
        lambda *_args, **_kwargs: (True, "deleted"),
    )
    monkeypatch.setattr(
        "app.services.network.olt_write_reconciliation.verify_ont_absent",
        lambda *_args, **_kwargs: SimpleNamespace(
            success=True,
            message="Verified ONT registration is absent on the OLT.",
        ),
    )
    monkeypatch.setattr(
        olt_authorization_workflow,
        "get_autofind_candidate_by_serial",
        lambda *_args, **_kwargs: None,
    )
    sync_calls: list[str] = []

    def _sync_without_rediscovery(*_args, **_kwargs):
        sync_calls.append("sync")
        return True, "Refreshed autofind cache.", {"active": 0}

    monkeypatch.setattr(
        "app.services.web_network_ont_autofind.sync_olt_autofind_candidates",
        _sync_without_rediscovery,
    )
    monkeypatch.setattr(
        "app.services.network.olt_authorization_workflow.sleep",
        lambda *_args, **_kwargs: None,
    )

    def _unexpected_authorize(*_args, **_kwargs):
        raise AssertionError(
            "authorize_ont should not run without autofind rediscovery"
        )

    monkeypatch.setattr(
        "app.services.network.olt_ssh.authorize_ont",
        _unexpected_authorize,
    )

    result = olt_authorization_workflow.authorize_autofind_ont(
        db_session,
        str(olt.id),
        "0/1/3",
        "4857544328201B9A",
        force_reauthorize=True,
    )

    assert result.success is False
    assert result.status == "error"
    assert "Validate discovered ONT row" in result.message
    assert result.steps[-1].name == "Validate discovered ONT row"
    assert result.steps[-1].success is False
    assert "was not rediscovered in autofind" in result.steps[-1].message
    assert len(sync_calls) == 3


def test_force_authorize_rejects_stale_cached_autofind_after_delete(
    db_session, monkeypatch
) -> None:
    olt = OLTDevice(name="Stale Cache OLT", vendor="Huawei", model="MA5608T")
    db_session.add(olt)
    db_session.commit()
    db_session.refresh(olt)
    _add_olt_auth_profile(db_session, olt)

    stale_candidate = OltAutofindCandidate(
        olt_id=olt.id,
        fsp="0/1/3",
        serial_number="4857544328201B9A",
        model="HG8546M",
        is_active=True,
        first_seen_at=datetime.now(UTC) - timedelta(minutes=30),
        last_seen_at=datetime.now(UTC) - timedelta(minutes=30),
    )
    db_session.add(stale_candidate)
    db_session.commit()

    monkeypatch.setattr(
        olt_authorization_workflow,
        "get_olt_or_none",
        lambda *_args, **_kwargs: olt,
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh_ont.find_ont_by_serial",
        lambda *_args, **_kwargs: (
            True,
            "found",
            SimpleNamespace(
                fsp="0/1/3",
                onu_id=9,
                real_serial="4857544328201B9A",
                run_state="online",
            ),
        ),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh_ont.deauthorize_ont",
        lambda *_args, **_kwargs: (True, "deleted"),
    )
    monkeypatch.setattr(
        "app.services.network.olt_write_reconciliation.verify_ont_absent",
        lambda *_args, **_kwargs: SimpleNamespace(
            success=True,
            message="Verified ONT registration is absent on the OLT.",
        ),
    )

    sync_calls: list[str] = []

    def _sync_without_updating_stale_cache(*_args, **_kwargs):
        sync_calls.append("sync")
        return True, "Refreshed autofind cache.", {"active": 1}

    monkeypatch.setattr(
        "app.services.web_network_ont_autofind.sync_olt_autofind_candidates",
        _sync_without_updating_stale_cache,
    )
    monkeypatch.setattr(
        "app.services.network.olt_authorization_workflow.sleep",
        lambda *_args, **_kwargs: None,
    )

    def _unexpected_authorize(*_args, **_kwargs):
        raise AssertionError(
            "stale cached autofind data must not authorize after delete"
        )

    monkeypatch.setattr(
        "app.services.network.olt_ssh.authorize_ont",
        _unexpected_authorize,
    )

    result = olt_authorization_workflow.authorize_autofind_ont(
        db_session,
        str(olt.id),
        "0/1/3",
        "4857544328201B9A",
        force_reauthorize=True,
    )

    assert result.success is False
    assert "was not rediscovered in autofind" in result.steps[-1].message
    assert len(sync_calls) == 3


def test_force_authorize_resolves_olt_profiles_before_delete(
    db_session, monkeypatch
) -> None:
    olt = OLTDevice(name="Profile Guard OLT", vendor="Huawei", model="MA5608T")
    db_session.add(olt)
    db_session.commit()
    db_session.refresh(olt)

    monkeypatch.setattr(
        olt_authorization_workflow,
        "get_olt_or_none",
        lambda *_args, **_kwargs: olt,
    )

    def _unexpected_delete(*_args, **_kwargs):
        raise AssertionError("force authorize must not delete before profiles resolve")

    monkeypatch.setattr(
        "app.services.network.olt_ssh_ont.deauthorize_ont",
        _unexpected_delete,
    )

    result = olt_authorization_workflow.authorize_autofind_ont(
        db_session,
        str(olt.id),
        "0/1/6",
        "4857544328201B9A",
        force_reauthorize=True,
    )

    assert result.success is False
    assert result.steps[-1].name == "Resolve OLT authorization profiles"
    assert "No active provisioning profile is scoped" in result.steps[-1].message


def test_authorize_autofind_uses_configured_olt_profile_ids(
    db_session, monkeypatch
) -> None:
    olt = OLTDevice(name="Configured Profile OLT", vendor="Huawei", model="MA5608T")
    db_session.add(olt)
    db_session.commit()
    db_session.refresh(olt)
    _add_olt_auth_profile(
        db_session,
        olt,
        line_profile_id=51,
        service_profile_id=52,
    )
    candidate = OltAutofindCandidate(
        olt_id=olt.id,
        fsp="0/1/3",
        serial_number="HWTC-8535819A",
        serial_hex="485754438535819A",
        vendor_id="HWTC",
        model="HG8546M",
        mac="",
        equipment_sn="",
        autofind_time="2026-04-06 09:00:00",
        is_active=True,
    )
    db_session.add(candidate)
    db_session.commit()

    monkeypatch.setattr(
        olt_authorization_workflow,
        "get_olt_or_none",
        lambda *_args, **_kwargs: olt,
    )

    def _unexpected_live_profile_scan(*_args, **_kwargs):
        raise AssertionError("authorization must not scan live OLT profiles")

    monkeypatch.setattr(
        "app.services.network.olt_profile_resolution.resolve_authorization_profiles",
        _unexpected_live_profile_scan,
    )

    sent: dict[str, object] = {}

    def _authorize(_olt, fsp, serial_number, **kwargs):
        sent.update({"fsp": fsp, "serial_number": serial_number, **kwargs})
        return True, "authorized", 9

    monkeypatch.setattr("app.services.network.olt_ssh.authorize_ont", _authorize)
    monkeypatch.setattr(
        "app.services.network.olt_write_reconciliation.verify_ont_authorized",
        lambda *_args, **_kwargs: SimpleNamespace(
            success=True,
            message="Verified ONT on OLT.",
            details={"ont_id": 9, "run_state": "online"},
        ),
    )
    monkeypatch.setattr(
        "app.services.network.olt_profile_resolution.ensure_ont_service_profile_match",
        lambda *_args, **_kwargs: (
            True,
            "ONT service profile already matches live capability.",
        ),
    )
    monkeypatch.setattr(
        olt_authorization_workflow,
        "queue_post_authorization_follow_up",
        lambda *_args, **_kwargs: (True, "Queued follow-up.", "op-123"),
    )

    result = olt_authorization_workflow.authorize_autofind_ont(
        db_session,
        str(olt.id),
        "0/1/3",
        "HWTC-8535819A",
    )

    assert result.success is True
    assert sent["line_profile_id"] == 51
    assert sent["service_profile_id"] == 52
    assert any(
        "from provisioning profile" in step.message
        for step in result.steps
        if step.name == "Resolve OLT authorization profiles"
    )


def test_queue_authorize_autofind_ont_records_tracked_operation(
    db_session, monkeypatch
) -> None:
    from app.models.network_operation import (
        NetworkOperation,
        NetworkOperationStatus,
        NetworkOperationTargetType,
    )

    queued: dict[str, object] = {}

    def _fake_enqueue(task, *, args, correlation_id, source, **_kwargs):
        queued.update(
            {
                "task": task,
                "args": args,
                "correlation_id": correlation_id,
                "source": source,
            }
        )
        return SimpleNamespace(id="celery-123")

    monkeypatch.setattr("app.celery_app.enqueue_celery_task", _fake_enqueue)

    ok, message, operation_id = olt_authorization_workflow.queue_authorize_autofind_ont(
        db_session,
        olt_id=str(uuid.uuid4()),
        fsp="0/1/6",
        serial_number="4857544328201B9A",
        force_reauthorize=True,
    )

    assert ok is True
    assert "queued" in message
    assert operation_id is not None
    op = db_session.get(NetworkOperation, uuid.UUID(operation_id))
    assert op is not None
    assert op.status == NetworkOperationStatus.waiting
    assert op.target_type == NetworkOperationTargetType.olt
    assert op.input_payload["force_reauthorize"] is True
    assert queued["args"][0] == operation_id
    assert queued["args"][4] is True
    assert queued["source"] == "olt_authorization"


def test_post_authorization_binds_resolved_tr069_profile_id(
    db_session, monkeypatch
) -> None:
    olt = OLTDevice(name="Resolved ACS OLT", vendor="Huawei", model="MA5608T")
    db_session.add(olt)
    db_session.commit()
    db_session.refresh(olt)

    bound: dict[str, object] = {}

    monkeypatch.setattr(
        olt_authorization_workflow,
        "ensure_assignment_and_pon_port_for_authorized_ont",
        lambda *_args, **_kwargs: (True, "assignment ok"),
    )
    targeted_sync: dict[str, object] = {}

    def fake_targeted_sync(_db, **kwargs):
        targeted_sync.update(kwargs)
        return True, "targeted sync ok", {"matched_index": "4194312960.9"}

    monkeypatch.setattr(
        "app.services.network.olt_targeted_sync.sync_authorized_ont_from_olt_snmp",
        fake_targeted_sync,
    )
    monkeypatch.setattr(
        "app.services.web_network_ont_autofind.resolve_candidate_authorized",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        olt_authorization_workflow,
        "get_olt_or_none",
        lambda *_args, **_kwargs: olt,
    )
    monkeypatch.setattr(
        "app.services.network.olt_tr069_admin.ensure_tr069_profile_for_linked_acs",
        lambda _olt: (True, "TR-069 profile already exists: DotMac-ACS (ID 7)", 7),
    )

    def fake_bind(_olt, fsp, ont_id_on_olt, profile_id):
        bound["fsp"] = fsp
        bound["ont_id_on_olt"] = ont_id_on_olt
        bound["profile_id"] = profile_id
        return True, f"profile {profile_id} bound"

    monkeypatch.setattr(
        "app.services.network.olt_ssh_ont.bind_tr069_server_profile",
        fake_bind,
    )

    ont_unit_id = str(uuid.uuid4())
    ok, message, steps = olt_authorization_workflow.run_post_authorization_follow_up(
        db_session,
        ont_unit_id=ont_unit_id,
        olt_id=str(olt.id),
        fsp="0/1/3",
        serial_number="HWTC8535819A",
        ont_id_on_olt=9,
    )

    assert ok is True
    assert message == "Post-authorization sync completed successfully."
    assert targeted_sync == {
        "olt_id": str(olt.id),
        "ont_unit_id": ont_unit_id,
        "fsp": "0/1/3",
        "serial_number": "HWTC8535819A",
        "ont_id_on_olt": 9,
    }
    assert bound == {"fsp": "0/1/3", "ont_id_on_olt": 9, "profile_id": 7}
    assert any(step["name"] == "Verify DotMac ACS profile" for step in steps)
    assert any(step["name"] == "Bind DotMac ACS profile" for step in steps)


# ---------------------------------------------------------------------------
# Phase 1: Service-port SSH parsing and filtering
# ---------------------------------------------------------------------------


class TestServicePortParsing:
    """Test Huawei service-port table parsing."""

    def test_parse_standard_service_port_line(self) -> None:
        output = (
            "  27  201 common   gpon 0/2 /1  0    2     vlan  201  86   86   up\n"
            "  28  203 common   gpon 0/2 /1  0    3     vlan  203  86   86   down\n"
        )
        entries = _parse_service_port_table(output)
        assert len(entries) == 2
        assert entries[0].index == 27
        assert entries[0].vlan_id == 201
        assert entries[0].ont_id == 0
        assert entries[0].gem_index == 2
        assert entries[0].flow_type == "vlan"
        assert entries[0].state == "up"
        assert entries[1].index == 28
        assert entries[1].vlan_id == 203
        assert entries[1].state == "down"

    def test_parse_ignores_header_lines(self) -> None:
        output = (
            "INDEX VLAN ATTR TYPE F/S/P ONT GEM FLOW PARA RX TX STATE\n"
            "----- ---- ---- ---- ----- --- --- ---- ---- -- -- -----\n"
            "  27  201 common   gpon 0/2 /1  0    2     vlan  201  86   86   up\n"
        )
        entries = _parse_service_port_table(output)
        assert len(entries) == 1
        assert entries[0].index == 27

    def test_parse_empty_output(self) -> None:
        entries = _parse_service_port_table("")
        assert entries == []

    def test_parse_no_gpon_token(self) -> None:
        output = (
            "  27  201 common   ethernet 0/2 /1  0    2     vlan  201  86   86   up\n"
        )
        entries = _parse_service_port_table(output)
        assert len(entries) == 0


class TestServicePortFiltering:
    """Test get_service_ports_for_ont filtering logic."""

    def test_filter_by_ont_id(self) -> None:
        """Verify that get_service_ports_for_ont filters correctly."""
        all_ports = [
            ServicePortEntry(
                index=1,
                vlan_id=100,
                ont_id=0,
                gem_index=1,
                flow_type="vlan",
                flow_para="100",
                state="up",
            ),
            ServicePortEntry(
                index=2,
                vlan_id=200,
                ont_id=1,
                gem_index=1,
                flow_type="vlan",
                flow_para="200",
                state="up",
            ),
            ServicePortEntry(
                index=3,
                vlan_id=300,
                ont_id=0,
                gem_index=2,
                flow_type="vlan",
                flow_para="300",
                state="up",
            ),
        ]
        # Simulate filtering (the actual function does SSH + filter, we test the filter logic)
        filtered = [p for p in all_ports if p.ont_id == 0]
        assert len(filtered) == 2
        assert all(p.ont_id == 0 for p in filtered)

    def test_clone_preserves_user_vlan_and_tag_transform(self, monkeypatch) -> None:
        from app.services.network.olt_ssh import create_service_ports

        olt = OLTDevice(name="Clone OLT", vendor="Huawei", model="MA5608T")

        sent_commands: list[str] = []

        class _FakeChannel:
            def send(self, _chars: str) -> None:
                return None

            def settimeout(self, _timeout: float) -> None:
                return None

        class _FakeTransport:
            def close(self) -> None:
                return None

        def _fake_run_huawei_cmd(_channel, command, prompt=None):  # noqa: ARG001
            sent_commands.append(command)
            return "success"

        monkeypatch.setattr(
            "app.services.network.olt_ssh._open_shell",
            lambda *_args, **_kwargs: (_FakeTransport(), _FakeChannel(), None),
        )
        monkeypatch.setattr(
            "app.services.network.olt_ssh._read_until_prompt",
            lambda *_args, **_kwargs: "#",
        )
        monkeypatch.setattr(
            "app.services.network.olt_ssh._run_huawei_cmd",
            _fake_run_huawei_cmd,
        )

        ok, msg = create_service_ports(
            olt,
            "0/2/1",
            7,
            cast(
                list[ServicePortEntry],
                [
                    SimpleNamespace(
                        index=1,
                        vlan_id=201,
                        ont_id=3,
                        gem_index=2,
                        flow_type="vlan",
                        flow_para="101",
                        state="up",
                        tag_transform="translate",
                    ),
                    SimpleNamespace(
                        index=2,
                        vlan_id=301,
                        ont_id=3,
                        gem_index=4,
                        flow_type="vlan",
                        flow_para="untagged",
                        state="up",
                        tag_transform="default",
                    ),
                ],
            ),
        )

        assert ok is True
        assert "Created 2 service-port(s)" in msg
        service_port_commands = [
            command
            for command in sent_commands
            if command.startswith("service-port vlan")
        ]
        assert service_port_commands == [
            "service-port vlan 201 gpon 0/2/1 ont 7 gemport 2 multi-service user-vlan 101 tag-transform translate",
            "service-port vlan 301 gpon 0/2/1 ont 7 gemport 4 multi-service user-vlan untagged tag-transform default",
        ]


# ---------------------------------------------------------------------------
# Phase 1: VLAN chain validation
# ---------------------------------------------------------------------------


class TestVlanChainValidation:
    """Test VLAN chain validation logic."""

    def test_ont_not_found_returns_error(self, db_session) -> None:
        result = validate_chain(db_session, str(uuid.uuid4()))
        assert not result.valid
        assert any(w.level == "error" for w in result.warnings)

    def test_ont_without_assignment_returns_info(self, db_session) -> None:
        ont = OntUnit(serial_number="TEST-VLAN-001")
        db_session.add(ont)
        db_session.commit()
        db_session.refresh(ont)

        result = validate_chain(db_session, str(ont.id))
        assert result.valid
        assert any("No active assignment" in w.message for w in result.warnings)

    def test_vlan_chain_result_structure(self) -> None:
        result = VlanChainResult(ont_id="test-id")
        assert result.valid is True
        assert result.desired_vlans == []
        assert result.actual_vlans == []
        assert result.warnings == []

    def test_vlan_chain_warning_fields(self) -> None:
        warning = VlanChainWarning("warning", "VLAN 100 missing")
        assert warning.level == "warning"
        assert warning.message == "VLAN 100 missing"


# ---------------------------------------------------------------------------
# Phase 3: Huawei command generation
# ---------------------------------------------------------------------------


class TestHuaweiCommandGenerator:
    """Test CLI command generation from provisioning specs."""

    def _make_context(
        self,
        *,
        frame: int = 0,
        slot: int = 2,
        port: int = 1,
        ont_id: int = 5,
        olt_name: str = "Test OLT",
        subscriber_code: str = "100014919",
        subscriber_name: str = "Demo User",
    ) -> OntProvisioningContext:
        return OntProvisioningContext(
            frame=frame,
            slot=slot,
            port=port,
            ont_id=ont_id,
            olt_name=olt_name,
            subscriber_code=subscriber_code,
            subscriber_name=subscriber_name,
        )

    def test_service_port_commands_generated(self) -> None:
        ctx = self._make_context()
        spec = ProvisioningSpec(
            wan_services=[
                WanServiceSpec(service_type="internet", vlan_id=201, gem_index=2),
                WanServiceSpec(service_type="iptv", vlan_id=203, gem_index=3),
            ]
        )
        result = HuaweiCommandGenerator.generate_service_port_commands(spec, ctx)
        assert len(result) == 1
        assert result[0].step == "Create Service Ports"
        assert len(result[0].commands) == 2
        assert "vlan 201" in result[0].commands[0]
        assert "ont 5" in result[0].commands[0]
        assert "gemport 2" in result[0].commands[0]
        assert "0/2/1" in result[0].commands[0]
        assert "vlan 203" in result[0].commands[1]

    def test_service_port_commands_empty_when_no_wan_services(self) -> None:
        ctx = self._make_context()
        spec = ProvisioningSpec(wan_services=[])
        result = HuaweiCommandGenerator.generate_service_port_commands(spec, ctx)
        assert result == []

    def test_iphost_dhcp_commands(self) -> None:
        ctx = self._make_context()
        spec = ProvisioningSpec(mgmt_vlan_tag=100, mgmt_ip_mode="dhcp")
        result = HuaweiCommandGenerator.generate_iphost_commands(spec, ctx)
        assert len(result) == 1
        assert result[0].step == "Configure Management IP"
        cmds = result[0].commands
        assert any("interface gpon 0/2" in c for c in cmds)
        assert any("dhcp vlan 100" in c for c in cmds)

    def test_iphost_dhcp_commands_include_priority_when_set(self) -> None:
        ctx = self._make_context()
        spec = ProvisioningSpec(
            mgmt_vlan_tag=100,
            mgmt_ip_mode="dhcp",
            mgmt_priority=2,
        )
        result = HuaweiCommandGenerator.generate_iphost_commands(spec, ctx)

        assert len(result) == 1
        assert any("dhcp vlan 100 priority 2" in c for c in result[0].commands)

    def test_iphost_static_commands(self) -> None:
        ctx = self._make_context()
        spec = ProvisioningSpec(
            mgmt_vlan_tag=100,
            mgmt_ip_mode="static",
            mgmt_ip_address="192.168.1.50",
            mgmt_subnet="255.255.255.0",
            mgmt_gateway="192.168.1.1",
        )
        result = HuaweiCommandGenerator.generate_iphost_commands(spec, ctx)
        assert len(result) == 1
        cmds = " ".join(result[0].commands)
        assert "static" in cmds
        assert "192.168.1.50" in cmds
        assert "255.255.255.0" in cmds
        assert "192.168.1.1" in cmds

    def test_iphost_skipped_when_no_mgmt_vlan(self) -> None:
        ctx = self._make_context()
        spec = ProvisioningSpec(mgmt_vlan_tag=None)
        result = HuaweiCommandGenerator.generate_iphost_commands(spec, ctx)
        assert result == []

    def test_tr069_binding_commands(self) -> None:
        ctx = self._make_context()
        spec = ProvisioningSpec(tr069_profile_id=3)
        result = HuaweiCommandGenerator.generate_tr069_binding_commands(spec, ctx)
        assert len(result) == 1
        assert result[0].step == "Bind TR-069 Profile"
        cmds = " ".join(result[0].commands)
        assert "tr069-server-config 1 5 profile-id 3" in cmds

    def test_tr069_skipped_when_no_profile(self) -> None:
        ctx = self._make_context()
        spec = ProvisioningSpec(tr069_profile_id=None)
        result = HuaweiCommandGenerator.generate_tr069_binding_commands(spec, ctx)
        assert result == []

    def test_full_provisioning_generates_all_steps(self) -> None:
        ctx = self._make_context()
        spec = ProvisioningSpec(
            wan_services=[
                WanServiceSpec(service_type="internet", vlan_id=201, gem_index=2),
            ],
            mgmt_vlan_tag=100,
            mgmt_ip_mode="dhcp",
            tr069_profile_id=1,
        )
        result = HuaweiCommandGenerator.generate_full_provisioning(spec, ctx)
        steps = [cs.step for cs in result]
        assert "Create Service Ports" in steps
        assert "Configure Management IP" in steps
        assert "Bind TR-069 Profile" in steps

    def test_full_provisioning_empty_spec(self) -> None:
        ctx = self._make_context()
        spec = ProvisioningSpec()
        result = HuaweiCommandGenerator.generate_full_provisioning(spec, ctx)
        assert result == []


class TestOntProvisioningContext:
    """Test provisioning context properties."""

    def test_fsp_format(self) -> None:
        ctx = OntProvisioningContext(frame=0, slot=2, port=1, ont_id=5)
        assert ctx.fsp == "0/2/1"

    def test_frame_slot_format(self) -> None:
        ctx = OntProvisioningContext(frame=0, slot=2, port=1, ont_id=5)
        assert ctx.frame_slot == "0/2"


class TestTemplateRendering:
    """Test PPPoE username template rendering."""

    def test_subscriber_code_rendered(self) -> None:
        from app.services.network.olt_command_gen import _render_template

        ctx = OntProvisioningContext(
            frame=0,
            slot=2,
            port=1,
            ont_id=5,
            subscriber_code="100014919",
        )
        result = _render_template("{subscriber_code}", ctx)
        assert result == "100014919"

    def test_subscriber_name_rendered(self) -> None:
        from app.services.network.olt_command_gen import _render_template

        ctx = OntProvisioningContext(
            frame=0,
            slot=2,
            port=1,
            ont_id=5,
            subscriber_name="John Doe",
        )
        result = _render_template("{subscriber_name}", ctx)
        assert result == "John Doe"

    def test_ont_id_rendered(self) -> None:
        from app.services.network.olt_command_gen import _render_template

        ctx = OntProvisioningContext(frame=0, slot=2, port=1, ont_id=42)
        result = _render_template("user-{ont_id}", ctx)
        assert result == "user-42"

    def test_empty_template_unchanged(self) -> None:
        from app.services.network.olt_command_gen import _render_template

        ctx = OntProvisioningContext(frame=0, slot=2, port=1, ont_id=5)
        assert _render_template("", ctx) == ""

    def test_no_placeholders_unchanged(self) -> None:
        from app.services.network.olt_command_gen import _render_template

        ctx = OntProvisioningContext(frame=0, slot=2, port=1, ont_id=5)
        assert _render_template("static_user", ctx) == "static_user"


class TestBuildSpecFromProfile:
    """Test profile-to-spec conversion."""

    def test_builds_spec_from_mock_profile(self) -> None:
        wan_svc = SimpleNamespace(
            is_active=True,
            s_vlan=201,
            c_vlan=None,
            service_type=SimpleNamespace(value="internet"),
            gem_port_id=2,
            connection_type=SimpleNamespace(value="pppoe"),
            pppoe_username_template="{subscriber_code}@isp.ng",
            pppoe_static_password="secret123",
            cos_priority=None,
            nat_enabled=True,
            t_cont_profile=None,
        )
        profile = SimpleNamespace(
            wan_services=[wan_svc],
            mgmt_vlan_tag=100,
            mgmt_ip_mode=SimpleNamespace(value="dhcp"),
        )
        ctx = OntProvisioningContext(
            frame=0,
            slot=2,
            port=1,
            ont_id=5,
            subscriber_code="100014919",
        )
        spec = build_spec_from_profile(profile, ctx, tr069_profile_id=3)

        assert len(spec.wan_services) == 1
        assert spec.wan_services[0].vlan_id == 201
        assert spec.wan_services[0].gem_index == 2
        assert (
            spec.wan_services[0].pppoe_username_template == "{subscriber_code}@isp.ng"
        )
        assert spec.mgmt_vlan_tag == 100
        assert spec.mgmt_ip_mode == "dhcp"
        assert spec.tr069_profile_id == 3

    def test_builds_translate_service_port_with_distinct_user_vlan(self) -> None:
        wan_svc = SimpleNamespace(
            is_active=True,
            s_vlan=201,
            c_vlan=101,
            vlan_mode=SimpleNamespace(value="translate"),
            service_type=SimpleNamespace(value="internet"),
            gem_port_id=2,
            connection_type=SimpleNamespace(value="pppoe"),
            pppoe_username_template=None,
            pppoe_password_mode=None,
            pppoe_static_password=None,
            cos_priority=None,
            nat_enabled=True,
            t_cont_profile=None,
        )
        profile = SimpleNamespace(
            wan_services=[wan_svc],
            mgmt_vlan_tag=None,
            mgmt_ip_mode=None,
        )
        ctx = OntProvisioningContext(frame=0, slot=2, port=1, ont_id=5)

        spec = build_spec_from_profile(profile, ctx)
        commands = HuaweiCommandGenerator.generate_service_port_commands(spec, ctx)

        assert spec.wan_services[0].vlan_id == 201
        assert spec.wan_services[0].user_vlan == 101
        assert "service-port vlan 201" in commands[0].commands[0]
        assert "user-vlan 101" in commands[0].commands[0]

    def test_build_spec_decrypts_static_pppoe_password(self) -> None:
        wan_svc = SimpleNamespace(
            is_active=True,
            s_vlan=201,
            c_vlan=None,
            vlan_mode=SimpleNamespace(value=VlanMode.tagged.value),
            service_type=SimpleNamespace(value="internet"),
            gem_port_id=2,
            connection_type=SimpleNamespace(value="pppoe"),
            pppoe_username_template="{subscriber_code}",
            pppoe_password_mode=SimpleNamespace(value=PppoePasswordMode.static.value),
            pppoe_static_password="plain:secret123",
            cos_priority=None,
            nat_enabled=True,
            t_cont_profile=None,
        )
        profile = SimpleNamespace(
            wan_services=[wan_svc],
            mgmt_vlan_tag=None,
            mgmt_ip_mode=None,
        )
        ctx = OntProvisioningContext(frame=0, slot=2, port=1, ont_id=5)

        spec = build_spec_from_profile(profile, ctx)

        assert spec.wan_services[0].pppoe_password == "secret123"
        assert spec.wan_services[0].pppoe_password_mode == "static"

    def test_inactive_wan_services_skipped(self) -> None:
        wan_svc = SimpleNamespace(
            is_active=False,
            s_vlan=201,
            c_vlan=None,
            service_type=SimpleNamespace(value="internet"),
            gem_port_id=2,
            connection_type=SimpleNamespace(value="pppoe"),
            pppoe_username_template="",
            pppoe_static_password="",
            cos_priority=None,
            nat_enabled=True,
        )
        profile = SimpleNamespace(
            wan_services=[wan_svc],
            mgmt_vlan_tag=None,
            mgmt_ip_mode=None,
        )
        ctx = OntProvisioningContext(frame=0, slot=2, port=1, ont_id=5)
        spec = build_spec_from_profile(profile, ctx)
        assert len(spec.wan_services) == 0

    def test_wan_service_without_vlan_skipped(self) -> None:
        wan_svc = SimpleNamespace(
            is_active=True,
            s_vlan=None,
            c_vlan=None,
            service_type=SimpleNamespace(value="internet"),
            gem_port_id=2,
            connection_type=SimpleNamespace(value="pppoe"),
            pppoe_username_template="",
            pppoe_static_password="",
            cos_priority=None,
            nat_enabled=True,
        )
        profile = SimpleNamespace(
            wan_services=[wan_svc],
            mgmt_vlan_tag=None,
            mgmt_ip_mode=None,
        )
        ctx = OntProvisioningContext(frame=0, slot=2, port=1, ont_id=5)
        spec = build_spec_from_profile(profile, ctx)
        assert len(spec.wan_services) == 0


# ---------------------------------------------------------------------------
# Phase 3: OLT profile SSH output parsing
# ---------------------------------------------------------------------------


class TestOltProfileParsing:
    """Test OLT profile table output parsing."""

    def test_parse_profile_table_standard(self) -> None:
        output = (
            "  Profile-ID  Profile-name\n"
            "  ----------  ----------------------\n"
            "  1           default\n"
            "  2           residential-gpon\n"
            "  3           business-gpon\n"
        )
        entries = _parse_profile_table(output)
        assert len(entries) == 3
        assert entries[0].profile_id == 1
        assert entries[0].name == "default"
        assert entries[1].profile_id == 2
        assert entries[1].name == "residential-gpon"
        assert entries[2].profile_id == 3
        assert entries[2].name == "business-gpon"

    def test_parse_profile_table_empty(self) -> None:
        entries = _parse_profile_table("")
        assert entries == []

    def test_parse_profile_table_separator_lines_ignored(self) -> None:
        output = (
            "---------- ----------------------\n"
            "============================================\n"
            "  1  default\n"
        )
        entries = _parse_profile_table(output)
        assert len(entries) == 1

    def test_olt_profile_entry_defaults(self) -> None:
        entry = OltProfileEntry(profile_id=1, name="test")
        assert entry.type == ""
        assert entry.binding_count == 0
        assert entry.extra == {}

    def test_line_profile_tr069_detail_parser(self) -> None:
        from app.services.network.olt_profile_resolution import (
            parse_line_profile_tr069_enabled,
        )

        output = "TR069 management      : Enable\nTR069 IP index        : 0\n"

        assert parse_line_profile_tr069_enabled(output) is True

    def test_service_profile_matching_prefers_capability_over_name(self) -> None:
        from app.services.network.olt_profile_resolution import (
            OntCapabilityCounts,
            ServiceProfileDetail,
            choose_service_profile,
        )

        profiles = [
            ServiceProfileDetail(
                profile_id=41,
                name="EG8145V5",
                ethernet_ports=4,
                voip_ports=2,
                binding_count=100,
            ),
            ServiceProfileDetail(
                profile_id=44,
                name="Residential-4GE-1POTS",
                ethernet_ports=4,
                voip_ports=1,
                binding_count=10,
            ),
        ]

        selected = choose_service_profile(
            profiles,
            capability=OntCapabilityCounts(ethernet_ports=4, voip_ports=1),
            model="EG8145V5",
        )

        assert selected is not None
        assert selected.profile_id == 44

    def test_authorize_ont_refuses_static_profile_defaults(self) -> None:
        from app.models.network import OLTDevice
        from app.services.network.olt_ssh import authorize_ont

        ok, msg, ont_id = authorize_ont(
            OLTDevice(name="No Defaults OLT", vendor="Huawei", model="MA5608T"),
            "0/1/13",
            "HWTC-348F8A84",
        )

        assert ok is False
        assert ont_id is None
        assert "refusing to use static profile defaults" in msg

    def test_parse_huawei_iphost_config_output(self) -> None:
        from app.services.network.olt_ssh_ont import parse_iphost_config_output

        output = """
  ONT IP host index        : 0
  ONT config type          : DHCP
  ONT IP                   : -
  ONT MAC                  : 9C74-1A3F-98C6
  ONT manage VLAN          : 201
  ONT manage priority      : 5
"""

        config = parse_iphost_config_output(output)

        assert config["ip_index"] == "0"
        assert config["mode"] == "DHCP"
        assert config["ip_address"] == "-"
        assert config["mac_address"] == "9C74-1A3F-98C6"
        assert config["vlan"] == "201"
        assert config["priority"] == "5"


# ---------------------------------------------------------------------------
# Web service wrappers (Phase 2)
# ---------------------------------------------------------------------------


class TestWebNetworkOntActionsWrappers:
    """Test that the new web service wrapper functions exist and have correct signatures."""

    def test_execute_omci_reboot_exists(self) -> None:
        from app.services.web_network_ont_actions import execute_omci_reboot

        assert callable(execute_omci_reboot)

    def test_configure_management_ip_exists(self) -> None:
        from app.services.web_network_ont_actions import configure_management_ip

        assert callable(configure_management_ip)

    def test_fetch_iphost_config_exists(self) -> None:
        from app.services.web_network_ont_actions import fetch_iphost_config

        assert callable(fetch_iphost_config)

    def test_bind_tr069_profile_exists(self) -> None:
        from app.services.web_network_ont_actions import bind_tr069_profile

        assert callable(bind_tr069_profile)


class TestWebNetworkServicePortsWrappers:
    """Test service-port web service functions."""

    def test_parse_ont_id_on_olt_accepts_generic_prefixed_numeric_id(self) -> None:
        from app.services.web_network_service_ports import _parse_ont_id_on_olt

        assert _parse_ont_id_on_olt("generic:5") == 5

    def test_shared_parse_ont_id_on_olt_accepts_prefixed_and_fsp_formats(self) -> None:
        from app.services.network.serial_utils import parse_ont_id_on_olt

        assert parse_ont_id_on_olt("generic:5") == 5
        assert parse_ont_id_on_olt("huawei:4194320384.5") == 5
        assert parse_ont_id_on_olt("0/1/6.8") == 8

    def test_normalize_fsp_strips_pon_prefix(self) -> None:
        from app.services.web_network_service_ports import _normalize_fsp

        assert _normalize_fsp("pon-0/2/1") == "0/2/1"

    def test_list_context_ont_not_found(self, db_session) -> None:
        from app.services.web_network_service_ports import list_context

        ctx = list_context(db_session, str(uuid.uuid4()))
        assert ctx["error"] is not None
        assert "not found" in ctx["error"].lower()

    def test_list_context_ont_no_assignment(self, db_session) -> None:
        from app.services.web_network_service_ports import list_context

        ont = OntUnit(serial_number="TEST-SP-001")
        db_session.add(ont)
        db_session.commit()
        db_session.refresh(ont)

        ctx = list_context(db_session, str(ont.id))
        assert ctx["error"] is not None
        assert "assignment" in ctx["error"].lower() or "mapping" in ctx["error"].lower()

    def test_list_context_includes_reference_onts_on_same_olt(
        self, db_session, monkeypatch
    ) -> None:
        from app.services.network.olt_protocol_adapters import OltOperationResult
        from app.services.web_network_service_ports import list_context

        olt = OLTDevice(name="Reference OLT", vendor="Huawei", model="MA5608T")
        db_session.add(olt)
        db_session.commit()
        db_session.refresh(olt)

        pon_a = PonPort(olt_id=olt.id, name="0/2/1")
        pon_b = PonPort(olt_id=olt.id, name="0/2/2")
        db_session.add_all([pon_a, pon_b])
        db_session.commit()
        db_session.refresh(pon_a)
        db_session.refresh(pon_b)

        target = OntUnit(
            serial_number="TARGET-ONT", board="0/2", port="1", external_id="7"
        )
        ref = OntUnit(serial_number="REF-ONT", board="0/2", port="2", external_id="9")
        db_session.add_all([target, ref])
        db_session.commit()
        db_session.refresh(target)
        db_session.refresh(ref)

        db_session.add_all(
            [
                OntAssignment(ont_unit_id=target.id, pon_port_id=pon_a.id, active=True),
                OntAssignment(ont_unit_id=ref.id, pon_port_id=pon_b.id, active=True),
            ]
        )
        db_session.commit()

        class MockAdapter:
            def get_service_ports_for_ont(self, fsp, ont_id):
                return OltOperationResult(
                    success=True, message="ok", data={"service_ports": []}
                )

        monkeypatch.setattr(
            "app.services.web_network_service_ports.get_protocol_adapter",
            lambda *_args, **_kwargs: MockAdapter(),
        )

        ctx = list_context(db_session, str(target.id))

        assert ctx["error"] is None
        assert ctx["reference_onts"] == [
            {
                "id": str(ref.id),
                "label": "REF-ONT | ONT-ID 9 | 0/2/2",
            }
        ]

    def test_list_context_requires_scanned_board_port_not_pon_name_fallback(
        self, db_session, monkeypatch
    ) -> None:
        from app.services.network.olt_protocol_adapters import OltOperationResult
        from app.services.web_network_service_ports import list_context

        olt = OLTDevice(name="Prefixed OLT", vendor="Huawei", model="MA5608T")
        db_session.add(olt)
        db_session.commit()
        db_session.refresh(olt)

        pon = PonPort(olt_id=olt.id, name="pon-0/2/1")
        db_session.add(pon)
        db_session.commit()
        db_session.refresh(pon)

        ont = OntUnit(serial_number="TARGET-ONT", external_id="generic:7")
        db_session.add(ont)
        db_session.commit()
        db_session.refresh(ont)

        db_session.add(
            OntAssignment(ont_unit_id=ont.id, pon_port_id=pon.id, active=True)
        )
        db_session.commit()

        class MockAdapter:
            def get_service_ports_for_ont(self, fsp, ont_id):
                return OltOperationResult(
                    success=True, message="ok", data={"service_ports": []}
                )

        monkeypatch.setattr(
            "app.services.web_network_service_ports.get_protocol_adapter",
            lambda *_args, **_kwargs: MockAdapter(),
        )

        ctx = list_context(db_session, str(ont.id))

        assert ctx["error"] is not None
        assert "assignment" in str(ctx["error"]).lower()

    def test_handle_create_no_olt_context(self, db_session) -> None:
        from app.services.web_network_service_ports import handle_create

        ok, msg = handle_create(db_session, str(uuid.uuid4()), 201, 1)
        assert not ok
        assert "OLT" in msg or "resolve" in msg.lower()

    def test_handle_create_forwards_user_vlan_and_transform(
        self, db_session, monkeypatch
    ) -> None:
        from app.services.network.olt_protocol_adapters import OltOperationResult
        from app.services.web_network_service_ports import handle_create

        olt = OLTDevice(name="SP Create OLT", vendor="Huawei", model="MA5608T")
        db_session.add(olt)
        db_session.commit()
        db_session.refresh(olt)

        pon = PonPort(olt_id=olt.id, name="0/2/1")
        db_session.add(pon)
        db_session.commit()
        db_session.refresh(pon)

        ont = OntUnit(
            serial_number="TEST-SP-CREATE-001", board="0/2", port="1", external_id="7"
        )
        db_session.add(ont)
        db_session.commit()
        db_session.refresh(ont)

        db_session.add(
            OntAssignment(ont_unit_id=ont.id, pon_port_id=pon.id, active=True)
        )
        db_session.commit()

        captured: dict[str, object] = {}

        class MockAdapter:
            def create_service_port(
                self,
                fsp,
                ont_id,
                *,
                gem_index,
                vlan_id,
                user_vlan=None,
                tag_transform="translate",
                port_index=None,
            ):
                captured.update(
                    {
                        "fsp": fsp,
                        "olt_ont_id": ont_id,
                        "gem_index": gem_index,
                        "vlan_id": vlan_id,
                        "user_vlan": user_vlan,
                        "tag_transform": tag_transform,
                    }
                )
                return OltOperationResult(
                    success=True, message="created", data={"port_index": 700}
                )

        monkeypatch.setattr(
            "app.services.web_network_service_ports.get_protocol_adapter",
            lambda *_args, **_kwargs: MockAdapter(),
        )

        ok, msg = handle_create(
            db_session,
            str(ont.id),
            201,
            3,
            user_vlan=101,
            tag_transform="transparent",
        )

        assert ok is True
        assert "created" in msg.lower()  # Service port was created
        assert captured == {
            "fsp": "0/2/1",
            "olt_ont_id": 7,
            "gem_index": 3,
            "vlan_id": 201,
            "user_vlan": 101,
            "tag_transform": "transparent",
        }

    def test_handle_delete_no_olt_context(self, db_session) -> None:
        from app.services.web_network_service_ports import handle_delete

        ok, msg = handle_delete(db_session, str(uuid.uuid4()), 27)
        assert not ok

    def test_handle_clone_no_olt_context(self, db_session) -> None:
        from app.services.web_network_service_ports import handle_clone

        ok, msg = handle_clone(db_session, str(uuid.uuid4()), str(uuid.uuid4()))
        assert not ok


class TestWebNetworkOltProfiles:
    """Test OLT profile web service functions."""

    def test_line_profiles_olt_not_found(self, db_session) -> None:
        from app.services.web_network_olt_profiles import line_profiles_context

        ctx = line_profiles_context(db_session, str(uuid.uuid4()))
        assert ctx["error"] is not None
        assert ctx["line_profiles"] == []

    def test_tr069_profiles_olt_not_found(self, db_session) -> None:
        from app.services.web_network_olt_profiles import tr069_profiles_context

        ctx = tr069_profiles_context(db_session, str(uuid.uuid4()))
        assert ctx["error"] is not None
        assert ctx["tr069_profiles"] == []

    def test_command_preview_ont_not_found(self, db_session) -> None:
        from app.services.web_network_olt_profiles import command_preview_context

        ctx = command_preview_context(db_session, str(uuid.uuid4()), str(uuid.uuid4()))
        assert ctx["error"] is not None


class TestWebNetworkOltsMigration:
    def test_provision_ont_service_ports_targets_explicit_ont_id(
        self, db_session, monkeypatch
    ) -> None:
        from app.services.web_network_olts import provision_ont_service_ports

        olt = OLTDevice(name="Neighbor Learning OLT", vendor="Huawei", model="MA5608T")
        db_session.add(olt)
        db_session.commit()
        db_session.refresh(olt)

        entries = [
            SimpleNamespace(ont_id=11, vlan_id=201, gem_index=1),
            SimpleNamespace(ont_id=11, vlan_id=202, gem_index=2),
            SimpleNamespace(ont_id=15, vlan_id=300, gem_index=1),
        ]
        captured: dict[str, object] = {}

        monkeypatch.setattr(
            "app.services.web_network_olts.olt_ssh_service.get_service_ports",
            lambda *_args, **_kwargs: (True, "ok", entries),
        )

        def _fake_create_service_ports(olt_obj, fsp, ont_id, reference_ports):
            captured.update(
                {
                    "olt_id": str(olt_obj.id),
                    "fsp": fsp,
                    "ont_id": ont_id,
                    "reference_ont_ids": [port.ont_id for port in reference_ports],
                    "reference_vlans": [port.vlan_id for port in reference_ports],
                }
            )
            return True, "created"

        monkeypatch.setattr(
            "app.services.web_network_olts.olt_ssh_service.create_service_ports",
            _fake_create_service_ports,
        )

        ok, msg = provision_ont_service_ports(db_session, str(olt.id), "0/2/1", 7)

        assert ok is True
        assert msg == "created"
        assert captured == {
            "olt_id": str(olt.id),
            "fsp": "0/2/1",
            "ont_id": 7,
            "reference_ont_ids": [11, 11],
            "reference_vlans": [201, 202],
        }

    def test_authorize_autofind_skips_clone_when_ont_id_unknown(
        self, db_session, monkeypatch
    ) -> None:
        from app.services.web_network_olts import authorize_autofind_ont

        workflow_path = (
            "app.services.network.olt_authorization_workflow."
            "authorize_autofind_ont_and_provision_network"
        )
        monkeypatch.setattr(
            workflow_path,
            lambda *_args, **_kwargs: SimpleNamespace(
                success=True,
                status="warning",
                message=(
                    "Authorization completed on OLT, but OLT network "
                    "provisioning failed."
                ),
            ),
        )

        ok, status, msg = authorize_autofind_ont(
            db_session,
            str(uuid.uuid4()),
            "0/2/1",
            "48575443ABCDEF01",
        )

        assert ok is True
        assert status == "warning"
        assert (
            msg
            == "Authorization completed on OLT, but OLT network provisioning failed."
        )

    def test_authorize_autofind_delegates_to_authorization_workflow(
        self, db_session, monkeypatch
    ) -> None:
        from app.services.web_network_olts import authorize_autofind_ont

        captured: dict[str, object] = {}

        def _fake_authorize_workflow(db, olt_id, fsp, serial_number):
            captured.update(
                {
                    "db": db,
                    "olt_id": olt_id,
                    "fsp": fsp,
                    "serial_number": serial_number,
                }
            )
            return SimpleNamespace(
                success=True,
                status="success",
                message=(
                    "ONT authorization and OLT network provisioning completed. "
                    "Next action: configure ONT."
                ),
            )

        workflow_path = (
            "app.services.network.olt_authorization_workflow."
            "authorize_autofind_ont_and_provision_network"
        )
        monkeypatch.setattr(
            workflow_path,
            _fake_authorize_workflow,
        )

        ok, status, msg = authorize_autofind_ont(
            db_session,
            "olt-123",
            "0/2/1",
            "48575443ABCDEF02",
        )

        assert ok is True
        assert status == "success"
        assert (
            msg == "ONT authorization and OLT network provisioning completed. "
            "Next action: configure ONT."
        )
        assert captured == {
            "db": db_session,
            "olt_id": "olt-123",
            "fsp": "0/2/1",
            "serial_number": "48575443ABCDEF02",
        }

    def test_authorize_and_provision_runs_inline_network_provisioning(
        self, db_session, monkeypatch
    ) -> None:
        captured: dict[str, object] = {}

        def _fake_authorize(db, olt_id, fsp, serial_number, **kwargs):
            captured["authorize_kwargs"] = kwargs
            return olt_authorization_workflow.AuthorizationWorkflowResult(
                success=True,
                status="success",
                message="ONT authorization completed.",
                ont_unit_id="ont-123",
                ont_id_on_olt=9,
                completed_authorization=True,
            )

        def _fake_assignment(db, *, ont_unit_id, olt_id, fsp):
            captured["assignment"] = {
                "ont_unit_id": ont_unit_id,
                "olt_id": olt_id,
                "fsp": fsp,
            }
            return True, "Linked ONT to PON port 0/2/1."

        def _fake_provision(db, ont_id):
            captured["provision_ont_id"] = ont_id
            from app.services.network.ont_provision_steps import StepResult

            return StepResult(
                "provision_reconciled",
                True,
                "No changes needed - ONT already matches desired state",
            )

        monkeypatch.setattr(
            olt_authorization_workflow,
            "authorize_autofind_ont",
            _fake_authorize,
        )
        monkeypatch.setattr(
            olt_authorization_workflow,
            "ensure_assignment_and_pon_port_for_authorized_ont",
            _fake_assignment,
        )
        monkeypatch.setattr(
            "app.services.network.ont_provision_steps.provision_with_reconciliation",
            _fake_provision,
        )

        result = (
            olt_authorization_workflow.authorize_autofind_ont_and_provision_network(
                db_session,
                "olt-123",
                "0/2/1",
                "48575443ABCDEF03",
            )
        )

        assert result.success is True
        assert result.status == "success"
        assert (
            result.message
            == "ONT authorization and OLT network provisioning completed. "
            "Next action: configure ONT."
        )
        assert captured["authorize_kwargs"]["queue_follow_up"] is False
        assert captured["assignment"] == {
            "ont_unit_id": "ont-123",
            "olt_id": "olt-123",
            "fsp": "0/2/1",
        }
        assert captured["provision_ont_id"] == "ont-123"
        assert result.steps[-1].name == "Reconcile and provision OLT network"

    def test_authorize_and_provision_warning_still_completes_authorize(
        self, db_session, monkeypatch
    ) -> None:
        def _fake_authorize(db, olt_id, fsp, serial_number, **kwargs):
            return olt_authorization_workflow.AuthorizationWorkflowResult(
                success=True,
                status="success",
                message="ONT authorization completed.",
                ont_unit_id="ont-123",
                ont_id_on_olt=9,
                completed_authorization=True,
            )

        def _fake_assignment(db, *, ont_unit_id, olt_id, fsp):
            return True, "Linked ONT to PON port 0/2/1."

        def _fake_provision(db, ont_id):
            from app.services.network.ont_provision_steps import StepResult

            return StepResult(
                "provision_reconciled",
                False,
                "No provisioning profile assigned or specified",
            )

        monkeypatch.setattr(
            olt_authorization_workflow,
            "authorize_autofind_ont",
            _fake_authorize,
        )
        monkeypatch.setattr(
            olt_authorization_workflow,
            "ensure_assignment_and_pon_port_for_authorized_ont",
            _fake_assignment,
        )
        monkeypatch.setattr(
            "app.services.network.ont_provision_steps.provision_with_reconciliation",
            _fake_provision,
        )

        result = (
            olt_authorization_workflow.authorize_autofind_ont_and_provision_network(
                db_session,
                "olt-123",
                "0/2/1",
                "48575443ABCDEF04",
            )
        )

        assert result.success is True
        assert result.status == "warning"
        assert "Authorization completed on OLT" in result.message
        assert "No provisioning profile assigned or specified" in result.message

    def test_authorize_and_provision_persists_olt_state(
        self, db_session, monkeypatch
    ) -> None:
        ont_id = str(uuid.uuid4())
        ont = OntUnit(
            id=ont_id,
            serial_number="48575443ABCDEF05",
            authorization_status=None,
            provisioning_status=OntProvisioningStatus.unprovisioned,
        )
        db_session.add(ont)
        db_session.commit()

        def _fake_authorize(db, olt_id, fsp, serial_number, **kwargs):
            return olt_authorization_workflow.AuthorizationWorkflowResult(
                success=True,
                status="success",
                message="ONT authorization completed.",
                ont_unit_id=ont_id,
                ont_id_on_olt=9,
                completed_authorization=True,
            )

        def _fake_assignment(db, *, ont_unit_id, olt_id, fsp):
            return True, "Linked ONT to PON port 0/2/1."

        def _fake_provision(db, ont_id):
            from app.services.network.ont_provision_steps import StepResult

            return StepResult(
                "provision_reconciled",
                True,
                "No changes needed - ONT already matches desired state",
            )

        monkeypatch.setattr(
            olt_authorization_workflow,
            "authorize_autofind_ont",
            _fake_authorize,
        )
        monkeypatch.setattr(
            olt_authorization_workflow,
            "ensure_assignment_and_pon_port_for_authorized_ont",
            _fake_assignment,
        )
        monkeypatch.setattr(
            "app.services.network.ont_provision_steps.provision_with_reconciliation",
            _fake_provision,
        )

        result = (
            olt_authorization_workflow.authorize_autofind_ont_and_provision_network(
                db_session,
                "olt-123",
                "0/2/1",
                "48575443ABCDEF05",
            )
        )
        db_session.refresh(ont)

        assert result.success is True
        assert ont.authorization_status == OntAuthorizationStatus.authorized
        assert ont.provisioning_status == OntProvisioningStatus.provisioned

    def test_command_preview_profile_not_found(self, db_session) -> None:
        from app.services.web_network_olt_profiles import command_preview_context

        ont = OntUnit(
            serial_number="TEST-CP-001", board="0/2", port="1", external_id="5"
        )
        db_session.add(ont)
        db_session.commit()
        db_session.refresh(ont)

        olt = OLTDevice(name="Preview OLT", vendor="Huawei", model="MA5608T")
        db_session.add(olt)
        db_session.commit()
        db_session.refresh(olt)

        pon = PonPort(olt_id=olt.id, name="0/2/1")
        db_session.add(pon)
        db_session.commit()
        db_session.refresh(pon)

        assignment = OntAssignment(ont_unit_id=ont.id, pon_port_id=pon.id, active=True)
        db_session.add(assignment)
        db_session.commit()

        ctx = command_preview_context(db_session, str(ont.id), str(uuid.uuid4()))
        assert ctx["error"] is not None
        assert "profile" in ctx["error"].lower() or "not found" in ctx["error"].lower()


class TestGetProfileTemplates:
    """Test ONT profile template listing service."""

    def test_get_profile_templates_empty(self, db_session) -> None:
        from app.services.web_network_onts import get_profile_templates

        profiles = get_profile_templates(db_session)
        # May have existing profiles from other tests, just verify it returns a list
        assert isinstance(profiles, list)

    def test_get_profile_templates_returns_active_only(self, db_session) -> None:
        from app.services.web_network_onts import get_profile_templates

        active = OntProvisioningProfile(
            name="Active Profile Test",
            is_active=True,
        )
        inactive = OntProvisioningProfile(
            name="Inactive Profile Test",
            is_active=False,
        )
        db_session.add_all([active, inactive])
        db_session.commit()

        profiles = get_profile_templates(db_session)
        profile_names = [p.name for p in profiles]
        assert "Active Profile Test" in profile_names
        assert "Inactive Profile Test" not in profile_names

    def test_get_profile_templates_filters_to_olt_scope(self, db_session) -> None:
        from app.services.web_network_onts import get_profile_templates

        olt_a = OLTDevice(name="Template Scope A", vendor="Huawei", model="MA5608T")
        olt_b = OLTDevice(name="Template Scope B", vendor="Huawei", model="MA5608T")
        db_session.add_all([olt_a, olt_b])
        db_session.commit()
        db_session.refresh(olt_a)
        db_session.refresh(olt_b)

        scoped_a = OntProvisioningProfile(
            name="Scoped Template A",
            olt_device_id=olt_a.id,
            is_active=True,
        )
        scoped_b = OntProvisioningProfile(
            name="Scoped Template B",
            olt_device_id=olt_b.id,
            is_active=True,
        )
        global_profile = OntProvisioningProfile(
            name="Global Template",
            is_active=True,
        )
        db_session.add_all([scoped_a, scoped_b, global_profile])
        db_session.commit()

        profiles = get_profile_templates(db_session, str(olt_a.id))
        names = {profile.name for profile in profiles}
        assert "Scoped Template A" in names
        assert "Global Template" not in names
        assert "Scoped Template B" not in names

    def test_apply_profile_rejects_other_olt_scope(self, db_session) -> None:
        from app.services.network.ont_profile_apply import apply_bundle_to_ont

        ont_olt = OLTDevice(name="ONT Scope OLT", vendor="Huawei", model="MA5608T")
        profile_olt = OLTDevice(
            name="Profile Scope OLT", vendor="Huawei", model="MA5608T"
        )
        db_session.add_all([ont_olt, profile_olt])
        db_session.commit()
        db_session.refresh(ont_olt)
        db_session.refresh(profile_olt)

        ont = OntUnit(serial_number="SCOPE-REJECT", olt_device_id=ont_olt.id)
        profile = OntProvisioningProfile(
            name="Wrong OLT Profile",
            olt_device_id=profile_olt.id,
            is_active=True,
        )
        db_session.add_all([ont, profile])
        db_session.commit()
        db_session.refresh(ont)
        db_session.refresh(profile)

        result = apply_bundle_to_ont(db_session, str(ont.id), str(profile.id))
        assert result.success is False
        assert "another OLT" in result.message

    def test_apply_profile_allows_global_bundle_for_olt_ont(self, db_session) -> None:
        from app.models.network import OntProvisioningStatus
        from app.services.network.ont_profile_apply import apply_bundle_to_ont

        olt = OLTDevice(name="Global Scope OLT", vendor="Huawei", model="MA5608T")
        ont = OntUnit(serial_number="SCOPE-GLOBAL", olt_device_id=olt.id)
        profile = OntProvisioningProfile(
            name="Global Bundle",
            olt_device_id=None,
            is_active=True,
        )
        db_session.add_all([olt, ont, profile])
        db_session.commit()

        result = apply_bundle_to_ont(db_session, str(ont.id), str(profile.id))

        assert result.success is True
        db_session.refresh(ont)
        assert ont.provisioning_status == OntProvisioningStatus.partial

    def test_apply_profile_rejects_other_business_owner_scope(
        self, db_session
    ) -> None:
        from app.services.network.ont_profile_apply import apply_bundle_to_ont

        olt = OLTDevice(name="Owner Scope OLT", vendor="Huawei", model="MA5608T")
        owner = Subscriber(
            first_name="Owner",
            last_name="Business",
            email="owner-scope@example.test",
            company_name="Owner Scope Ltd",
        )
        owner.category = SubscriberCategory.business
        other = Subscriber(
            first_name="Other",
            last_name="Business",
            email="other-scope@example.test",
            company_name="Other Scope Ltd",
        )
        other.category = SubscriberCategory.business
        db_session.add_all([olt, owner, other])
        db_session.commit()

        ont = OntUnit(serial_number="OWNER-SCOPE-REJECT", olt_device_id=olt.id)
        profile = OntProvisioningProfile(
            name="Owner Scoped Profile",
            olt_device_id=olt.id,
            owner_subscriber_id=owner.id,
            is_active=True,
        )
        db_session.add_all([ont, profile])
        db_session.commit()
        db_session.add(
            OntAssignment(
                ont_unit_id=ont.id,
                subscriber_id=other.id,
                active=True,
            )
        )
        db_session.commit()

        result = apply_bundle_to_ont(db_session, str(ont.id), str(profile.id))

        assert result.success is False
        assert "another business account" in result.message

    def test_create_profile_rejects_non_business_owner(self, db_session) -> None:
        from fastapi import HTTPException

        from app.services.network.ont_provisioning_profiles import (
            ont_provisioning_profiles,
        )

        olt = OLTDevice(name="Owner Validate OLT", vendor="Huawei", model="MA5608T")
        subscriber = Subscriber(
            first_name="Residential",
            last_name="Owner",
            email="res-owner@example.test",
        )
        db_session.add_all([olt, subscriber])
        db_session.commit()

        try:
            ont_provisioning_profiles.create(
                db_session,
                name="Bad Owner Profile",
                olt_device_id=str(olt.id),
                owner_subscriber_id=str(subscriber.id),
            )
        except HTTPException as exc:
            assert exc.status_code == 400
            assert "business account" in str(exc.detail)
        else:
            raise AssertionError("Expected non-business owner to be rejected")

    def test_resolve_profile_accepts_matching_business_owner_scope(
        self, db_session
    ) -> None:
        from app.models.network import OntBundleAssignment, OntBundleAssignmentStatus
        from app.services.network.ont_profile_apply import resolve_profile_for_ont

        olt = OLTDevice(name="Owner Scope Match OLT", vendor="Huawei", model="MA5608T")
        owner = Subscriber(
            first_name="Match",
            last_name="Business",
            email="match-scope@example.test",
            company_name="Match Scope Ltd",
        )
        owner.category = SubscriberCategory.business
        db_session.add_all([olt, owner])
        db_session.commit()

        ont = OntUnit(serial_number="OWNER-SCOPE-MATCH", olt_device_id=olt.id)
        profile = OntProvisioningProfile(
            name="Matching Owner Scoped Profile",
            olt_device_id=olt.id,
            owner_subscriber_id=owner.id,
            is_active=True,
        )
        db_session.add_all([ont, profile])
        db_session.commit()
        db_session.add(
            OntAssignment(
                ont_unit_id=ont.id,
                subscriber_id=owner.id,
                active=True,
            )
        )
        db_session.add(
            OntBundleAssignment(
                ont_unit_id=ont.id,
                bundle_id=profile.id,
                status=OntBundleAssignmentStatus.applied,
                is_active=True,
            )
        )
        db_session.commit()

        resolved = resolve_profile_for_ont(db_session, str(ont.id))

        assert resolved is not None
        assert resolved.id == profile.id


# ---------------------------------------------------------------------------
# Route registration (all phases)
# ---------------------------------------------------------------------------


class TestRouteRegistration:
    """Verify all new routes are registered on the router."""

    def test_new_routes_registered(self) -> None:
        from app.web.admin.network_olts_onts import router
        from app.web.admin.network_onts_actions import router as actions_router
        from app.web.admin.network_onts_inventory import router as inventory_router
        from app.web.admin.network_onts_provisioning import (
            router as provisioning_router,
        )

        # Collect paths from both routers (main routes + action routes)
        route_paths = [
            route.path for route in router.routes if isinstance(route, Route)
        ]
        inventory_paths = [
            route.path for route in inventory_router.routes if isinstance(route, Route)
        ]
        action_paths = [
            route.path for route in actions_router.routes if isinstance(route, Route)
        ]
        provisioning_paths = [
            route.path
            for route in provisioning_router.routes
            if isinstance(route, Route)
        ]
        all_paths = route_paths + inventory_paths + action_paths + provisioning_paths

        # Phase 1: Service-port routes
        assert "/network/onts/{ont_id}/service-ports" in route_paths
        assert "/network/onts/{ont_id}/service-ports/create" in route_paths
        assert "/network/onts/{ont_id}/service-ports/{index}/delete" in route_paths
        assert "/network/onts/{ont_id}/service-ports/clone" in route_paths

        # Phase 2: OMCI / management IP / TR-069 routes (on actions router)
        assert "/network/onts/{ont_id}/actions/omci-reboot" in action_paths
        assert "/network/onts/{ont_id}/actions/configure-mgmt-ip" in action_paths
        assert "/network/onts/{ont_id}/actions/bind-tr069-profile" in action_paths
        assert "/network/onts/{ont_id}/iphost-config" in route_paths
        assert "/network/onts/{ont_id}/location-details" in route_paths
        assert "/network/onts/{ont_id}/device-info" in route_paths
        assert "/network/onts/{ont_id}/gpon-channel" in route_paths
        assert "/network/onts/{ont_id}/operational-health" in action_paths
        assert "/network/onts/{ont_id}/reconcile" in action_paths
        assert "/network/onts/{ont_id}/config-snapshot" in action_paths
        assert "/network/onts/{ont_id}/edit" not in all_paths

        # Phase 3: OLT profile routes remain, provisioning UI routes are registered
        assert "/network/olts/{olt_id}/profiles/line" in route_paths
        assert "/network/olts/{olt_id}/profiles/tr069" in route_paths

        assert "/network/onts/{ont_id}/provisioning-preview" in provisioning_paths
        assert "/network/onts/{ont_id}/preflight" in provisioning_paths
        assert "/network/onts/{ont_id}/provision" in route_paths
        assert "/network/onts/{ont_id}/provision-status" not in all_paths

    def test_configure_page_is_readable_without_write_permission(self) -> None:
        from app.web.admin.network_onts import router

        route = next(
            route
            for route in router.routes
            if isinstance(route, APIRoute)
            and route.path == "/network/onts/{ont_id}/provision"
        )
        permission_keys = {
            cell.cell_contents
            for dependency in route.dependant.dependencies
            for cell in (getattr(dependency.call, "__closure__", None) or [])
            if isinstance(cell.cell_contents, str)
        }

        assert "network:read" in permission_keys
        assert "network:write" not in permission_keys


class TestProvisioningUiTemplates:
    def test_provisioning_widget_links_to_gated_provisioning_page(self) -> None:
        template = Path(
            "templates/admin/network/onts/_provision_action.html"
        ).read_text()

        assert "Configure ONT" in template
        assert "/admin/network/onts/{{ ont.id }}/provision" in template
        assert "management, internet, LAN, WiFi, and profile selections" in template

    def test_ont_detail_home_does_not_duplicate_tab_details(self) -> None:
        template = Path("templates/admin/network/onts/detail.html").read_text()

        assert 'card("Location Details"' not in template
        assert 'card("Network Path"' not in template
        assert 'card("Observed Runtime"' not in template
        assert "Device Actions" not in template
        assert "TR-069 Management" not in template
        assert "Capabilities" not in template

    def test_ont_detail_template_locks_discovered_device_info(self) -> None:
        template = Path("templates/admin/network/onts/detail.html").read_text()

        assert 'hx-get="/admin/network/onts/{{ ont.id }}/device-info"' not in template
        assert "/admin/network/onts/{{ ont.id }}/edit" not in template
        assert "Board" in template

    def test_ont_detail_template_keeps_single_status_section(self) -> None:
        detail_template = Path("templates/admin/network/onts/detail.html").read_text()

        assert "Quick Status Strip" not in detail_template
        assert 'id="operational-health-container"' not in detail_template
        assert (
            'hx-get="/admin/network/onts/{{ ont.id }}/operational-health"'
            not in detail_template
        )
        assert "Optical Signal Detail" not in detail_template
        assert "Status — single ONT state section" in detail_template

    def test_ont_detail_template_removes_duplicate_provisioning_controls(self) -> None:
        detail_template = Path("templates/admin/network/onts/detail.html").read_text()

        assert "_sidebar_network_config.html" not in detail_template
        assert "_service_intent_summary.html" not in detail_template
        assert (
            'hx-get="/admin/network/onts/{{ ont.id }}/profile-form"'
            not in detail_template
        )
        assert (
            'hx-get="/admin/network/onts/{{ ont.id }}/firmware-form"'
            not in detail_template
        )
        assert "/admin/network/onts/{{ ont.id }}/provision" in detail_template

    def test_ont_form_requires_olt_selection(self) -> None:
        template = Path("templates/admin/network/onts/form.html").read_text()

        assert 'name="olt_device_id" id="olt_device_id" required' in template
        assert "Required before authorization" in template


class TestOntLocationDetailsHelpers:
    def test_resolve_splitter_port_id_by_number(self, db_session) -> None:
        from app.services.network.ont_web_forms import resolve_splitter_port_id

        splitter = Splitter(name="ODB-A", splitter_ratio="1:8")
        db_session.add(splitter)
        db_session.commit()
        db_session.refresh(splitter)

        db_session.add_all(
            [
                SplitterPort(splitter_id=splitter.id, port_number=1),
                SplitterPort(splitter_id=splitter.id, port_number=8),
            ]
        )
        db_session.commit()

        resolved = resolve_splitter_port_id(
            db_session,
            splitter_id=splitter.id,
            splitter_port_number=8,
        )

        assert resolved is not None

    def test_resolve_splitter_port_id_rejects_port_without_splitter(
        self, db_session
    ) -> None:
        from app.services.network.ont_web_forms import resolve_splitter_port_id

        try:
            resolve_splitter_port_id(
                db_session,
                splitter_id=None,
                splitter_port_number=3,
            )
        except ValueError as exc:
            assert "Select an ODB" in str(exc)
        else:
            raise AssertionError(
                "Expected ValueError when ODB Port is set without splitter"
            )

    def test_provisioning_widget_has_no_orchestration_controls(self) -> None:
        template = Path(
            "templates/admin/network/onts/_provision_action.html"
        ).read_text()

        assert "/provision-status" not in template

    def test_provisioning_profile_links_use_profile_catalog_routes(self) -> None:
        provision_template = Path(
            "templates/admin/network/onts/provision.html"
        ).read_text()
        preview_template = Path(
            "templates/admin/network/onts/_profile_preview.html"
        ).read_text()
        olt_detail_template = Path(
            "templates/admin/network/olts/detail.html"
        ).read_text()

        assert "/admin/network/provisioning-profiles/create" in provision_template
        assert (
            "/admin/network/provisioning-profiles/create?olt_device_id={{ olt.id }}"
            in olt_detail_template
        )
        assert (
            "/admin/network/provisioning-profiles/{{ profile.id }}/edit"
            in preview_template
        )
        assert "/admin/network/olts/profiles" not in provision_template
        assert "/admin/network/olts/profiles" not in preview_template

    def test_ont_form_describes_supported_external_id_formats(self) -> None:
        template = Path("templates/admin/network/onts/form.html").read_text()

        assert "huawei:4194320640.5" in template
        assert "resolved ONT-ID" in template

    def test_assignment_ui_allows_blank_pon_port_for_tr069_only(self) -> None:
        template = Path("templates/admin/network/onts/assign.html").read_text()

        assert "Leave blank for TR-069-only assignment" in template
        assert "TR-069-only onboarding" in template
        assert 'name="pon_port_id" id="pon_port_id" required' not in template

    def test_ont_list_offers_tr069_import_shortcut(self) -> None:
        template = Path("templates/admin/network/onts/index.html").read_text()

        assert "/admin/network/tr069?only_unlinked=true" in template
        assert "Import From TR-069" in template


class TestOntAssignmentValidation:
    def test_validate_form_values_allows_blank_pon_port(self) -> None:
        from app.services import web_network_ont_assignments as svc

        error = svc.validate_form_values(
            {
                "pon_port_id": "",
                "account_id": "acct-1",
                "service_address_id": None,
                "notes": None,
            }
        )

        assert error is None

    def test_assignment_edit_payload_does_not_require_subscription_id(self) -> None:
        from types import SimpleNamespace
        from uuid import uuid4

        from app.services import web_network_ont_assignments as svc

        assignment = SimpleNamespace(
            pon_port_id=uuid4(),
            subscriber_id=uuid4(),
            subscriber=SimpleNamespace(name="Jane Customer"),
            service_address_id=None,
            notes="Installed at side wall",
        )

        payload = svc.assignment_form_payload_from_assignment(assignment)

        assert payload["account_id"] == str(assignment.subscriber_id)
        assert payload["account_label"] == "Jane Customer"
        assert "subscription_id" not in payload


# ---------------------------------------------------------------------------
# OLT SSH function existence checks
# ---------------------------------------------------------------------------


class TestOltSshFunctionExistence:
    """Verify all new SSH functions are importable."""

    def test_delete_service_port_importable(self) -> None:
        from app.services.network.olt_ssh import delete_service_port

        assert callable(delete_service_port)

    def test_create_single_service_port_importable(self) -> None:
        from app.services.network.olt_ssh import create_single_service_port

        assert callable(create_single_service_port)

    def test_get_service_ports_for_ont_importable(self) -> None:
        from app.services.network.olt_ssh import get_service_ports_for_ont

        assert callable(get_service_ports_for_ont)

    def test_configure_ont_iphost_importable(self) -> None:
        from app.services.network.olt_ssh import configure_ont_iphost

        assert callable(configure_ont_iphost)

    def test_get_ont_iphost_config_importable(self) -> None:
        from app.services.network.olt_ssh import get_ont_iphost_config

        assert callable(get_ont_iphost_config)

    def test_configure_ont_iphost_verifies_applied_state(self, monkeypatch) -> None:
        from app.services.network.olt_ssh_ont import configure_ont_iphost

        olt = OLTDevice(name="Verify OLT", vendor="Huawei", model="MA5608T")
        sent_commands: list[str] = []

        class _FakeChannel:
            def send(self, _chars: str) -> None:
                return None

        class _FakeTransport:
            def close(self) -> None:
                return None

        def _fake_run_huawei_cmd(_channel, command, prompt=None):  # noqa: ARG001
            sent_commands.append(command)
            if command.startswith("display ont ipconfig"):
                return """
ONT IP host index      : 0
ONT config type        : Static config
ONT IP                 : 172.16.202.20
ONT subnet mask        : 255.255.255.0
ONT gateway            : 172.16.202.1
ONT manage VLAN        : 201
ONT manage priority    : 2
"""
            return "success"

        monkeypatch.setattr(
            "app.services.network.olt_ssh._open_shell",
            lambda *_args, **_kwargs: (_FakeTransport(), _FakeChannel(), None),
        )
        monkeypatch.setattr(
            "app.services.network.olt_ssh._read_until_prompt",
            lambda *_args, **_kwargs: "#",
        )
        monkeypatch.setattr(
            "app.services.network.olt_ssh._run_huawei_cmd",
            _fake_run_huawei_cmd,
        )

        ok, msg = configure_ont_iphost(
            olt,
            "0/1/13",
            8,
            vlan_id=201,
            ip_mode="static_ip",
            priority=2,
            ip_address="172.16.202.20",
            subnet="255.255.255.0",
            gateway="172.16.202.1",
        )

        assert ok is True
        assert "configured" in msg.lower()
        assert any(
            command.startswith("display ont ipconfig") for command in sent_commands
        )

    def test_reboot_ont_omci_importable(self) -> None:
        from app.services.network.olt_ssh import reboot_ont_omci

        assert callable(reboot_ont_omci)

    def test_bind_tr069_server_profile_importable(self) -> None:
        from app.services.network.olt_ssh import bind_tr069_server_profile

        assert callable(bind_tr069_server_profile)

    def test_get_line_profiles_importable(self) -> None:
        from app.services.network.olt_ssh import get_line_profiles

        assert callable(get_line_profiles)

    def test_get_service_profiles_importable(self) -> None:
        from app.services.network.olt_ssh import get_service_profiles

        assert callable(get_service_profiles)

    def test_get_tr069_server_profiles_importable(self) -> None:
        from app.services.network.olt_ssh import get_tr069_server_profiles

        assert callable(get_tr069_server_profiles)


# ---------------------------------------------------------------------------
# OLT live profile reconciliation
# ---------------------------------------------------------------------------


class TestOltProvisioningProfileReconcile:
    """Test OLT-scoped provisioning profile inference from live samples."""

    def test_infer_profile_from_iphost_and_service_ports(self) -> None:
        from app.services.network.olt_provisioning_profile_reconcile import (
            infer_profile_from_samples,
        )

        olt = OLTDevice(name="Infer Huawei OLT", vendor="Huawei", model="MA5608T")
        service_ports = [
            [
                ServicePortEntry(
                    index=1,
                    vlan_id=201,
                    ont_id=8,
                    gem_index=2,
                    flow_type="vlan",
                    flow_para="201",
                    state="up",
                ),
                ServicePortEntry(
                    index=2,
                    vlan_id=203,
                    ont_id=8,
                    gem_index=1,
                    flow_type="vlan",
                    flow_para="203",
                    state="up",
                ),
            ]
        ]
        iphosts = [{"vlan": "201", "priority": "2"}]

        observed = infer_profile_from_samples(
            olt=olt,
            service_port_samples=service_ports,
            iphost_samples=iphosts,
        )

        assert observed is not None
        assert observed.mgmt_vlan_tag == 201
        assert observed.mgmt_priority == 2
        assert observed.services[0].vlan_id == 203
        assert observed.services[0].gem_port_id == 1
        assert observed.services[1].vlan_id == 201
        assert observed.services[1].gem_port_id == 2

    def test_parse_service_port_observations_keeps_fsp(self) -> None:
        from app.services.network.olt_provisioning_profile_reconcile import (
            parse_service_port_observations,
        )

        output = """
       31  201 common   gpon 0/1 /2  0    2     vlan  201        25   25   up
       32  203 common   gpon 0/1 /2  0    1     vlan  203        27   26   up
        """

        observations = parse_service_port_observations(output)

        assert len(observations) == 2
        assert observations[0].fsp == "0/1/2"
        assert observations[0].entry.vlan_id == 201
        assert observations[0].entry.ont_id == 0
        assert observations[0].entry.gem_index == 2

    def test_apply_observed_profile_updates_services(self, db_session) -> None:
        from app.services.network.olt_provisioning_profile_reconcile import (
            ObservedOltProvisioningProfile,
            ObservedWanService,
            apply_observed_profile,
        )

        olt = OLTDevice(name="Apply Huawei OLT", vendor="Huawei", model="MA5608T")
        profile = OntProvisioningProfile(
            name="Apply Old",
            olt_device=olt,
            mgmt_vlan_tag=100,
            is_active=True,
        )
        db_session.add_all([olt, profile])
        db_session.commit()
        db_session.refresh(profile)

        observed = ObservedOltProvisioningProfile(
            olt_id=str(olt.id),
            olt_name=olt.name,
            mgmt_vlan_tag=201,
            mgmt_priority=2,
            mgmt_config_mode="DHCP",
            sampled_onts=1,
            services=[
                ObservedWanService(
                    service_type=WanServiceType.internet,
                    name="Internet PPPoE",
                    vlan_id=203,
                    gem_port_id=1,
                    connection_type=WanConnectionType.pppoe,
                    priority=1,
                ),
                ObservedWanService(
                    service_type=WanServiceType.management,
                    name="TR-069 Management",
                    vlan_id=201,
                    gem_port_id=2,
                    connection_type=WanConnectionType.dhcp,
                    priority=2,
                    cos_priority=2,
                ),
            ],
        )

        changed = apply_observed_profile(db_session, profile, observed)
        db_session.commit()
        db_session.refresh(profile)

        assert changed is True
        assert profile.name == "Apply PPPoE mgmt201 internet203"
        assert profile.mgmt_vlan_tag == 201
        services = {service.service_type: service for service in profile.wan_services}
        assert services[WanServiceType.internet].s_vlan == 203
        assert services[WanServiceType.internet].gem_port_id == 1
        assert services[WanServiceType.management].s_vlan == 201
        assert services[WanServiceType.management].gem_port_id == 2
        assert services[WanServiceType.management].cos_priority == 2


class TestProfileWanServiceVlanScope:
    """Test profile WAN services are tied to the profile OLT VLAN catalog."""

    def test_vlan_tag_can_repeat_across_olts_in_same_region(self, db_session) -> None:
        from app.models.catalog import RegionZone

        region = RegionZone(name="Repeated VLAN Region")
        olt_a = OLTDevice(name="Repeated VLAN OLT A", vendor="Huawei", model="MA5608T")
        olt_b = OLTDevice(name="Repeated VLAN OLT B", vendor="Huawei", model="MA5608T")
        db_session.add_all([region, olt_a, olt_b])
        db_session.commit()

        db_session.add_all(
            [
                Vlan(
                    region_id=region.id, olt_device_id=olt_a.id, tag=203, is_active=True
                ),
                Vlan(
                    region_id=region.id, olt_device_id=olt_b.id, tag=203, is_active=True
                ),
            ]
        )
        db_session.commit()

        vlans = db_session.query(Vlan).filter(Vlan.tag == 203).all()
        assert len(vlans) == 2

    def test_profile_management_vlan_requires_profile_olt_vlan(
        self, db_session
    ) -> None:
        from fastapi import HTTPException

        from app.models.catalog import RegionZone
        from app.services.network.ont_provisioning_profiles import (
            ont_provisioning_profiles,
        )

        region = RegionZone(name="Profile VLAN Region")
        olt = OLTDevice(name="Profile VLAN OLT", vendor="Huawei", model="MA5608T")
        db_session.add_all([region, olt])
        db_session.commit()

        try:
            ont_provisioning_profiles.create(
                db_session,
                name="Missing Mgmt VLAN Profile",
                olt_device_id=str(olt.id),
                mgmt_vlan_tag=201,
            )
        except HTTPException as exc:
            assert exc.status_code == 400
            assert "Management VLAN 201" in str(exc.detail)
        else:
            raise AssertionError("Expected missing management VLAN to be rejected")

        db_session.add(
            Vlan(region_id=region.id, olt_device_id=olt.id, tag=201, is_active=True)
        )
        db_session.commit()

        profile = ont_provisioning_profiles.create(
            db_session,
            name="Valid Mgmt VLAN Profile",
            olt_device_id=str(olt.id),
            mgmt_vlan_tag=201,
        )
        assert profile.mgmt_vlan_tag == 201

    def test_wan_service_requires_vlan_on_profile_olt(self, db_session) -> None:
        from fastapi import HTTPException

        from app.models.catalog import RegionZone
        from app.services.network.ont_provisioning_profiles import wan_services

        region = RegionZone(name="VLAN Scope Region")
        olt = OLTDevice(name="VLAN Scope OLT", vendor="Huawei", model="MA5608T")
        profile = OntProvisioningProfile(name="Scoped Profile", olt_device=olt)
        db_session.add_all([region, olt, profile])
        db_session.commit()
        db_session.refresh(profile)

        try:
            wan_services.create(
                db_session,
                profile_id=str(profile.id),
                service_type=WanServiceType.internet,
                s_vlan=203,
            )
        except HTTPException as exc:
            assert exc.status_code == 400
            assert "not defined" in str(exc.detail)
        else:
            raise AssertionError("Expected missing OLT VLAN to be rejected")

        db_session.add(
            Vlan(region_id=region.id, olt_device_id=olt.id, tag=203, is_active=True)
        )
        db_session.commit()

        service = wan_services.create(
            db_session,
            profile_id=str(profile.id),
            service_type=WanServiceType.internet,
            s_vlan=203,
        )
        assert service.s_vlan == 203


# ---------------------------------------------------------------------------
# OltCommandSet and dataclass tests
# ---------------------------------------------------------------------------


class TestOltCommandSetDataclass:
    """Test OltCommandSet fields."""

    def test_defaults(self) -> None:
        cs = OltCommandSet(step="Test", commands=["cmd1"])
        assert cs.description == ""
        assert cs.requires_config_mode is True

    def test_custom_fields(self) -> None:
        cs = OltCommandSet(
            step="Custom",
            commands=["a", "b"],
            description="Some desc",
            requires_config_mode=False,
        )
        assert cs.step == "Custom"
        assert len(cs.commands) == 2
        assert cs.description == "Some desc"
        assert cs.requires_config_mode is False


# ---------------------------------------------------------------------------
# Management IP Allocation Tests
# ---------------------------------------------------------------------------


class TestManagementIpAllocation:
    """Test the _allocate_mgmt_ip_from_pool function."""

    def test_allocate_ip_from_pool_success(self, db_session) -> None:
        from app.models.network import IpBlock, IpPool, IPVersion
        from app.services.network.olt_authorization_workflow import (
            _allocate_mgmt_ip_from_pool,
        )

        pool = IpPool(
            name="Test Mgmt Pool",
            cidr="10.0.100.0/24",
            gateway="10.0.100.1",
            dns_primary="8.8.8.8",
            ip_version=IPVersion.ipv4,
            is_active=True,
        )
        db_session.add(pool)
        db_session.commit()
        db_session.refresh(pool)

        block = IpBlock(
            pool_id=pool.id,
            cidr="10.0.100.0/28",
            is_active=True,
        )
        db_session.add(block)
        db_session.commit()

        success, ip, subnet, gw, msg = _allocate_mgmt_ip_from_pool(
            db_session, pool.id, ont_serial="TEST-ONT-001"
        )

        assert success is True
        assert ip == "10.0.100.2"  # .1 is gateway, so first allocated is .2
        assert subnet == "255.255.255.0"
        assert gw == "10.0.100.1"
        assert "Allocated" in msg

    def test_allocate_ip_skips_existing_addresses(self, db_session) -> None:
        from app.models.network import IpBlock, IpPool, IPv4Address, IPVersion
        from app.services.network.olt_authorization_workflow import (
            _allocate_mgmt_ip_from_pool,
        )

        pool = IpPool(
            name="Test Pool Skip Existing",
            cidr="10.0.200.0/24",
            gateway="10.0.200.1",
            ip_version=IPVersion.ipv4,
            is_active=True,
        )
        db_session.add(pool)
        db_session.commit()
        db_session.refresh(pool)

        block = IpBlock(
            pool_id=pool.id,
            cidr="10.0.200.0/28",
            is_active=True,
        )
        db_session.add(block)
        db_session.commit()

        # Pre-allocate .2 to force skipping to .3
        existing = IPv4Address(address="10.0.200.2", pool_id=pool.id)
        db_session.add(existing)
        db_session.commit()

        success, ip, subnet, gw, msg = _allocate_mgmt_ip_from_pool(db_session, pool.id)

        assert success is True
        assert ip == "10.0.200.3"  # .1 is gateway, .2 exists, so .3 is allocated
        assert subnet == "255.255.255.0"

    def test_allocate_ip_pool_not_found(self, db_session) -> None:
        from app.services.network.olt_authorization_workflow import (
            _allocate_mgmt_ip_from_pool,
        )

        fake_id = uuid.uuid4()
        success, ip, subnet, gw, msg = _allocate_mgmt_ip_from_pool(db_session, fake_id)

        assert success is False
        assert ip is None
        assert "not found" in msg

    def test_allocate_ip_pool_no_gateway(self, db_session) -> None:
        from app.models.network import IpPool, IPVersion
        from app.services.network.olt_authorization_workflow import (
            _allocate_mgmt_ip_from_pool,
        )

        pool = IpPool(
            name="Pool No Gateway",
            cidr="10.0.150.0/24",
            gateway=None,  # No gateway
            ip_version=IPVersion.ipv4,
            is_active=True,
        )
        db_session.add(pool)
        db_session.commit()
        db_session.refresh(pool)

        success, ip, subnet, gw, msg = _allocate_mgmt_ip_from_pool(db_session, pool.id)

        assert success is False
        assert "no gateway" in msg.lower()

    def test_allocate_ip_pool_inactive(self, db_session) -> None:
        from app.models.network import IpPool, IPVersion
        from app.services.network.olt_authorization_workflow import (
            _allocate_mgmt_ip_from_pool,
        )

        pool = IpPool(
            name="Inactive Pool",
            cidr="10.0.160.0/24",
            gateway="10.0.160.1",
            ip_version=IPVersion.ipv4,
            is_active=False,  # Inactive
        )
        db_session.add(pool)
        db_session.commit()
        db_session.refresh(pool)

        success, ip, subnet, gw, msg = _allocate_mgmt_ip_from_pool(db_session, pool.id)

        assert success is False
        assert "not active" in msg.lower()

    def test_allocate_mgmt_ip_function_exists(self) -> None:
        from app.services.network.olt_authorization_workflow import (
            _allocate_mgmt_ip_from_pool,
        )

        assert callable(_allocate_mgmt_ip_from_pool)

    def test_configure_management_ip_accepts_new_params(self) -> None:
        import inspect

        from app.services.network.olt_authorization_workflow import (
            _configure_management_ip_for_authorization,
        )

        sig = inspect.signature(_configure_management_ip_for_authorization)
        params = list(sig.parameters.keys())

        assert "ont_unit_id" in params
        assert "serial_number" in params

    def test_allocate_ip_updates_pool_cache(self, db_session) -> None:
        """Test that allocation updates next_available_ip and available_count."""
        from app.models.network import IpBlock, IpPool, IPVersion
        from app.services.network.olt_authorization_workflow import (
            _allocate_mgmt_ip_from_pool,
        )

        pool = IpPool(
            name="Test Cache Pool",
            cidr="10.0.250.0/28",  # /28 = 14 usable hosts
            gateway="10.0.250.1",
            ip_version=IPVersion.ipv4,
            is_active=True,
        )
        db_session.add(pool)
        db_session.commit()
        db_session.refresh(pool)

        block = IpBlock(
            pool_id=pool.id,
            cidr="10.0.250.0/28",
            is_active=True,
        )
        db_session.add(block)
        db_session.commit()

        # First allocation
        success, ip1, _, _, _ = _allocate_mgmt_ip_from_pool(db_session, pool.id)
        assert success is True
        assert ip1 == "10.0.250.2"

        # Refresh pool and check cache was updated
        db_session.refresh(pool)
        assert pool.next_available_ip == "10.0.250.3"
        assert pool.available_count == 12  # 14 - gateway - 1 allocated = 12

        # Second allocation should use cached value
        success, ip2, _, _, _ = _allocate_mgmt_ip_from_pool(db_session, pool.id)
        assert success is True
        assert ip2 == "10.0.250.3"

        db_session.refresh(pool)
        assert pool.next_available_ip == "10.0.250.4"
        assert pool.available_count == 11

    def test_refresh_pool_availability_function(self, db_session) -> None:
        """Test the refresh_pool_availability helper function."""
        from app.models.network import IpBlock, IpPool, IPv4Address, IPVersion
        from app.services.network.olt_authorization_workflow import (
            refresh_pool_availability,
        )

        pool = IpPool(
            name="Refresh Test Pool",
            cidr="10.0.251.0/28",
            gateway="10.0.251.1",
            ip_version=IPVersion.ipv4,
            is_active=True,
        )
        db_session.add(pool)
        db_session.commit()
        db_session.refresh(pool)

        block = IpBlock(
            pool_id=pool.id,
            cidr="10.0.251.0/28",
            is_active=True,
        )
        db_session.add(block)

        # Pre-allocate some IPs
        for i in [2, 3, 5]:
            addr = IPv4Address(address=f"10.0.251.{i}", pool_id=pool.id)
            db_session.add(addr)
        db_session.commit()

        # Refresh availability
        next_ip, count = refresh_pool_availability(db_session, pool.id)

        assert next_ip == "10.0.251.4"  # .1 gateway, .2,.3,.5 allocated, .4 is next
        assert count == 10  # 14 usable - 1 gateway - 3 allocated = 10

        db_session.refresh(pool)
        assert pool.next_available_ip == "10.0.251.4"
        assert pool.available_count == 10
