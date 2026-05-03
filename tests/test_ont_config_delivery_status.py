from __future__ import annotations


def test_update_ont_config_reports_pending_when_acs_delivery_is_unavailable(
    db_session, monkeypatch
) -> None:
    from app.models.network import OntUnit
    from app.services.network.ont_action_common import ActionResult
    from app.services.web_network_ont_actions.db_config import update_ont_config

    ont = OntUnit(serial_number="CONFIG-PENDING-001", is_active=True)
    db_session.add(ont)
    db_session.commit()

    def fake_set_lan_config(*args, **kwargs):
        return ActionResult(
            success=False,
            message=(
                "ONT CONFIG-PENDING-001 has no GenieACS identity. Sync-only "
                "provisioning requires a resolvable ACS device before push."
            ),
            data={"missing_acs_identity": True},
        )

    monkeypatch.setattr(
        "app.services.web_network_ont_actions.db_config.set_lan_config",
        fake_set_lan_config,
    )

    result = update_ont_config(
        db_session,
        str(ont.id),
        lan_gateway_ip="192.168.1.1",
        lan_dhcp_enabled=True,
        push_to_device=True,
        push_wan=False,
        push_lan=True,
        push_mgmt=False,
        push_wifi=False,
    )

    assert result.success is True
    assert result.waiting is True
    assert "Configuration saved." in result.message
    assert "waiting for device inform to apply" in result.message
    assert "Use Advanced Actions" not in result.message
    db_session.refresh(ont)
    assert ont.desired_config["delivery"]["pending_apply"] is True


def test_update_ont_config_still_fails_invalid_delivery_input(
    db_session, monkeypatch
) -> None:
    from app.models.network import OntUnit
    from app.services.network.ont_action_common import ActionResult
    from app.services.web_network_ont_actions.db_config import update_ont_config

    ont = OntUnit(serial_number="CONFIG-INVALID-001", is_active=True)
    db_session.add(ont)
    db_session.commit()

    def fake_set_lan_config(*args, **kwargs):
        return ActionResult(
            success=False,
            message="LAN IP address must be a valid IPv4 address.",
        )

    monkeypatch.setattr(
        "app.services.web_network_ont_actions.db_config.set_lan_config",
        fake_set_lan_config,
    )

    result = update_ont_config(
        db_session,
        str(ont.id),
        lan_gateway_ip="not-an-ip",
        push_to_device=True,
        push_wan=False,
        push_lan=True,
        push_mgmt=False,
        push_wifi=False,
    )

    assert result.success is False
    assert result.waiting is False
    assert "must be a valid IPv4 address" in result.message


def test_update_ont_config_pushes_wifi_enabled_only(db_session, monkeypatch) -> None:
    from app.models.network import OntUnit
    from app.services.network.ont_action_common import ActionResult
    from app.services.web_network_ont_actions.db_config import update_ont_config

    ont = OntUnit(serial_number="WIFI-ENABLE-ONLY", is_active=True)
    db_session.add(ont)
    db_session.commit()

    calls = []

    def fake_set_wifi_config(*args, **kwargs):
        calls.append(kwargs)
        return ActionResult(success=True, message="wifi ok")

    monkeypatch.setattr(
        "app.services.web_network_ont_actions.db_config.set_wifi_config",
        fake_set_wifi_config,
    )

    result = update_ont_config(
        db_session,
        str(ont.id),
        wifi_enabled=False,
        push_to_device=True,
        push_wan=False,
        push_lan=False,
        push_mgmt=False,
        push_wifi=True,
    )

    assert result.success is True
    assert calls[0]["enabled"] is False


def test_update_ont_config_pushes_lan_dhcp_range_only(db_session, monkeypatch) -> None:
    from app.models.network import OntUnit
    from app.services.network.ont_action_common import ActionResult
    from app.services.web_network_ont_actions.db_config import update_ont_config

    ont = OntUnit(serial_number="LAN-RANGE-ONLY", is_active=True)
    db_session.add(ont)
    db_session.commit()

    calls = []

    def fake_set_lan_config(*args, **kwargs):
        calls.append(kwargs)
        return ActionResult(success=True, message="lan ok")

    monkeypatch.setattr(
        "app.services.web_network_ont_actions.db_config.set_lan_config",
        fake_set_lan_config,
    )

    result = update_ont_config(
        db_session,
        str(ont.id),
        lan_dhcp_start="192.168.1.100",
        lan_dhcp_end="192.168.1.200",
        push_to_device=True,
        push_wan=False,
        push_lan=True,
        push_mgmt=False,
        push_wifi=False,
    )

    assert result.success is True
    assert calls[0]["dhcp_start"] == "192.168.1.100"
    assert calls[0]["dhcp_end"] == "192.168.1.200"


