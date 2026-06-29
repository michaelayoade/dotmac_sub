from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from starlette.datastructures import FormData

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.schemas.router_management import RouterCreate
from app.services.network.service_classification import (
    parse_vlan_list,
    service_type_for_vlan,
)
from app.services.radius_address_lists import suspended_address_list
from app.services.radius_population import _radreply_attrs
from app.services.router_management.inventory import RouterInventory
from app.services.settings_cache import SettingsCache
from app.services.web_network_radius import parse_profile_form
from app.services.web_network_speedtests import count_underperforming_connections


def _text_setting(db_session, domain: SettingDomain, key: str, value: str) -> None:
    db_session.add(
        DomainSetting(
            domain=domain,
            key=key,
            value_type=SettingValueType.string,
            value_text=value,
        )
    )
    db_session.commit()
    SettingsCache.invalidate(domain.value, key)


def test_internet_service_vlans_are_configurable(db_session):
    assert parse_vlan_list("203, 310;bad,5000") == {203, 310}
    _text_setting(
        db_session,
        SettingDomain.network,
        "internet_service_vlans",
        "310, 311",
    )

    assert service_type_for_vlan(db_session, 310) == "internet"
    assert service_type_for_vlan(db_session, 203) == "management"


def test_suspended_address_list_setting_drives_radius_attrs(db_session):
    _text_setting(
        db_session,
        SettingDomain.radius,
        "suspended_address_list",
        "blocked-subscribers",
    )

    assert suspended_address_list(db_session) == "blocked-subscribers"
    attrs = _radreply_attrs(
        SimpleNamespace(status="suspended", ipv4_address=None),
        None,
        None,
        subscriber_blocked=True,
        captive_redirect_enabled=True,
        suspended_list_name=suspended_address_list(db_session),
    )
    assert ("Mikrotik-Address-List", ":=", "blocked-subscribers") in attrs


def test_radius_profile_form_surfaces_mikrotik_address_list():
    profile_data, _attributes, error = parse_profile_form(
        FormData(
            {
                "name": "Gold",
                "vendor": "mikrotik",
                "mikrotik_address_list": "gold-customers",
                "is_active": "true",
            }
        )
    )

    assert error is None
    assert profile_data["mikrotik_address_list"] == "gold-customers"


def test_router_count_honors_search_filter(db_session):
    RouterInventory.create(
        db_session,
        RouterCreate(
            name="edge-alpha",
            hostname="edge-alpha",
            management_ip="10.10.10.1",
            rest_api_username="admin",
            rest_api_password="secret",
        ),
    )
    RouterInventory.create(
        db_session,
        RouterCreate(
            name="core-beta",
            hostname="core-beta",
            management_ip="10.10.10.2",
            rest_api_username="admin",
            rest_api_password="secret",
        ),
    )

    assert RouterInventory.count(db_session, search="edge-alpha") == 1


def test_speedtest_underperforming_ratio_is_controllable():
    item = SimpleNamespace(
        download_mbps=75,
        upload_mbps=75,
        subscription=SimpleNamespace(
            offer=SimpleNamespace(speed_download_mbps=100, speed_upload_mbps=100)
        ),
    )

    assert count_underperforming_connections([item]) == 1
    assert count_underperforming_connections([item], sla_ratio=0.7) == 0


def test_polish_controls_are_rendered_in_templates():
    sessions = Path("templates/admin/network/sessions.html").read_text()
    speedtests = Path("templates/admin/network/speedtests/index.html").read_text()
    dns_threats = Path("templates/admin/network/dns_threats/index.html").read_text()
    profile = Path("templates/admin/network/radius/profile_form.html").read_text()
    decommission = Path(
        "templates/admin/network/onts/_decommission_modal.html"
    ).read_text()

    assert 'name="nas_filter"' in sessions
    assert 'name="date_to"' in speedtests
    assert 'name="network_device_id"' in dns_threats
    assert 'name="mikrotik_address_list"' in profile
    assert 'name="reason"' in decommission
    assert 'name="deauthorize_on_olt"' in decommission
    assert 'name="remove_from_acs"' in decommission


def test_destructive_actions_have_confirmations():
    quick_actions = Path("templates/admin/network/onts/_quick_actions.html").read_text()
    cpe = Path("templates/admin/network/cpes/_tr069_partial.html").read_text()
    tr069 = Path("templates/admin/network/tr069/index.html").read_text()
    router_push = Path("templates/admin/network/routers/push.html").read_text()
    vpn = Path("templates/admin/network/vpn/index.html").read_text()
    nas = Path("templates/admin/network/nas/device_detail.html").read_text()
    radius = Path("templates/admin/network/radius/index.html").read_text()

    assert "hx-confirm" in quick_actions
    assert "factory-reset" in cpe
    assert "Factory reset CPE" in cpe
    assert "confirmTr069BulkAction" in tr069
    assert "Push ${cmds.length} command(s)" in router_push
    assert "Regenerate keys for VPN server" in vpn
    assert "rotates the stored API credentials" in nas
    assert "Import PPPoE credentials now" in radius


def test_freshness_labels_match_snapshot_data():
    sessions = Path("templates/admin/network/sessions.html").read_text()
    monitoring = Path("templates/admin/network/monitoring/index.html").read_text()
    topology = Path("templates/admin/network/topology/index.html").read_text()

    assert "Live RADIUS active sessions" not in sessions
    assert "RADIUS active-session snapshot" in sessions
    assert "snapshot_at.strftime" in sessions
    assert "Current ONU Status Snapshot" in monitoring
    assert "ONU Status Trend (24h)" not in monitoring
    assert "Single snapshot" in monitoring
    assert ">Links<" in topology
    assert ">Nodes<" in topology
    assert "Maintenance" in topology
