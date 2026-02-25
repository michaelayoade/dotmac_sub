from app.services import web_vpn_management as vpn_mgmt


def test_generate_openvpn_key_material_returns_pem_blocks():
    key_pem, cert_pem = vpn_mgmt._generate_openvpn_key_material("Unit Test Client")

    assert "BEGIN RSA PRIVATE KEY" in key_pem
    assert "END RSA PRIVATE KEY" in key_pem
    assert "BEGIN CERTIFICATE" in cert_pem
    assert "END CERTIFICATE" in cert_pem


def test_build_openvpn_client_config_embeds_required_sections():
    config = vpn_mgmt._build_openvpn_client_config(
        client_name="Site A",
        client_key="-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----",
        client_cert="-----BEGIN CERTIFICATE-----\nxyz\n-----END CERTIFICATE-----",
        remote_host="vpn.example.com",
        remote_port=1194,
        proto="udp",
    )

    assert "client" in config
    assert "remote vpn.example.com 1194" in config
    assert "<ca>" in config
    assert "<cert>" in config
    assert "<key>" in config