def test_update_ont_config_pushes_static_wan_fields(db_session, monkeypatch) -> None:
    from app.models.network import OntUnit
    from app.services.network.ont_action_common import ActionResult
    from app.services.web_network_ont_actions.db_config import update_ont_config

    ont = OntUnit(serial_number="STATIC-WAN-FIELDS", is_active=True)
    db_session.add(ont)
    db_session.commit()

    calls = []

    def fake_set_wan_config(*args, **kwargs):
        calls.append(kwargs)
        return ActionResult(success=True, message="wan ok")

    monkeypatch.setattr(
        "app.services.web_network_ont_actions.db_config.set_wan_config",
        fake_set_wan_config,
    )

    result = update_ont_config(
        db_session,
        str(ont.id),
        wan_mode="static_ip",
        wan_static_ip="100.64.1.2",
        wan_static_subnet="255.255.255.252",
        wan_static_gateway="100.64.1.1",
        wan_static_dns="1.1.1.1",
        push_to_device=True,
        push_wan=True,
        push_lan=False,
        push_mgmt=False,
        push_wifi=False,
    )

    assert result.success is True
    assert calls[0]["wan_mode"] == "static"
    assert calls[0]["ip_address"] == "100.64.1.2"
    assert calls[0]["subnet_mask"] == "255.255.255.252"
    assert calls[0]["gateway"] == "100.64.1.1"
    assert calls[0]["dns_servers"] == "1.1.1.1"


def test_saved_wifi_only_desired_config_qualifies_for_apply_on_inform(
    db_session,
) -> None:
    from app.models.network import OntUnit
    from app.services.network.ont_desired_config import set_desired_config_values
    from app.services.tr069 import _ont_has_saved_service_intent

    ont = OntUnit(serial_number="WIFI-INTENT-001", is_active=True)
    set_desired_config_values(
        ont,
        {
            "wifi.enabled": True,
            "wifi.ssid": "DOTMAC-WIFI-INTENT",
        },
    )
    db_session.add(ont)
    db_session.commit()

    assert _ont_has_saved_service_intent(db_session, ont.id) is True


def test_effective_config_ignores_legacy_assignment_service_fields(db_session) -> None:
    from app.models.network import MgmtIpMode, OntAssignment, OntUnit, OnuMode
    from app.services.network.effective_ont_config import resolve_effective_ont_config
    from app.services.network.ont_desired_config import set_desired_config_values

    ont = OntUnit(serial_number="NO-ASSIGNMENT-FALLBACK", is_active=True)
    set_desired_config_values(
        ont,
        {
            "wan.mode": "dhcp",
            "wifi.ssid": None,
        },
    )
    db_session.add(ont)
    db_session.flush()
    db_session.add(
        OntAssignment(
            ont_unit_id=ont.id,
            active=True,
            wan_mode=OnuMode.routing,
            ip_mode=MgmtIpMode.dhcp,
            pppoe_username="stale-user",
            pppoe_password="stale-password",
            wifi_ssid="STALE-WIFI",
        )
    )
    db_session.commit()

    values = resolve_effective_ont_config(db_session, ont)["values"]

    assert values["wan_mode"] == "dhcp"
    assert values["pppoe_username"] is None
    assert values["pppoe_password"] is None
    assert values["wifi_ssid"] is None


def test_pppoe_health_ignores_legacy_assignment_username() -> None:
    from types import SimpleNamespace

    from app.services.network.pppoe_health import _row_pppoe_username

    row = SimpleNamespace(desired_config={}, ont_pppoe_username="stale-user")

    assert _row_pppoe_username(row) is None


def test_pending_delivery_marker_queues_apply_on_recent_inform(
    db_session, monkeypatch
) -> None:
    from datetime import UTC, datetime

    from app.models.network import OntUnit
    from app.services.network.ont_desired_config import set_desired_config_values
    from app.services.tr069 import _queue_saved_service_apply_after_stale_inform

    ont = OntUnit(serial_number="PENDING-INFORM-001", is_active=True)
    set_desired_config_values(
        ont,
        {
            "delivery.pending_apply": True,
            "wifi.ssid": "PENDING-INFORM",
        },
    )
    db_session.add(ont)
    db_session.commit()

    queued = {}

    def fake_enqueue_task(*args, **kwargs):
        queued["args"] = args
        queued["kwargs"] = kwargs
        return type("Dispatch", (), {"queued": True})()

    monkeypatch.setattr("app.services.queue_adapter.enqueue_task", fake_enqueue_task)
    now = datetime.now(UTC)

    result = _queue_saved_service_apply_after_stale_inform(
        db_session,
        ont_id=ont.id,
        previous_last_inform_at=now,
        now=now,
    )

    assert result is True
    assert queued["args"][0] == "app.tasks.tr069.apply_saved_ont_service_config"


def test_successful_saved_service_apply_clears_pending_marker(
    db_session, monkeypatch
) -> None:
    from types import SimpleNamespace

    from app.models.network import OntUnit
    from app.services.network.ont_desired_config import set_desired_config_values
    from app.services.network.ont_provision_steps import apply_saved_service_config

    ont = OntUnit(serial_number="CLEAR-PENDING-001", is_active=True)
    set_desired_config_values(
        ont,
        {
            "delivery.pending_apply": True,
            "wifi.ssid": "CLEAR-PENDING",
        },
    )
    db_session.add(ont)
    db_session.commit()

    monkeypatch.setattr(
        "app.services.network.ont_provision_steps.genieacs_service",
        SimpleNamespace(
            set_wifi_config=lambda *a, **kw: SimpleNamespace(
                success=True, message="wifi ok"
            )
        ),
    )

    result = apply_saved_service_config(db_session, str(ont.id))

    assert result.success is True
    assert "delivery" not in (ont.desired_config or {})
