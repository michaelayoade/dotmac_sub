"""Tests for individual ONT provisioning step functions.

Tests the service functions in ``app.services.network.ont_provision_steps``
which are the canonical API for all provisioning operations.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch


class TestResolveOltContext:
    """Test ONT → OLT context resolution."""

    def test_ont_not_found(self, db_session) -> None:
        from app.services.network.ont_provision_steps import resolve_olt_context

        ctx, err = resolve_olt_context(db_session, str(uuid.uuid4()))
        assert ctx is None
        assert "not found" in err.lower()

    def test_no_active_assignment(self, db_session) -> None:
        from app.models.network import OntUnit
        from app.services.network.ont_provision_steps import resolve_olt_context

        ont = OntUnit(serial_number="TEST-CTX-001")
        db_session.add(ont)
        db_session.commit()
        db_session.refresh(ont)

        ctx, err = resolve_olt_context(db_session, str(ont.id))
        assert ctx is None
        assert "assignment" in err.lower()


class TestCreateServicePort:
    """Test create_service_port step."""

    def test_ont_not_found_returns_failure(self, db_session) -> None:
        from app.services.network.ont_provision_steps import create_service_port

        result = create_service_port(db_session, str(uuid.uuid4()), vlan_id=203)
        assert not result.success
        assert result.step_name == "create_service_port"

    def test_success_records_step(self, db_session) -> None:
        from types import SimpleNamespace

        from app.models.catalog import RegionZone
        from app.models.network import OLTDevice, OntAssignment, OntUnit, PonPort, Vlan
        from app.services.network.ont_provision_steps import create_service_port

        olt = OLTDevice(
            name="Test OLT",
            vendor="Huawei",
            model="MA5608T",
            ssh_username="admin",
            ssh_password="test",
        )
        db_session.add(olt)
        db_session.commit()
        db_session.refresh(olt)

        pon = PonPort(olt_id=olt.id, name="0/2/1")
        db_session.add(pon)
        db_session.commit()
        db_session.refresh(pon)

        # Create region + VLAN 203 for the OLT (required by VLAN validation)
        region = RegionZone(name="Test Region")
        db_session.add(region)
        db_session.commit()
        db_session.refresh(region)
        vlan = Vlan(tag=203, region_id=region.id, olt_device_id=olt.id, is_active=True)
        db_session.add(vlan)
        db_session.commit()

        ont = OntUnit(
            serial_number="TEST-SP-001", board="0/2", port="1", external_id="5"
        )
        db_session.add(ont)
        db_session.commit()
        db_session.refresh(ont)

        assignment = OntAssignment(ont_unit_id=ont.id, pon_port_id=pon.id, active=True)
        db_session.add(assignment)
        db_session.commit()

        mock_action = SimpleNamespace(success=True, message="Service-port 100 created")
        with patch(
            "app.services.network.ont_write.OntWriteService.update_service_port",
            return_value=mock_action,
        ):
            result = create_service_port(
                db_session, str(ont.id), vlan_id=203, gem_index=1
            )

        assert result.success
        assert result.step_name == "create_service_port"
        assert result.duration_ms >= 0

        # Step recording is log-based; verify result only
        assert result.message


class TestConfigureManagementIp:
    """Test configure_management_ip step."""

    def test_ont_not_found(self, db_session) -> None:
        from app.services.network.ont_provision_steps import configure_management_ip

        result = configure_management_ip(db_session, str(uuid.uuid4()), vlan_id=450)
        assert not result.success
        assert result.step_name == "configure_management_ip"
        assert not result.critical


class TestBindTr069:
    """Test bind_tr069 step."""

    def test_ont_not_found(self, db_session) -> None:
        from app.services.network.ont_provision_steps import bind_tr069

        result = bind_tr069(db_session, str(uuid.uuid4()), tr069_olt_profile_id=2)
        assert not result.success
        assert result.step_name == "bind_tr069"


class TestValidatePrerequisites:
    """Test preflight gating rules."""

    def test_acs_enabled_ont_does_not_require_manual_tr069_profile_id(
        self, db_session
    ) -> None:
        from app.models.network import (
            OLTDevice,
            OntAssignment,
            OntAuthorizationStatus,
            OntProvisioningProfile,
            OntUnit,
            PonPort,
        )
        from app.models.tr069 import Tr069AcsServer
        from app.services.network.ont_provision_steps import validate_prerequisites

        # OntAssignment.subscriber_id is optional; no subscriber needed.
        acs = Tr069AcsServer(
            name="DotMac ACS",
            base_url="http://genieacs:7557",
            cwmp_url="http://acs.example/cwmp",
            is_active=True,
        )
        db_session.add(acs)
        db_session.flush()

        olt = OLTDevice(
            name="ACS OLT",
            vendor="Huawei",
            model="MA5800-X2",
            ssh_username="admin",
            ssh_password="secret",
            tr069_acs_server_id=acs.id,
        )
        profile = OntProvisioningProfile(
            name="TR069 Profile",
            cr_username="cwmp",
            cr_password="secret",
            is_active=True,
            authorization_line_profile_id=10,
            authorization_service_profile_id=11,
        )
        db_session.add_all([olt, profile])
        db_session.flush()

        pon = PonPort(olt_id=olt.id, name="0/2/1")
        db_session.add(pon)
        db_session.flush()

        ont = OntUnit(
            serial_number="HWTC-PREFLIGHT",
            olt_device_id=olt.id,
            board="0/2",
            port="1",
            external_id="huawei:4194323968.1",
            provisioning_profile_id=profile.id,
            authorization_status=OntAuthorizationStatus.authorized,
            is_active=True,
        )
        db_session.add(ont)
        db_session.flush()
        db_session.add(
            OntAssignment(
                ont_unit_id=ont.id,
                pon_port_id=pon.id,
                active=True,
            )
        )
        db_session.commit()

        result = validate_prerequisites(db_session, str(ont.id))

        tr069_check = next(
            check for check in result["checks"] if check["name"] == "TR-069 OLT profile"
        )
        assert tr069_check["status"] == "ok"
        assert "dynamically" in tr069_check["message"]
        assert "PPPoE credential" not in {check["name"] for check in result["checks"]}
        assert result["ready"] is True


class TestWaitTr069Bootstrap:
    """Test bootstrap polling dispatch."""

    def test_queue_wait_tr069_bootstrap_dispatches_background_task(
        self, db_session
    ) -> None:
        from app.services.network.ont_provision_steps import queue_wait_tr069_bootstrap

        def _assert_committed(*args, **kwargs):
            assert db_session.in_transaction() is False
            return None

        with (
            patch(
                "app.services.network_operations.network_operations.start",
                return_value=SimpleNamespace(id="op-1"),
            ) as start,
            patch(
                "app.services.network_operations.network_operations.mark_waiting",
            ) as mark_waiting,
            patch(
                "app.celery_app.enqueue_celery_task",
                side_effect=_assert_committed,
            ) as enqueue_task,
        ):
            result = queue_wait_tr069_bootstrap(
                db_session,
                "ont-1",
            )

        assert result.success is False
        assert result.waiting is True
        assert result.step_name == "wait_tr069_bootstrap"
        start.assert_called_once()
        mark_waiting.assert_called_once_with(
            db_session,
            "op-1",
            "Waiting for background TR-069 bootstrap polling to start.",
        )
        enqueue_task.assert_called_once_with(
            "app.tasks.tr069.wait_for_ont_bootstrap",
            args=["ont-1", "op-1"],
            correlation_id="tr069_bootstrap:ont-1",
            source="ont_provision_step",
        )

    def test_queue_wait_tr069_bootstrap_marks_operation_failed_when_dispatch_fails(
        self, db_session
    ) -> None:
        from app.services.network.ont_provision_steps import queue_wait_tr069_bootstrap

        with (
            patch(
                "app.services.network_operations.network_operations.start",
                return_value=SimpleNamespace(id="op-1"),
            ),
            patch(
                "app.services.network_operations.network_operations.mark_waiting",
            ),
            patch(
                "app.services.network_operations.network_operations.mark_failed",
            ) as mark_failed,
            patch(
                "app.celery_app.enqueue_celery_task",
                side_effect=RuntimeError("broker down"),
            ),
        ):
            result = queue_wait_tr069_bootstrap(
                db_session,
                "ont-1",
            )

        assert result.success is False
        assert result.waiting is False
        assert "Failed to queue TR-069 bootstrap polling" in result.message
        mark_failed.assert_called_once()


class TestApplySavedServiceConfig:
    """Test deferred TR-069 service config behavior."""

    def test_missing_pppoe_password_is_needs_input_not_failure(
        self, db_session
    ) -> None:
        from app.models.network import OntUnit
        from app.services.network.ont_provision_steps import apply_saved_service_config

        ont = OntUnit(serial_number="TEST-SVC-NEEDS-INPUT", pppoe_username="cust")
        db_session.add(ont)
        db_session.commit()
        db_session.refresh(ont)

        # Mock capability probing to return a capable device (has PPP WAN)
        mock_cap_result = SimpleNamespace(
            success=True,
            message="Capabilities probed",
            data={
                "data_model": "InternetGatewayDevice",
                "has_ppp_wan": True,
                "supports_tr069_set_ppp_credentials": True,
            },
        )
        with (
            patch(
                "app.services.network.ont_service_intent.load_ont_plan_for_ont",
                return_value={"push_pppoe_tr069": {"username": "cust"}},
            ),
            patch(
                "app.services.network.ont_action_network.probe_wan_capabilities",
                return_value=mock_cap_result,
            ),
        ):
            result = apply_saved_service_config(db_session, str(ont.id))

        assert result.success is True
        assert result.step_name == "apply_saved_service_config"
        assert result.data is not None
        assert result.data["needs_input"] == [
            "PPPoE username and password are required."
        ]
        # Steps now includes capability probing
        assert any(
            s["step"] == "probe_wan_capabilities" and s["success"]
            for s in result.data["steps"]
        )


class TestStaticManagementIpReservation:
    """Test static management IP reservation before reconciled provisioning."""

    def test_reserves_first_available_pool_address_and_updates_cache(
        self, db_session
    ) -> None:
        from app.models.network import (
            IpPool,
            IPVersion,
            MgmtIpMode,
            OntProvisioningProfile,
            OntUnit,
        )
        from app.services.network.ont_provision_steps import (
            _ensure_static_management_ip_from_profile,
        )

        pool = IpPool(
            name=f"mgmt-{uuid.uuid4().hex[:8]}",
            ip_version=IPVersion.ipv4,
            cidr="192.0.2.0/29",
            gateway="192.0.2.1",
            is_active=True,
        )
        db_session.add(pool)
        db_session.flush()
        profile = OntProvisioningProfile(
            name=f"static-mgmt-{uuid.uuid4().hex[:8]}",
            mgmt_ip_mode=MgmtIpMode.static_ip,
            mgmt_ip_pool_id=pool.id,
        )
        ont = OntUnit(serial_number="TEST-STATIC-MGMT")
        db_session.add_all([profile, ont])
        db_session.commit()
        db_session.refresh(pool)
        db_session.refresh(profile)
        db_session.refresh(ont)

        ok, message = _ensure_static_management_ip_from_profile(
            db_session, ont, profile
        )
        db_session.commit()

        assert ok is True
        assert "Reserved static management IP 192.0.2.2" in message
        assert ont.mgmt_ip_address == "192.0.2.2"
        assert pool.next_available_ip == "192.0.2.3"
        assert pool.available_count == 4


class TestPushPppoeOmci:
    """Test push_pppoe_omci step."""

    def test_ont_not_found(self, db_session) -> None:
        from app.services.network.ont_provision_steps import push_pppoe_omci

        result = push_pppoe_omci(
            db_session,
            str(uuid.uuid4()),
            vlan_id=203,
            username="test",
            password="pass",
        )
        assert not result.success
        assert result.step_name == "push_pppoe_omci"


class TestPushPppoeTr069:
    """Test push_pppoe_tr069 step."""

    def test_delegates_to_ont_action(self, db_session) -> None:
        from types import SimpleNamespace

        from app.services.network.ont_provision_steps import push_pppoe_tr069

        mock_result = SimpleNamespace(success=True, message="PPPoE set OK")

        with patch(
            "app.services.network.ont_action_network.set_pppoe_credentials",
            return_value=mock_result,
        ):
            result = push_pppoe_tr069(
                db_session,
                str(uuid.uuid4()),
                username="test",
                password="pass",
                retry=False,
            )

        assert result.success
        assert result.step_name == "push_pppoe_tr069"

    def test_waiting_result_preserves_task_data_and_pending_step_state(
        self, db_session
    ) -> None:
        from app.models.network import OntUnit
        from app.services.network.ont_provision_steps import push_pppoe_tr069

        ont = OntUnit(serial_number="TEST-PPPOE-WAIT")
        db_session.add(ont)
        db_session.commit()
        db_session.refresh(ont)

        mock_result = SimpleNamespace(
            success=False,
            waiting=True,
            message="Queued PPPoE push.",
            data={"task_id": "task-123", "delivery": "queued"},
        )

        with patch(
            "app.services.network.ont_action_network.set_pppoe_credentials",
            return_value=mock_result,
        ):
            result = push_pppoe_tr069(
                db_session,
                str(ont.id),
                username="test",
                password="pass",
                retry=False,
            )

        assert result.waiting is True
        assert result.data == {"task_id": "task-123", "delivery": "queued"}


class TestConfigureWanTr069:
    """Test configure_wan_tr069 step."""

    def test_delegates_to_ont_action(self, db_session) -> None:
        from app.services.network.ont_provision_steps import configure_wan_tr069

        mock_result = SimpleNamespace(
            success=True,
            waiting=False,
            message="WAN config updated",
            data={"queued": True},
        )
        ont_id = str(uuid.uuid4())

        with patch(
            "app.services.network.ont_action_network.configure_wan_config",
            return_value=mock_result,
        ) as configure_wan_config:
            result = configure_wan_tr069(
                db_session,
                ont_id,
                wan_mode="static",
                wan_vlan="203",
                ip_address="172.16.203.50",
                subnet_mask="255.255.255.0",
                gateway="172.16.203.1",
                dns_servers="8.8.8.8,1.1.1.1",
                instance_index=2,
            )

        assert result.success is True
        assert result.step_name == "configure_wan_tr069"
        assert result.critical is False
        assert result.data == {"queued": True}
        configure_wan_config.assert_called_once_with(
            db_session,
            ont_id,
            wan_mode="static",
            wan_vlan=203,
            ip_address="172.16.203.50",
            subnet_mask="255.255.255.0",
            gateway="172.16.203.1",
            dns_servers="8.8.8.8,1.1.1.1",
            instance_index=2,
        )


class TestRollbackServicePorts:
    """Test rollback_service_ports step."""

    def test_ont_not_found(self, db_session) -> None:
        from app.services.network.ont_provision_steps import rollback_service_ports

        result = rollback_service_ports(db_session, str(uuid.uuid4()))
        assert not result.success
        assert result.step_name == "rollback_service_ports"


class TestStepResult:
    """Test StepResult dataclass."""

    @dataclass
    class _Meta:
        code: str
        size: int

    def test_default_values(self) -> None:
        from app.services.network.ont_provision_steps import StepResult

        r = StepResult(step_name="test", success=True, message="ok")
        assert r.duration_ms == 0
        assert r.critical is True
        assert r.skipped is False
        assert r.data is None

    def test_data_is_normalized_to_json_safe_values(self) -> None:
        from app.services.network.ont_provision_steps import StepResult

        when = datetime(2026, 3, 28, 12, 0, tzinfo=UTC)
        r = StepResult(
            step_name="test",
            success=False,
            message="waiting",
            waiting=True,
            data={
                "when": when,
                "items": {2, 1},
                "meta": self._Meta(code="x", size=3),
                "price": Decimal("12.50"),
                "ref": uuid.UUID("12345678-1234-5678-1234-567812345678"),
                "raw": SimpleNamespace(id="x"),
            },
        )

        assert r.data == {
            "items": [1, 2],
            "meta": {"code": "x", "size": 3},
            "price": "12.50",
            "raw": "namespace(id='x')",
            "ref": "12345678-1234-5678-1234-567812345678",
            "when": when.isoformat(),
        }
        assert list(r.data.keys()) == ["items", "meta", "price", "raw", "ref", "when"]


class TestMaskCredentials:
    """Test credential masking for safe logging."""

    def test_masks_password(self) -> None:
        from app.services.network.ont_provision_steps import mask_credentials

        cmd = "ont ipconfig 0/2/1 5 pppoe vlan 203 user test password secret123"
        masked = mask_credentials(cmd)
        assert "secret123" not in masked
        assert "********" in masked
        assert "user test" in masked

    def test_masks_common_credential_forms(self) -> None:
        from app.services.network.ont_provision_steps import mask_credentials

        cmd = (
            "wifi psk='wifi secret' api_key=abc123 "
            "snmp community public token:bearer123"
        )
        masked = mask_credentials(cmd)

        assert "wifi secret" not in masked
        assert "abc123" not in masked
        assert "public" not in masked
        assert "bearer123" not in masked
        assert masked.count("********") == 4


class TestWebRouteRegistration:
    """Verify per-step routes are registered."""

    def test_step_routes_registered(self) -> None:
        from starlette.routing import Route

        from app.web.admin.network_onts_provisioning import router

        route_paths = [
            route.path for route in router.routes if isinstance(route, Route)
        ]

        assert "/network/onts/{ont_id}/step/create-service-port" in route_paths
        assert "/network/onts/{ont_id}/step/configure-mgmt-ip" in route_paths
        assert "/network/onts/{ont_id}/step/activate-internet-config" in route_paths
        assert "/network/onts/{ont_id}/step/configure-wan-olt" in route_paths
        assert "/network/onts/{ont_id}/step/bind-tr069" in route_paths
        assert "/network/onts/{ont_id}/step/set-cr-credentials" in route_paths
        assert "/network/onts/{ont_id}/step/push-pppoe-omci" in route_paths
        assert "/network/onts/{ont_id}/step/push-pppoe-tr069" in route_paths
        assert "/network/onts/{ont_id}/step/configure-wan-tr069" in route_paths
        assert "/network/onts/{ont_id}/step/enable-ipv6" in route_paths
        assert "/network/onts/{ont_id}/step/rollback-service-ports" in route_paths

    def test_preflight_still_registered(self) -> None:
        from starlette.routing import Route

        from app.web.admin.network_onts_provisioning import router

        route_paths = [
            route.path for route in router.routes if isinstance(route, Route)
        ]
        assert "/network/onts/{ont_id}/preflight" in route_paths
        assert "/network/onts/{ont_id}/provisioning-preview" in route_paths


class TestModuleFunctions:
    """Verify key functions are importable from ont_provision_steps."""

    def test_validate_prerequisites_is_module_function(self) -> None:
        from app.services.network.ont_provision_steps import validate_prerequisites

        assert callable(validate_prerequisites)

    def test_preview_commands_is_module_function(self) -> None:
        from app.services.network.ont_provision_steps import preview_commands

        assert callable(preview_commands)
