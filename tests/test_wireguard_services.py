"""Tests for WireGuard VPN services.

Covers:
- wireguard_crypto: key generation, encryption/decryption, tokens
- wireguard service: server CRUD, peer CRUD, connection logs
- vpn_cache: Redis caching layer (mocked)
- vpn_routing: route helpers, LAN subnet sync
- wireguard_system: config generation, interface management (mocked subprocess)
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.models.wireguard import (
    WireGuardConnectionLog,
    WireGuardPeer,
    WireGuardPeerStatus,
    WireGuardServer,
)
from app.schemas.wireguard import (
    WireGuardPeerCreate,
    WireGuardPeerUpdate,
    WireGuardServerCreate,
    WireGuardServerUpdate,
)
from app.services.wireguard_crypto import (
    decrypt_private_key,
    derive_public_key,
    encrypt_private_key,
    generate_encryption_key,
    generate_keypair,
    generate_preshared_key,
    generate_provision_token,
    hash_token,
    validate_key,
    verify_token,
)


# ============================================================================
# Helpers
# ============================================================================


def _make_server(db_session, name: str = "test-server", **overrides) -> WireGuardServer:
    """Create a WireGuardServer directly in the DB for test isolation."""
    priv, pub = generate_keypair()
    defaults = dict(
        name=name,
        listen_port=51820,
        private_key=encrypt_private_key(priv),
        public_key=pub,
        vpn_address="10.10.0.1/24",
        mtu=1420,
        is_active=True,
    )
    defaults.update(overrides)
    server = WireGuardServer(**defaults)
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)
    return server


def _make_peer(
    db_session, server: WireGuardServer, name: str = "test-peer", **overrides
) -> WireGuardPeer:
    """Create a WireGuardPeer directly in the DB for test isolation."""
    priv, pub = generate_keypair()
    defaults = dict(
        server_id=server.id,
        name=name,
        public_key=pub,
        private_key=encrypt_private_key(priv),
        peer_address="10.10.0.2/32",
        allowed_ips=["10.10.0.2/32"],
        persistent_keepalive=25,
        status=WireGuardPeerStatus.active,
        rx_bytes=0,
        tx_bytes=0,
    )
    defaults.update(overrides)
    peer = WireGuardPeer(**defaults)
    db_session.add(peer)
    db_session.commit()
    db_session.refresh(peer)
    return peer


# ============================================================================
# wireguard_crypto tests
# ============================================================================


class TestGenerateKeypair:
    def test_returns_two_strings(self):
        priv, pub = generate_keypair()
        assert isinstance(priv, str)
        assert isinstance(pub, str)

    def test_keys_are_valid_base64(self):
        priv, pub = generate_keypair()
        priv_bytes = base64.b64decode(priv)
        pub_bytes = base64.b64decode(pub)
        assert len(priv_bytes) == 32
        assert len(pub_bytes) == 32

    def test_keys_differ(self):
        priv, pub = generate_keypair()
        assert priv != pub

    def test_successive_calls_produce_different_keys(self):
        priv1, pub1 = generate_keypair()
        priv2, pub2 = generate_keypair()
        assert priv1 != priv2
        assert pub1 != pub2


class TestDerivePublicKey:
    def test_derive_matches_generated(self):
        priv, pub = generate_keypair()
        derived = derive_public_key(priv)
        assert derived == pub

    def test_deterministic(self):
        priv, _ = generate_keypair()
        assert derive_public_key(priv) == derive_public_key(priv)


class TestGeneratePresharedKey:
    def test_returns_base64_32_bytes(self):
        psk = generate_preshared_key()
        decoded = base64.b64decode(psk)
        assert len(decoded) == 32

    def test_unique(self):
        assert generate_preshared_key() != generate_preshared_key()


class TestValidateKey:
    def test_valid_key(self):
        priv, pub = generate_keypair()
        assert validate_key(priv) is True
        assert validate_key(pub) is True

    def test_invalid_short(self):
        short = base64.b64encode(b"short").decode("ascii")
        assert validate_key(short) is False

    def test_invalid_not_base64(self):
        assert validate_key("not-valid-b64!@#$") is False

    def test_empty_string(self):
        assert validate_key("") is False

    def test_preshared_key(self):
        psk = generate_preshared_key()
        assert validate_key(psk) is True


class TestEncryptDecryptPrivateKey:
    def test_roundtrip_without_encryption_key(self):
        """Without WIREGUARD_KEY_ENCRYPTION_KEY, stores as plain:..."""
        priv, _ = generate_keypair()
        with patch("app.services.wireguard_crypto.get_encryption_key", return_value=None):
            encrypted = encrypt_private_key(priv)
            assert encrypted.startswith("plain:")
            decrypted = decrypt_private_key(encrypted)
            assert decrypted == priv

    def test_roundtrip_with_encryption_key(self):
        """With a Fernet key set, stores as enc:..."""
        from cryptography.fernet import Fernet

        fernet_key = Fernet.generate_key()

        priv, _ = generate_keypair()
        with patch(
            "app.services.wireguard_crypto.get_encryption_key",
            return_value=fernet_key,
        ):
            encrypted = encrypt_private_key(priv)
            assert encrypted.startswith("enc:")
            decrypted = decrypt_private_key(encrypted)
            assert decrypted == priv

    def test_decrypt_legacy_format(self):
        """Legacy keys without prefix are returned as-is."""
        priv, _ = generate_keypair()
        assert decrypt_private_key(priv) == priv

    def test_decrypt_enc_without_key_raises(self):
        with patch("app.services.wireguard_crypto.get_encryption_key", return_value=None):
            with pytest.raises(ValueError, match="not set"):
                decrypt_private_key("enc:bogusdata")


class TestProvisionToken:
    def test_token_length(self):
        token = generate_provision_token()
        assert len(token) == 32

    def test_unique(self):
        assert generate_provision_token() != generate_provision_token()


class TestHashToken:
    def test_deterministic(self):
        token = "test-token"
        assert hash_token(token) == hash_token(token)

    def test_sha256_hex(self):
        token = "hello"
        expected = hashlib.sha256(b"hello").hexdigest()
        assert hash_token(token) == expected


class TestVerifyToken:
    def test_correct_token(self):
        token = generate_provision_token()
        h = hash_token(token)
        assert verify_token(token, h) is True

    def test_wrong_token(self):
        h = hash_token("correct-token")
        assert verify_token("wrong-token", h) is False


class TestGenerateEncryptionKey:
    def test_returns_valid_fernet_key(self):
        key = generate_encryption_key()
        from cryptography.fernet import Fernet

        # Should not raise
        Fernet(key.encode("ascii"))


# ============================================================================
# wireguard service -- utility functions
# ============================================================================


class TestSanitizeInterfaceName:
    def test_basic(self):
        from app.services.wireguard import _sanitize_interface_name

        assert _sanitize_interface_name("My Router") == "wg-my-router"

    def test_special_chars(self):
        from app.services.wireguard import _sanitize_interface_name

        result = _sanitize_interface_name("@#$%^&")
        assert result.startswith("wg-")

    def test_empty(self):
        from app.services.wireguard import _sanitize_interface_name

        assert _sanitize_interface_name("") == "wg-wg"

    def test_truncation(self):
        from app.services.wireguard import _sanitize_interface_name

        result = _sanitize_interface_name("a" * 50, max_len=15)
        assert len(result) <= 15


class TestParseVpnNetwork:
    def test_valid(self):
        from app.services.wireguard import _parse_vpn_network

        server_ip, net_addr, prefix = _parse_vpn_network("10.10.0.1/24")
        assert server_ip == "10.10.0.1"
        assert net_addr == "10.10.0.0"
        assert prefix == 24

    def test_none_uses_default(self):
        from app.services.wireguard import _parse_vpn_network

        server_ip, net_addr, prefix = _parse_vpn_network(None)
        assert server_ip == "10.10.0.1"
        assert prefix == 24

    def test_string_none_uses_default(self):
        from app.services.wireguard import _parse_vpn_network

        server_ip, _, _ = _parse_vpn_network("None")
        assert server_ip == "10.10.0.1"

    def test_invalid_raises(self):
        from app.services.wireguard import _parse_vpn_network

        with pytest.raises(HTTPException) as exc_info:
            _parse_vpn_network("not-an-ip")
        assert exc_info.value.status_code == 400


class TestNormalizeAllowedIps:
    def test_basic(self):
        from app.services.wireguard import _normalize_allowed_ips

        result = _normalize_allowed_ips(["10.0.0.0/24", "192.168.1.0/24"])
        assert result == ["10.0.0.0/24", "192.168.1.0/24"]

    def test_bare_ip_gets_host_prefix(self):
        from app.services.wireguard import _normalize_allowed_ips

        result = _normalize_allowed_ips(["10.0.0.5"])
        assert result == ["10.0.0.5/32"]

    def test_deduplication(self):
        from app.services.wireguard import _normalize_allowed_ips

        result = _normalize_allowed_ips(["10.0.0.0/24", "10.0.0.0/24"])
        assert result == ["10.0.0.0/24"]

    def test_empty_returns_none(self):
        from app.services.wireguard import _normalize_allowed_ips

        assert _normalize_allowed_ips(None) is None
        assert _normalize_allowed_ips([]) is None

    def test_invalid_raises(self):
        from app.services.wireguard import _normalize_allowed_ips

        with pytest.raises(HTTPException):
            _normalize_allowed_ips(["not-a-network"])

    def test_ipv6_bare_gets_128(self):
        from app.services.wireguard import _normalize_allowed_ips

        result = _normalize_allowed_ips(["fd00::5"])
        assert result == ["fd00::5/128"]


# ============================================================================
# WireGuardServerService tests
# ============================================================================


class TestWireGuardServerService:
    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_create_server(self, mock_deploy, db_session):
        from app.services.wireguard import WireGuardServerService

        payload = WireGuardServerCreate(
            name=f"srv-{uuid.uuid4().hex[:8]}",
            vpn_address="10.20.0.1/24",
        )
        server = WireGuardServerService.create(db_session, payload)
        assert server.id is not None
        assert server.public_key is not None
        assert server.vpn_address == "10.20.0.1/24"

    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_create_duplicate_name_raises(self, mock_deploy, db_session):
        from app.services.wireguard import WireGuardServerService

        name = f"dup-{uuid.uuid4().hex[:8]}"
        WireGuardServerService.create(
            db_session, WireGuardServerCreate(name=name)
        )
        with pytest.raises(HTTPException) as exc_info:
            WireGuardServerService.create(
                db_session, WireGuardServerCreate(name=name)
            )
        assert exc_info.value.status_code == 400

    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_get_server(self, mock_deploy, db_session):
        from app.services.wireguard import WireGuardServerService

        server = _make_server(db_session, name=f"get-{uuid.uuid4().hex[:8]}")
        fetched = WireGuardServerService.get(db_session, server.id)
        assert fetched.id == server.id

    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_get_server_not_found(self, mock_deploy, db_session):
        from app.services.wireguard import WireGuardServerService

        with pytest.raises(HTTPException) as exc_info:
            WireGuardServerService.get(db_session, uuid.uuid4())
        assert exc_info.value.status_code == 404

    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_get_by_name(self, mock_deploy, db_session):
        from app.services.wireguard import WireGuardServerService

        name = f"byname-{uuid.uuid4().hex[:8]}"
        _make_server(db_session, name=name)
        result = WireGuardServerService.get_by_name(db_session, name)
        assert result is not None
        assert result.name == name

    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_get_by_name_not_found(self, mock_deploy, db_session):
        from app.services.wireguard import WireGuardServerService

        assert WireGuardServerService.get_by_name(db_session, "nonexistent") is None

    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_list_servers(self, mock_deploy, db_session):
        from app.services.wireguard import WireGuardServerService

        _make_server(db_session, name=f"list-a-{uuid.uuid4().hex[:8]}")
        _make_server(db_session, name=f"list-b-{uuid.uuid4().hex[:8]}")
        servers = WireGuardServerService.list(db_session)
        assert len(servers) >= 2

    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_list_servers_filter_active(self, mock_deploy, db_session):
        from app.services.wireguard import WireGuardServerService

        _make_server(db_session, name=f"active-{uuid.uuid4().hex[:8]}", is_active=True)
        _make_server(db_session, name=f"inactive-{uuid.uuid4().hex[:8]}", is_active=False)
        active = WireGuardServerService.list(db_session, is_active=True)
        assert all(s.is_active for s in active)

    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_update_server(self, mock_deploy, db_session):
        from app.services.wireguard import WireGuardServerService

        server = _make_server(db_session, name=f"upd-{uuid.uuid4().hex[:8]}")
        updated = WireGuardServerService.update(
            db_session,
            server.id,
            WireGuardServerUpdate(description="Updated desc"),
        )
        assert updated.description == "Updated desc"

    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_delete_server(self, mock_deploy, db_session):
        from app.services.wireguard import WireGuardServerService

        server = _make_server(db_session, name=f"del-{uuid.uuid4().hex[:8]}")
        sid = server.id
        WireGuardServerService.delete(db_session, sid)
        with pytest.raises(HTTPException) as exc_info:
            WireGuardServerService.get(db_session, sid)
        assert exc_info.value.status_code == 404

    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_regenerate_keys(self, mock_deploy, db_session):
        from app.services.wireguard import WireGuardServerService

        server = _make_server(db_session, name=f"regen-{uuid.uuid4().hex[:8]}")
        old_pub = server.public_key
        updated = WireGuardServerService.regenerate_keys(db_session, server.id)
        assert updated.public_key != old_pub

    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_get_peer_count(self, mock_deploy, db_session):
        from app.services.wireguard import WireGuardServerService

        server = _make_server(db_session, name=f"cnt-{uuid.uuid4().hex[:8]}")
        assert WireGuardServerService.get_peer_count(db_session, server.id) == 0
        _make_peer(db_session, server, name=f"p-{uuid.uuid4().hex[:8]}")
        assert WireGuardServerService.get_peer_count(db_session, server.id) == 1

    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_to_read_schema(self, mock_deploy, db_session):
        from app.services.wireguard import WireGuardServerService

        server = _make_server(db_session, name=f"schema-{uuid.uuid4().hex[:8]}")
        read = WireGuardServerService.to_read_schema(server, db_session)
        assert read.id == server.id
        assert read.has_private_key is True
        assert read.peer_count == 0

    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_get_server_status(self, mock_deploy, db_session):
        from app.services.wireguard import WireGuardServerService

        server = _make_server(db_session, name=f"status-{uuid.uuid4().hex[:8]}")
        _make_peer(
            db_session,
            server,
            name=f"sp-{uuid.uuid4().hex[:8]}",
            rx_bytes=100,
            tx_bytes=200,
        )
        status = WireGuardServerService.get_server_status(db_session, server.id)
        assert status["server_id"] == server.id
        assert status["total_peers"] == 1
        assert status["total_rx_bytes"] == 100
        assert status["total_tx_bytes"] == 200


# ============================================================================
# WireGuardPeerService tests
# ============================================================================


class TestWireGuardPeerService:
    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_create_peer_auto_keys(self, mock_deploy, db_session):
        from app.services.wireguard import WireGuardPeerService

        server = _make_server(db_session, name=f"pc-{uuid.uuid4().hex[:8]}")
        payload = WireGuardPeerCreate(
            server_id=server.id,
            name=f"peer-{uuid.uuid4().hex[:8]}",
        )
        created = WireGuardPeerService.create(db_session, payload)
        assert created.private_key is not None
        assert created.preshared_key is not None
        assert created.provision_token is not None

    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_create_peer_allocates_address(self, mock_deploy, db_session):
        from app.services.wireguard import WireGuardPeerService

        server = _make_server(db_session, name=f"pa-{uuid.uuid4().hex[:8]}")
        created = WireGuardPeerService.create(
            db_session,
            WireGuardPeerCreate(
                server_id=server.id,
                name=f"pa-{uuid.uuid4().hex[:8]}",
            ),
        )
        # First available after server (10.10.0.1) is 10.10.0.2
        assert created.peer_address == "10.10.0.2/32"

    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_create_peer_no_preshared_key(self, mock_deploy, db_session):
        from app.services.wireguard import WireGuardPeerService

        server = _make_server(db_session, name=f"nopsk-{uuid.uuid4().hex[:8]}")
        created = WireGuardPeerService.create(
            db_session,
            WireGuardPeerCreate(
                server_id=server.id,
                name=f"nopsk-{uuid.uuid4().hex[:8]}",
                use_preshared_key=False,
            ),
        )
        assert created.preshared_key is None

    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_create_peer_duplicate_public_key_raises(self, mock_deploy, db_session):
        from app.services.wireguard import WireGuardPeerService

        priv, pub = generate_keypair()
        server = _make_server(db_session, name=f"dpk-{uuid.uuid4().hex[:8]}")
        WireGuardPeerService.create(
            db_session,
            WireGuardPeerCreate(
                server_id=server.id,
                name=f"dpk1-{uuid.uuid4().hex[:8]}",
                public_key=pub,
                private_key=priv,
            ),
        )
        with pytest.raises(HTTPException) as exc_info:
            WireGuardPeerService.create(
                db_session,
                WireGuardPeerCreate(
                    server_id=server.id,
                    name=f"dpk2-{uuid.uuid4().hex[:8]}",
                    public_key=pub,
                    private_key=priv,
                ),
            )
        assert exc_info.value.status_code == 400
        assert "public key already exists" in exc_info.value.detail

    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_get_peer(self, mock_deploy, db_session):
        from app.services.wireguard import WireGuardPeerService

        server = _make_server(db_session, name=f"gp-{uuid.uuid4().hex[:8]}")
        peer = _make_peer(db_session, server, name=f"gp-{uuid.uuid4().hex[:8]}")
        fetched = WireGuardPeerService.get(db_session, peer.id)
        assert fetched.id == peer.id

    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_get_peer_not_found(self, mock_deploy, db_session):
        from app.services.wireguard import WireGuardPeerService

        with pytest.raises(HTTPException) as exc_info:
            WireGuardPeerService.get(db_session, uuid.uuid4())
        assert exc_info.value.status_code == 404

    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_list_peers(self, mock_deploy, db_session):
        from app.services.wireguard import WireGuardPeerService

        server = _make_server(db_session, name=f"lp-{uuid.uuid4().hex[:8]}")
        _make_peer(db_session, server, name=f"lp1-{uuid.uuid4().hex[:8]}", peer_address="10.10.0.2/32")
        _make_peer(db_session, server, name=f"lp2-{uuid.uuid4().hex[:8]}", peer_address="10.10.0.3/32")
        peers = WireGuardPeerService.list(db_session, server_id=server.id)
        assert len(peers) >= 2

    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_update_peer(self, mock_deploy, db_session):
        from app.services.wireguard import WireGuardPeerService

        server = _make_server(db_session, name=f"up-{uuid.uuid4().hex[:8]}")
        peer = _make_peer(db_session, server, name=f"up-{uuid.uuid4().hex[:8]}")
        updated = WireGuardPeerService.update(
            db_session, peer.id, WireGuardPeerUpdate(notes="Updated note")
        )
        assert updated.notes == "Updated note"

    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_delete_peer(self, mock_deploy, db_session):
        from app.services.wireguard import WireGuardPeerService

        server = _make_server(db_session, name=f"dp-{uuid.uuid4().hex[:8]}")
        peer = _make_peer(db_session, server, name=f"dp-{uuid.uuid4().hex[:8]}")
        pid = peer.id
        WireGuardPeerService.delete(db_session, pid)
        with pytest.raises(HTTPException) as exc_info:
            WireGuardPeerService.get(db_session, pid)
        assert exc_info.value.status_code == 404

    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_disable_enable_peer(self, mock_deploy, db_session):
        from app.services.wireguard import WireGuardPeerService

        server = _make_server(db_session, name=f"de-{uuid.uuid4().hex[:8]}")
        peer = _make_peer(db_session, server, name=f"de-{uuid.uuid4().hex[:8]}")
        disabled = WireGuardPeerService.disable(db_session, peer.id)
        assert disabled.status == WireGuardPeerStatus.disabled

        enabled = WireGuardPeerService.enable(db_session, peer.id)
        assert enabled.status == WireGuardPeerStatus.active

    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_regenerate_provision_token(self, mock_deploy, db_session):
        from app.services.wireguard import WireGuardPeerService

        server = _make_server(db_session, name=f"rpt-{uuid.uuid4().hex[:8]}")
        peer = _make_peer(db_session, server, name=f"rpt-{uuid.uuid4().hex[:8]}")
        token, expires_at = WireGuardPeerService.regenerate_provision_token(
            db_session, peer.id, expires_in_hours=48
        )
        assert len(token) > 0
        assert expires_at > datetime.now(timezone.utc)

    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_verify_provision_token_valid(self, mock_deploy, db_session):
        from app.services.wireguard import WireGuardPeerService

        server = _make_server(db_session, name=f"vpt-{uuid.uuid4().hex[:8]}")
        peer = _make_peer(db_session, server, name=f"vpt-{uuid.uuid4().hex[:8]}")
        token, _ = WireGuardPeerService.regenerate_provision_token(
            db_session, peer.id
        )
        verified = WireGuardPeerService.verify_provision_token(db_session, token)
        assert verified is not None
        assert verified.id == peer.id

    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_verify_provision_token_invalid(self, mock_deploy, db_session):
        from app.services.wireguard import WireGuardPeerService

        result = WireGuardPeerService.verify_provision_token(db_session, "bogus-token")
        assert result is None

    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_verify_provision_token_expired(self, mock_deploy, db_session):
        from app.services.wireguard import WireGuardPeerService

        server = _make_server(db_session, name=f"exp-{uuid.uuid4().hex[:8]}")
        peer = _make_peer(db_session, server, name=f"exp-{uuid.uuid4().hex[:8]}")
        token = generate_provision_token()
        peer.provision_token_hash = hash_token(token)
        peer.provision_token_expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db_session.commit()

        result = WireGuardPeerService.verify_provision_token(db_session, token)
        assert result is None

    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_generate_peer_config(self, mock_deploy, db_session):
        from app.services.wireguard import WireGuardPeerService

        server = _make_server(
            db_session,
            name=f"cfg-{uuid.uuid4().hex[:8]}",
            public_host="vpn.example.com",
        )
        peer = _make_peer(db_session, server, name=f"cfg-{uuid.uuid4().hex[:8]}")
        config = WireGuardPeerService.generate_peer_config(db_session, peer.id)
        assert "[Interface]" in config.config_content
        assert "[Peer]" in config.config_content
        assert server.public_key in config.config_content
        assert config.filename.endswith(".conf")

    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_generate_peer_config_no_private_key_raises(self, mock_deploy, db_session):
        from app.services.wireguard import WireGuardPeerService

        server = _make_server(db_session, name=f"nopk-{uuid.uuid4().hex[:8]}")
        peer = _make_peer(
            db_session,
            server,
            name=f"nopk-{uuid.uuid4().hex[:8]}",
            private_key=None,
        )
        with pytest.raises(HTTPException) as exc_info:
            WireGuardPeerService.generate_peer_config(db_session, peer.id)
        assert exc_info.value.status_code == 400

    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_to_read_schema(self, mock_deploy, db_session):
        from app.services.wireguard import WireGuardPeerService

        server = _make_server(db_session, name=f"prs-{uuid.uuid4().hex[:8]}")
        peer = _make_peer(db_session, server, name=f"prs-{uuid.uuid4().hex[:8]}")
        read = WireGuardPeerService.to_read_schema(peer, db_session)
        assert read.id == peer.id
        assert read.server_name == server.name
        assert read.has_private_key is True

    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_register_with_token(self, mock_deploy, db_session):
        from app.services.wireguard import WireGuardPeerService

        server = _make_server(
            db_session,
            name=f"reg-{uuid.uuid4().hex[:8]}",
            public_host="vpn.example.com",
        )
        peer = _make_peer(db_session, server, name=f"reg-{uuid.uuid4().hex[:8]}")

        # Set up a valid provision token
        token = generate_provision_token()
        peer.provision_token_hash = hash_token(token)
        peer.provision_token_expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
        db_session.commit()

        # Device generates its own keypair
        _, device_pub = generate_keypair()
        config = WireGuardPeerService.register_with_token(db_session, token, device_pub)
        assert config.config_content is not None
        assert server.public_key in config.config_content

        # Token should be invalidated
        db_session.refresh(peer)
        assert peer.provision_token_hash is None
        assert peer.public_key == device_pub


# ============================================================================
# WireGuardConnectionLogService tests
# ============================================================================


class TestConnectionLogService:
    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_log_connect(self, mock_deploy, db_session):
        from app.services.wireguard import WireGuardConnectionLogService

        server = _make_server(db_session, name=f"lc-{uuid.uuid4().hex[:8]}")
        peer = _make_peer(db_session, server, name=f"lc-{uuid.uuid4().hex[:8]}")
        log = WireGuardConnectionLogService.log_connect(
            db_session, peer.id, "1.2.3.4", "10.10.0.2/32"
        )
        assert log.id is not None
        assert log.endpoint_ip == "1.2.3.4"

        # Peer should be updated
        db_session.refresh(peer)
        assert peer.endpoint_ip == "1.2.3.4"
        assert peer.last_handshake_at is not None

    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_log_disconnect(self, mock_deploy, db_session):
        from app.services.wireguard import WireGuardConnectionLogService

        server = _make_server(db_session, name=f"ld-{uuid.uuid4().hex[:8]}")
        peer = _make_peer(db_session, server, name=f"ld-{uuid.uuid4().hex[:8]}")
        log = WireGuardConnectionLogService.log_connect(
            db_session, peer.id, "1.2.3.4", "10.10.0.2/32"
        )
        updated_log = WireGuardConnectionLogService.log_disconnect(
            db_session, log.id, rx_bytes=1000, tx_bytes=2000, reason="timeout"
        )
        assert updated_log.disconnected_at is not None
        assert updated_log.rx_bytes == 1000
        assert updated_log.disconnect_reason == "timeout"

        # Peer traffic should be accumulated
        db_session.refresh(peer)
        assert peer.rx_bytes == 1000
        assert peer.tx_bytes == 2000

    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_log_disconnect_not_found(self, mock_deploy, db_session):
        from app.services.wireguard import WireGuardConnectionLogService

        with pytest.raises(HTTPException) as exc_info:
            WireGuardConnectionLogService.log_disconnect(db_session, uuid.uuid4())
        assert exc_info.value.status_code == 404

    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_list_by_peer(self, mock_deploy, db_session):
        from app.services.wireguard import WireGuardConnectionLogService

        server = _make_server(db_session, name=f"lbp-{uuid.uuid4().hex[:8]}")
        peer = _make_peer(db_session, server, name=f"lbp-{uuid.uuid4().hex[:8]}")
        WireGuardConnectionLogService.log_connect(
            db_session, peer.id, "1.1.1.1", "10.10.0.2"
        )
        WireGuardConnectionLogService.log_connect(
            db_session, peer.id, "2.2.2.2", "10.10.0.2"
        )
        logs = WireGuardConnectionLogService.list_by_peer(db_session, peer.id)
        assert len(logs) == 2

    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_list_by_peer_with_names(self, mock_deploy, db_session):
        from app.services.wireguard import WireGuardConnectionLogService

        server = _make_server(db_session, name=f"lbpn-{uuid.uuid4().hex[:8]}")
        peer = _make_peer(db_session, server, name=f"lbpn-{uuid.uuid4().hex[:8]}")
        WireGuardConnectionLogService.log_connect(
            db_session, peer.id, "3.3.3.3", "10.10.0.2"
        )
        results = WireGuardConnectionLogService.list_by_peer_with_names(
            db_session, peer.id
        )
        assert len(results) == 1
        assert results[0]["peer_name"] == peer.name

    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_cleanup_old_logs(self, mock_deploy, db_session):
        from app.services.wireguard import WireGuardConnectionLogService

        server = _make_server(db_session, name=f"cl-{uuid.uuid4().hex[:8]}")
        peer = _make_peer(db_session, server, name=f"cl-{uuid.uuid4().hex[:8]}")

        # Create an old log entry manually
        old_log = WireGuardConnectionLog(
            peer_id=peer.id,
            connected_at=datetime.now(timezone.utc) - timedelta(days=100),
            endpoint_ip="9.9.9.9",
            peer_address="10.10.0.2",
        )
        db_session.add(old_log)
        db_session.commit()

        deleted = WireGuardConnectionLogService.cleanup_old_logs(db_session, days=90)
        assert deleted >= 1


# ============================================================================
# MikroTikScriptService tests
# ============================================================================


class TestMikroTikScriptService:
    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_generate_script(self, mock_deploy, db_session):
        from app.services.wireguard import MikroTikScriptService

        server = _make_server(
            db_session,
            name=f"mt-{uuid.uuid4().hex[:8]}",
            public_host="vpn.example.com",
        )
        peer = _make_peer(db_session, server, name=f"mt-{uuid.uuid4().hex[:8]}")
        result = MikroTikScriptService.generate_script(db_session, peer.id)
        assert "RouterOS 7" in result.script_content
        assert server.public_key in result.script_content
        assert result.filename.endswith(".rsc")

    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_generate_script_no_private_key(self, mock_deploy, db_session):
        from app.services.wireguard import MikroTikScriptService

        server = _make_server(db_session, name=f"mtnk-{uuid.uuid4().hex[:8]}")
        peer = _make_peer(
            db_session,
            server,
            name=f"mtnk-{uuid.uuid4().hex[:8]}",
            private_key=None,
        )
        with pytest.raises(HTTPException) as exc_info:
            MikroTikScriptService.generate_script(db_session, peer.id)
        assert exc_info.value.status_code == 400


# ============================================================================
# vpn_cache tests (mock Redis)
# ============================================================================


class TestVpnCache:
    def test_make_key(self):
        from app.services.vpn_cache import _make_key

        key = _make_key("server_config", "abc-123")
        assert key == "wg:server_config:abc-123"

    def test_make_key_multiple_parts(self):
        from app.services.vpn_cache import _make_key

        key = _make_key("peer_config", "server1", "peer1")
        assert key == "wg:peer_config:server1:peer1"

    def test_make_key_skips_none(self):
        from app.services.vpn_cache import _make_key

        key = _make_key("test", "a", None, "b")
        assert key == "wg:test:a:b"

    def test_get_cached_no_redis(self):
        from app.services.vpn_cache import get_cached

        with patch("app.services.vpn_cache.get_redis_client", return_value=None):
            assert get_cached("some-key") is None

    def test_set_cached_no_redis(self):
        from app.services.vpn_cache import set_cached

        with patch("app.services.vpn_cache.get_redis_client", return_value=None):
            assert set_cached("key", "value") is False

    def test_delete_cached_no_redis(self):
        from app.services.vpn_cache import delete_cached

        with patch("app.services.vpn_cache.get_redis_client", return_value=None):
            assert delete_cached("key") is False

    def test_set_and_get_with_mock_redis(self):
        from app.services.vpn_cache import get_cached, set_cached

        mock_client = MagicMock()
        mock_client.get.return_value = "cached-value"

        with patch("app.services.vpn_cache.get_redis_client", return_value=mock_client):
            assert set_cached("key", "value", ttl=60) is True
            mock_client.setex.assert_called_once_with("key", 60, "value")

            result = get_cached("key")
            assert result == "cached-value"

    def test_delete_cached_with_mock_redis(self):
        from app.services.vpn_cache import delete_cached

        mock_client = MagicMock()
        with patch("app.services.vpn_cache.get_redis_client", return_value=mock_client):
            assert delete_cached("key") is True
            mock_client.delete.assert_called_once_with("key")

    def test_delete_pattern_no_redis(self):
        from app.services.vpn_cache import delete_pattern

        with patch("app.services.vpn_cache.get_redis_client", return_value=None):
            assert delete_pattern("test*") == 0

    def test_delete_pattern_with_mock_redis(self):
        from app.services.vpn_cache import delete_pattern

        mock_client = MagicMock()
        mock_client.scan_iter.return_value = ["wg:key1", "wg:key2"]
        mock_client.delete.return_value = 2

        with patch("app.services.vpn_cache.get_redis_client", return_value=mock_client):
            count = delete_pattern("key")
            assert count == 2

    def test_server_config_helpers(self):
        from app.services.vpn_cache import get_server_config, set_server_config

        mock_client = MagicMock()
        mock_client.get.return_value = "server-config-data"

        with patch("app.services.vpn_cache.get_redis_client", return_value=mock_client):
            set_server_config("srv-id", "config-data")
            result = get_server_config("srv-id")
            assert result == "server-config-data"

    def test_peer_config_helpers(self):
        from app.services.vpn_cache import get_peer_config, set_peer_config

        mock_client = MagicMock()
        mock_client.get.return_value = "peer-config-data"

        with patch("app.services.vpn_cache.get_redis_client", return_value=mock_client):
            set_peer_config("peer-id", "config-data", "server-id")
            result = get_peer_config("peer-id")
            assert result == "peer-config-data"

    def test_mikrotik_script_helpers(self):
        from app.services.vpn_cache import get_mikrotik_script, set_mikrotik_script

        mock_client = MagicMock()
        mock_client.get.return_value = "script-data"

        with patch("app.services.vpn_cache.get_redis_client", return_value=mock_client):
            set_mikrotik_script("peer-id", "script-data", "server-id")
            result = get_mikrotik_script("peer-id")
            assert result == "script-data"

    def test_invalidate_server(self):
        from app.services.vpn_cache import invalidate_server

        mock_client = MagicMock()
        mock_client.delete.return_value = 1
        mock_client.scan_iter.return_value = []

        with patch("app.services.vpn_cache.get_redis_client", return_value=mock_client):
            count = invalidate_server("srv-id")
            assert count >= 1

    def test_invalidate_peer(self):
        from app.services.vpn_cache import invalidate_peer

        mock_client = MagicMock()
        with patch("app.services.vpn_cache.get_redis_client", return_value=mock_client):
            result = invalidate_peer("peer-id")
            assert result is True

    def test_is_cache_available_false(self):
        from app.services.vpn_cache import is_cache_available

        with patch("app.services.vpn_cache.get_redis_client", return_value=None):
            assert is_cache_available() is False

    def test_is_cache_available_true(self):
        from app.services.vpn_cache import is_cache_available

        mock_client = MagicMock()
        mock_client.ping.return_value = True
        with patch("app.services.vpn_cache.get_redis_client", return_value=mock_client):
            assert is_cache_available() is True

    def test_get_cache_stats_unavailable(self):
        from app.services.vpn_cache import get_cache_stats

        with patch("app.services.vpn_cache.get_redis_client", return_value=None):
            stats = get_cache_stats()
            assert stats["available"] is False

    def test_flush_all_vpn_cache(self):
        from app.services.vpn_cache import flush_all_vpn_cache

        mock_client = MagicMock()
        mock_client.scan_iter.return_value = ["wg:a", "wg:b"]
        mock_client.delete.return_value = 2
        with patch("app.services.vpn_cache.get_redis_client", return_value=mock_client):
            count = flush_all_vpn_cache()
            assert count == 2


# ============================================================================
# vpn_routing tests
# ============================================================================


class TestVpnRouting:
    def test_vpn_routing_error_is_runtime_error(self):
        from app.services.vpn_routing import VpnRoutingError

        assert issubclass(VpnRoutingError, RuntimeError)

    def test_ensure_vpn_ready_none(self):
        from app.services.vpn_routing import ensure_vpn_ready

        assert ensure_vpn_ready(MagicMock(), None) is None

    def test_ensure_vpn_ready_not_found(self, db_session):
        from app.services.vpn_routing import VpnRoutingError, ensure_vpn_ready

        with pytest.raises(VpnRoutingError, match="not found"):
            ensure_vpn_ready(db_session, uuid.uuid4())

    def test_ensure_vpn_ready_inactive(self, db_session):
        from app.services.vpn_routing import VpnRoutingError, ensure_vpn_ready

        server = _make_server(
            db_session,
            name=f"inact-{uuid.uuid4().hex[:8]}",
            is_active=False,
        )
        with pytest.raises(VpnRoutingError, match="inactive"):
            ensure_vpn_ready(db_session, server.id)

    @patch("app.services.vpn_routing.WireGuardSystemService.is_interface_up", return_value=True)
    def test_ensure_vpn_ready_active_and_up(self, mock_up, db_session):
        from app.services.vpn_routing import ensure_vpn_ready

        server = _make_server(db_session, name=f"up-{uuid.uuid4().hex[:8]}")
        result = ensure_vpn_ready(db_session, server.id)
        assert result is not None
        assert result.id == server.id

    def test_sync_peer_routes_for_ip_no_ip(self):
        from app.services.vpn_routing import sync_peer_routes_for_ip

        peer = MagicMock()
        server = MagicMock()
        assert sync_peer_routes_for_ip(peer, server, None) is False

    def test_sync_peer_routes_for_ip_public_ip(self):
        from app.services.vpn_routing import sync_peer_routes_for_ip

        peer = MagicMock()
        server = MagicMock()
        assert sync_peer_routes_for_ip(peer, server, "8.8.8.8") is False

    def test_sync_peer_routes_for_ip_private_ip(self):
        from app.services.vpn_routing import sync_peer_routes_for_ip

        peer = MagicMock()
        peer.metadata_ = None
        peer.allowed_ips = []
        server = MagicMock()
        server.metadata_ = {}
        result = sync_peer_routes_for_ip(peer, server, "192.168.1.100")
        assert result is True

    def test_normalize_networks_valid(self):
        from app.services.vpn_routing import _normalize_networks

        nets = _normalize_networks(["10.0.0.0/24", "192.168.1.0/24"])
        assert len(nets) == 2

    def test_normalize_networks_invalid_skipped(self):
        from app.services.vpn_routing import _normalize_networks

        nets = _normalize_networks(["not-a-cidr", "10.0.0.0/24"])
        assert len(nets) == 1

    def test_select_target_cidr_matching(self):
        from ipaddress import ip_address, ip_network

        from app.services.vpn_routing import _select_target_cidr

        ip = ip_address("192.168.1.50")
        nets = [ip_network("192.168.1.0/24")]
        result = _select_target_cidr(ip, nets)
        assert result == "192.168.1.0/24"

    def test_select_target_cidr_no_match(self):
        from ipaddress import ip_address

        from app.services.vpn_routing import _select_target_cidr

        ip = ip_address("10.0.0.5")
        result = _select_target_cidr(ip, [])
        assert result == "10.0.0.5/32"

    def test_ip_in_networks(self):
        from ipaddress import ip_address

        from app.services.vpn_routing import _ip_in_networks

        ip = ip_address("10.0.0.5")
        assert _ip_in_networks(ip, ["10.0.0.0/24"]) is True
        assert _ip_in_networks(ip, ["192.168.0.0/24"]) is False

    def test_sync_lan_subnets_adds_and_removes(self):
        from app.services.vpn_routing import sync_lan_subnets

        peer = MagicMock()
        peer.metadata_ = {"lan_subnets": ["10.0.1.0/24"]}
        peer.allowed_ips = ["10.10.0.2/32", "10.0.0.0/24"]

        server = MagicMock()
        server.metadata_ = {"routes": ["10.0.0.0/24"]}

        changed = sync_lan_subnets(
            peer, server, previous_subnets=["10.0.0.0/24"]
        )
        assert changed is True
        # Old subnet removed, new one added
        assert "10.0.1.0/24" in peer.allowed_ips
        assert "10.0.0.0/24" not in peer.allowed_ips

    def test_sync_lan_subnets_empty(self):
        from app.services.vpn_routing import sync_lan_subnets

        peer = MagicMock()
        peer.metadata_ = None
        peer.allowed_ips = []

        server = MagicMock()
        server.metadata_ = {}

        changed = sync_lan_subnets(peer, server)
        assert changed is False

    def test_blocked_lan_subnet(self):
        from app.services.vpn_routing import _is_blocked_lan_subnet
        from ipaddress import ip_network

        # Default blocked is 172.20.0.0/16
        assert _is_blocked_lan_subnet(ip_network("172.20.1.0/24")) is True
        assert _is_blocked_lan_subnet(ip_network("10.0.0.0/24")) is False


# ============================================================================
# WireGuardSystemService tests (subprocess mocked)
# ============================================================================


class TestWireGuardSystemService:
    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_generate_config(self, mock_deploy, db_session):
        from app.services.wireguard_system import WireGuardSystemService

        server = _make_server(db_session, name=f"gc-{uuid.uuid4().hex[:8]}")
        peer = _make_peer(db_session, server, name=f"gc-{uuid.uuid4().hex[:8]}")
        config = WireGuardSystemService.generate_config(db_session, server.id)
        assert "[Interface]" in config
        assert "[Peer]" in config
        assert peer.public_key in config

    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_generate_config_server_not_found(self, mock_deploy, db_session):
        from app.services.wireguard_system import WireGuardSystemService

        with pytest.raises(ValueError, match="not found"):
            WireGuardSystemService.generate_config(db_session, uuid.uuid4())

    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_generate_config_no_private_key(self, mock_deploy, db_session):
        from app.services.wireguard_system import WireGuardSystemService

        server = _make_server(
            db_session,
            name=f"nopk-{uuid.uuid4().hex[:8]}",
            private_key=None,
        )
        with pytest.raises(ValueError, match="no private key"):
            WireGuardSystemService.generate_config(db_session, server.id)

    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_generate_config_excludes_disabled_peers(self, mock_deploy, db_session):
        from app.services.wireguard_system import WireGuardSystemService

        server = _make_server(db_session, name=f"exc-{uuid.uuid4().hex[:8]}")
        active_peer = _make_peer(
            db_session,
            server,
            name=f"act-{uuid.uuid4().hex[:8]}",
            peer_address="10.10.0.2/32",
            status=WireGuardPeerStatus.active,
        )
        disabled_peer = _make_peer(
            db_session,
            server,
            name=f"dis-{uuid.uuid4().hex[:8]}",
            peer_address="10.10.0.3/32",
            status=WireGuardPeerStatus.disabled,
        )
        config = WireGuardSystemService.generate_config(db_session, server.id)
        assert active_peer.public_key in config
        assert disabled_peer.public_key not in config

    @patch("subprocess.run")
    def test_is_interface_up_true(self, mock_run):
        from app.services.wireguard_system import WireGuardSystemService

        mock_run.return_value = MagicMock(returncode=0)
        assert WireGuardSystemService.is_interface_up("wg0") is True

    @patch("subprocess.run")
    def test_is_interface_up_false(self, mock_run):
        from app.services.wireguard_system import WireGuardSystemService

        mock_run.return_value = MagicMock(returncode=1)
        assert WireGuardSystemService.is_interface_up("wg0") is False

    @patch("subprocess.run")
    def test_bring_up_interface_success(self, mock_run):
        from app.services.wireguard_system import WireGuardSystemService

        mock_run.return_value = MagicMock(returncode=0)
        success, msg = WireGuardSystemService.bring_up_interface("wg0")
        assert success is True
        assert "up" in msg.lower()

    @patch("subprocess.run")
    def test_bring_up_interface_failure(self, mock_run):
        from app.services.wireguard_system import WireGuardSystemService

        mock_run.return_value = MagicMock(returncode=1, stderr="error")
        success, msg = WireGuardSystemService.bring_up_interface("wg0")
        assert success is False

    @patch("subprocess.run", side_effect=FileNotFoundError)
    def test_bring_up_interface_not_installed(self, mock_run):
        from app.services.wireguard_system import WireGuardSystemService

        success, msg = WireGuardSystemService.bring_up_interface("wg0")
        assert success is False
        assert "not found" in msg.lower()

    @patch("subprocess.run")
    def test_bring_down_interface_success(self, mock_run):
        from app.services.wireguard_system import WireGuardSystemService

        mock_run.return_value = MagicMock(returncode=0)
        success, msg = WireGuardSystemService.bring_down_interface("wg0")
        assert success is True

    @patch("subprocess.run")
    def test_bring_down_interface_not_up(self, mock_run):
        from app.services.wireguard_system import WireGuardSystemService

        mock_run.return_value = MagicMock(
            returncode=1, stderr="is not a WireGuard interface"
        )
        success, msg = WireGuardSystemService.bring_down_interface("wg0")
        assert success is True

    @patch("subprocess.run")
    def test_get_interface_status_down(self, mock_run):
        from app.services.wireguard_system import WireGuardSystemService

        mock_run.return_value = MagicMock(returncode=1)
        status = WireGuardSystemService.get_interface_status("wg0")
        assert status["is_up"] is False
        assert status["peers"] == []

    @patch("subprocess.run")
    def test_get_interface_status_up(self, mock_run):
        from app.services.wireguard_system import WireGuardSystemService

        priv, pub = generate_keypair()
        _, peer_pub = generate_keypair()
        dump_output = (
            f"{priv}\t{pub}\t51820\toff\n"
            f"{peer_pub}\t(none)\t1.2.3.4:51820\t10.10.0.2/32\t1700000000\t1024\t2048\t25\n"
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=dump_output)
        status = WireGuardSystemService.get_interface_status("wg0")
        assert status["is_up"] is True
        assert len(status["peers"]) == 1
        assert status["peers"][0]["public_key"] == peer_pub

    @patch("subprocess.run")
    def test_enable_systemd_service(self, mock_run):
        from app.services.wireguard_system import WireGuardSystemService

        mock_run.return_value = MagicMock(returncode=0)
        success, msg = WireGuardSystemService.enable_systemd_service("wg0")
        assert success is True

    @patch("subprocess.run")
    def test_disable_systemd_service(self, mock_run):
        from app.services.wireguard_system import WireGuardSystemService

        mock_run.return_value = MagicMock(returncode=0)
        success, msg = WireGuardSystemService.disable_systemd_service("wg0")
        assert success is True


# ============================================================================
# Address allocation tests
# ============================================================================


class TestAllocatePeerAddress:
    @patch("app.services.wireguard.WireGuardPeerService._auto_deploy")
    def test_sequential_allocation(self, mock_deploy, db_session):
        """Multiple peers get sequential addresses."""
        from app.services.wireguard import WireGuardPeerService

        server = _make_server(db_session, name=f"alloc-{uuid.uuid4().hex[:8]}")
        peer1 = WireGuardPeerService.create(
            db_session,
            WireGuardPeerCreate(
                server_id=server.id,
                name=f"alloc-1-{uuid.uuid4().hex[:8]}",
            ),
        )
        peer2 = WireGuardPeerService.create(
            db_session,
            WireGuardPeerCreate(
                server_id=server.id,
                name=f"alloc-2-{uuid.uuid4().hex[:8]}",
            ),
        )
        # Server is 10.10.0.1, first peer is .2, second is .3
        assert peer1.peer_address == "10.10.0.2/32"
        assert peer2.peer_address == "10.10.0.3/32"

    def test_requested_address_outside_network_raises(self, db_session):
        from app.services.wireguard import _allocate_peer_address

        server = _make_server(db_session, name=f"oor-{uuid.uuid4().hex[:8]}")
        with pytest.raises(HTTPException) as exc_info:
            _allocate_peer_address(
                db_session,
                server,
                "10.10.0.1/24",
                requested_address="192.168.1.5",
            )
        assert exc_info.value.status_code == 400
        assert "not in server network" in exc_info.value.detail
