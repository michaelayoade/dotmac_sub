from types import SimpleNamespace

from app.services import nas as nas_service
from app.services import web_network_ip as web_network_ip_service
from app.web.admin import nas as nas_web


def test_validate_ipv4_address_rejects_invalid_octet():
    error = nas_web._validate_ipv4_address("172.16.300.5", "IP address")
    assert error == "IP address must be a valid IPv4 address."


def test_merge_radius_pool_tags_replaces_previous_radius_tags():
    merged = nas_web._merge_radius_pool_tags(
        ["site:pop1", "radius_pool:old-1"],
        ["pool-a", "pool-b"],
    )
    assert merged == ["site:pop1", "radius_pool:pool-a", "radius_pool:pool-b"]


def test_extract_enhanced_fields_from_tags():
    fields = nas_web._extract_enhanced_fields(
        [
            "partner_org:11111111-1111-1111-1111-111111111111",
            "authorization_type:ppp_dhcp_radius",
            "accounting_type:radius_accounting",
            "physical_address:Main Street",
            "latitude:9.0820",
            "longitude:8.6753",
        ]
    )
    assert fields["partner_org_ids"] == ["11111111-1111-1111-1111-111111111111"]
    assert fields["authorization_type"] == "ppp_dhcp_radius"
    assert fields["accounting_type"] == "radius_accounting"


def test_usable_ipv4_count_handles_common_prefixes():
    assert web_network_ip_service._usable_ipv4_count("10.0.0.0/24") == 254
    assert web_network_ip_service._usable_ipv4_count("10.0.0.0/31") == 2
    assert web_network_ip_service._usable_ipv4_count("not-a-cidr") == 0


def test_get_ping_status_reachable_with_latency(monkeypatch):
    def _fake_run(*_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout="64 bytes time=12.5 ms", stderr="")

    monkeypatch.setattr(nas_service.subprocess, "run", _fake_run)
    status = nas_service.get_ping_status("192.0.2.10")
    assert status["state"] == "reachable"
    assert status["latency_ms"] == 12.5


def test_get_ping_status_unreachable(monkeypatch):
    def _fake_run(*_args, **_kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="timeout")

    monkeypatch.setattr(nas_service.subprocess, "run", _fake_run)
    status = nas_service.get_ping_status("192.0.2.11")
    assert status == {"state": "unreachable", "label": "Unreachable"}
