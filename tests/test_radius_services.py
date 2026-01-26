"""Tests for RADIUS service."""

import socket
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.models.radius import RadiusServer
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.schemas.radius import (
    RadiusServerCreate, RadiusServerUpdate,
    RadiusClientCreate, RadiusClientUpdate,
    RadiusSyncJobCreate,
)
from app.services import radius as radius_service
from app.services import radius_auth


def test_create_radius_server(db_session):
    """Test creating a RADIUS server."""
    server = radius_service.radius_servers.create(
        db_session,
        RadiusServerCreate(
            name="Main RADIUS",
            host="radius.example.com",
            auth_port=1812,
            acct_port=1813,
        ),
    )
    assert server.name == "Main RADIUS"
    assert server.host == "radius.example.com"
    assert server.auth_port == 1812
    assert server.acct_port == 1813


def test_radius_server_default_ports(db_session):
    """Test RADIUS server with default ports."""
    server = radius_service.radius_servers.create(
        db_session,
        RadiusServerCreate(
            name="Default Ports",
            host="radius2.example.com",
        ),
    )
    assert server.host == "radius2.example.com"


def test_list_radius_servers(db_session):
    """Test listing RADIUS servers."""
    radius_service.radius_servers.create(
        db_session,
        RadiusServerCreate(name="Server 1", host="radius1.local"),
    )
    radius_service.radius_servers.create(
        db_session,
        RadiusServerCreate(name="Server 2", host="radius2.local"),
    )

    servers = radius_service.radius_servers.list(
        db_session,
        is_active=None,
        order_by="created_at",
        order_dir="asc",
        limit=10,
        offset=0,
    )
    assert len(servers) >= 2


def test_update_radius_server(db_session):
    """Test updating a RADIUS server."""
    server = radius_service.radius_servers.create(
        db_session,
        RadiusServerCreate(name="Original", host="old.host.com"),
    )
    updated = radius_service.radius_servers.update(
        db_session,
        str(server.id),
        RadiusServerUpdate(name="Updated", host="new.host.com"),
    )
    assert updated.name == "Updated"
    assert updated.host == "new.host.com"


def test_delete_radius_server(db_session):
    """Test deleting a RADIUS server."""
    server = radius_service.radius_servers.create(
        db_session,
        RadiusServerCreate(name="To Delete", host="delete.local"),
    )
    radius_service.radius_servers.delete(db_session, str(server.id))
    db_session.refresh(server)
    assert server.is_active is False


def test_create_radius_client(db_session, radius_server):
    """Test creating a RADIUS client."""
    client = radius_service.radius_clients.create(
        db_session,
        RadiusClientCreate(
            server_id=radius_server.id,
            client_ip="10.0.0.1",
            shared_secret_hash="shared-secret-hash",
        ),
    )
    assert client.server_id == radius_server.id
    assert client.client_ip == "10.0.0.1"


def test_list_radius_clients_by_server(db_session, radius_server):
    """Test listing RADIUS clients by server."""
    radius_service.radius_clients.create(
        db_session,
        RadiusClientCreate(
            server_id=radius_server.id,
            client_ip="10.0.0.1",
            shared_secret_hash="secret1",
        ),
    )
    radius_service.radius_clients.create(
        db_session,
        RadiusClientCreate(
            server_id=radius_server.id,
            client_ip="10.0.0.2",
            shared_secret_hash="secret2",
        ),
    )

    clients = radius_service.radius_clients.list(
        db_session,
        server_id=str(radius_server.id),
        is_active=None,
        order_by="created_at",
        order_dir="asc",
        limit=10,
        offset=0,
    )
    assert len(clients) >= 2
    assert all(c.server_id == radius_server.id for c in clients)


def test_create_radius_sync_job(db_session, radius_server):
    """Test creating a RADIUS sync job."""
    job = radius_service.radius_sync_jobs.create(
        db_session,
        RadiusSyncJobCreate(
            name="Daily Sync",
            server_id=radius_server.id,
        ),
    )
    assert job.name == "Daily Sync"
    assert job.server_id == radius_server.id


def test_list_radius_sync_jobs(db_session, radius_server):
    """Test listing RADIUS sync jobs."""
    radius_service.radius_sync_jobs.create(
        db_session,
        RadiusSyncJobCreate(name="Job 1", server_id=radius_server.id),
    )
    radius_service.radius_sync_jobs.create(
        db_session,
        RadiusSyncJobCreate(name="Job 2", server_id=radius_server.id),
    )

    jobs = radius_service.radius_sync_jobs.list(
        db_session,
        server_id=None,
        is_active=None,
        order_by="created_at",
        order_dir="asc",
        limit=10,
        offset=0,
    )
    assert len(jobs) >= 2


def test_get_radius_server(db_session):
    """Test getting a RADIUS server by ID."""
    server = radius_service.radius_servers.create(
        db_session,
        RadiusServerCreate(
            name="Get Test",
            host="get.test.local",
            description="Test server",
        ),
    )
    fetched = radius_service.radius_servers.get(db_session, str(server.id))
    assert fetched is not None
    assert fetched.id == server.id
    assert fetched.name == "Get Test"


def test_update_radius_client(db_session, radius_server):
    """Test updating a RADIUS client."""
    client = radius_service.radius_clients.create(
        db_session,
        RadiusClientCreate(
            server_id=radius_server.id,
            client_ip="10.0.0.100",
            shared_secret_hash="old-secret",
        ),
    )
    updated = radius_service.radius_clients.update(
        db_session,
        str(client.id),
        RadiusClientUpdate(description="Updated description"),
    )
    assert updated.description == "Updated description"


def test_delete_radius_client(db_session, radius_server):
    """Test deleting a RADIUS client."""
    client = radius_service.radius_clients.create(
        db_session,
        RadiusClientCreate(
            server_id=radius_server.id,
            client_ip="10.0.0.200",
            shared_secret_hash="delete-me",
        ),
    )
    radius_service.radius_clients.delete(db_session, str(client.id))
    db_session.refresh(client)
    assert client.is_active is False


# =============================================================================
# Radius Auth Tests
# =============================================================================


class TestRadiusAuthSettingValue:
    """Tests for _setting_value function."""

    def test_returns_value_text(self, db_session):
        """Test returns value_text when set."""
        setting = DomainSetting(
            domain=SettingDomain.radius,
            key="auth_server_id",
            value_type=SettingValueType.string,
            value_text="server-123",
            is_active=True,
        )
        db_session.add(setting)
        db_session.commit()

        result = radius_auth._setting_value(db_session, "auth_server_id")
        assert result == "server-123"

    def test_returns_value_json_as_string(self, db_session):
        """Test returns value_json as string when value_text is None."""
        setting = DomainSetting(
            domain=SettingDomain.radius,
            key="auth_config",
            value_type=SettingValueType.json,
            value_json={"timeout": 5},
            is_active=True,
        )
        db_session.add(setting)
        db_session.commit()

        result = radius_auth._setting_value(db_session, "auth_config")
        assert "timeout" in result

    def test_returns_none_when_not_found(self, db_session):
        """Test returns None when setting not found."""
        result = radius_auth._setting_value(db_session, "nonexistent_key")
        assert result is None

    def test_ignores_inactive_settings(self, db_session):
        """Test ignores inactive settings."""
        setting = DomainSetting(
            domain=SettingDomain.radius,
            key="inactive_key",
            value_type=SettingValueType.string,
            value_text="value",
            is_active=False,
        )
        db_session.add(setting)
        db_session.commit()

        result = radius_auth._setting_value(db_session, "inactive_key")
        assert result is None


class TestRadiusAuthPickServer:
    """Tests for _pick_radius_server function."""

    def test_picks_server_by_id(self, db_session, radius_server):
        """Test picks specific server by ID."""
        server = radius_auth._pick_radius_server(db_session, str(radius_server.id))
        assert server.id == radius_server.id

    def test_picks_server_from_setting(self, db_session, radius_server):
        """Test picks server from settings when no ID provided."""
        setting = DomainSetting(
            domain=SettingDomain.radius,
            key="auth_server_id",
            value_type=SettingValueType.string,
            value_text=str(radius_server.id),
            is_active=True,
        )
        db_session.add(setting)
        db_session.commit()

        server = radius_auth._pick_radius_server(db_session, None)
        assert server.id == radius_server.id

    def test_picks_most_recent_when_no_preference(self, db_session):
        """Test picks most recent server when no preference."""
        server1 = RadiusServer(name="Old", host="old.local", is_active=True)
        db_session.add(server1)
        db_session.commit()

        server2 = RadiusServer(name="New", host="new.local", is_active=True)
        db_session.add(server2)
        db_session.commit()

        server = radius_auth._pick_radius_server(db_session, None)
        assert server.id == server2.id

    def test_raises_when_no_server_configured(self, db_session):
        """Test raises HTTPException when no server available."""
        # Deactivate all servers
        db_session.query(RadiusServer).update({RadiusServer.is_active: False})
        db_session.commit()

        with pytest.raises(HTTPException) as exc_info:
            radius_auth._pick_radius_server(db_session, None)

        assert exc_info.value.status_code == 400
        assert "not configured" in exc_info.value.detail


class TestRadiusAuthenticate:
    """Tests for authenticate function."""

    def test_raises_without_shared_secret(self, db_session, radius_server):
        """Test raises when shared secret not configured."""
        with pytest.raises(HTTPException) as exc_info:
            radius_auth.authenticate(db_session, "user", "pass", str(radius_server.id))

        assert exc_info.value.status_code == 400
        assert "secret not configured" in exc_info.value.detail

    def test_raises_on_dictionary_error(self, db_session, radius_server):
        """Test raises when dictionary file not available."""
        secret = DomainSetting(
            domain=SettingDomain.radius,
            key="auth_shared_secret",
            value_type=SettingValueType.string,
            value_text="secret123",
            is_active=True,
        )
        db_session.add(secret)
        db_session.commit()

        with pytest.raises(HTTPException) as exc_info:
            radius_auth.authenticate(db_session, "user", "pass", str(radius_server.id))

        assert exc_info.value.status_code == 500
        assert "dictionary" in exc_info.value.detail.lower()

    def test_raises_on_timeout(self, db_session, radius_server):
        """Test raises on RADIUS timeout."""
        secret = DomainSetting(
            domain=SettingDomain.radius,
            key="auth_shared_secret",
            value_type=SettingValueType.string,
            value_text="secret123",
            is_active=True,
        )
        db_session.add(secret)
        db_session.commit()

        mock_dict = MagicMock()
        mock_client = MagicMock()
        mock_client.SendPacket.side_effect = socket.timeout("Timeout")

        with patch("app.services.radius_auth.Dictionary", return_value=mock_dict):
            with patch("app.services.radius_auth.Client", return_value=mock_client):
                with pytest.raises(HTTPException) as exc_info:
                    radius_auth.authenticate(
                        db_session, "user", "pass", str(radius_server.id)
                    )

                assert exc_info.value.status_code == 502
                assert "timeout" in exc_info.value.detail.lower()

    def test_raises_on_send_error(self, db_session, radius_server):
        """Test raises on RADIUS send error."""
        secret = DomainSetting(
            domain=SettingDomain.radius,
            key="auth_shared_secret",
            value_type=SettingValueType.string,
            value_text="secret123",
            is_active=True,
        )
        db_session.add(secret)
        db_session.commit()

        mock_dict = MagicMock()
        mock_client = MagicMock()
        mock_client.SendPacket.side_effect = Exception("Connection refused")

        with patch("app.services.radius_auth.Dictionary", return_value=mock_dict):
            with patch("app.services.radius_auth.Client", return_value=mock_client):
                with pytest.raises(HTTPException) as exc_info:
                    radius_auth.authenticate(
                        db_session, "user", "pass", str(radius_server.id)
                    )

                assert exc_info.value.status_code == 502
                assert "failed" in exc_info.value.detail.lower()

    def test_raises_on_access_reject(self, db_session, radius_server):
        """Test raises on access reject."""
        secret = DomainSetting(
            domain=SettingDomain.radius,
            key="auth_shared_secret",
            value_type=SettingValueType.string,
            value_text="secret123",
            is_active=True,
        )
        db_session.add(secret)
        db_session.commit()

        mock_dict = MagicMock()
        mock_client = MagicMock()
        mock_reply = MagicMock()
        mock_reply.code = 3  # AccessReject
        mock_reply.AccessAccept = 2  # Different from reply.code
        mock_client.SendPacket.return_value = mock_reply

        with patch("app.services.radius_auth.Dictionary", return_value=mock_dict):
            with patch("app.services.radius_auth.Client", return_value=mock_client):
                with pytest.raises(HTTPException) as exc_info:
                    radius_auth.authenticate(
                        db_session, "user", "pass", str(radius_server.id)
                    )

                assert exc_info.value.status_code == 401
                assert "credentials" in exc_info.value.detail.lower()

    def test_success_on_access_accept(self, db_session, radius_server):
        """Test success on access accept."""
        secret = DomainSetting(
            domain=SettingDomain.radius,
            key="auth_shared_secret",
            value_type=SettingValueType.string,
            value_text="secret123",
            is_active=True,
        )
        db_session.add(secret)
        db_session.commit()

        mock_dict = MagicMock()
        mock_client = MagicMock()
        mock_reply = MagicMock()
        mock_reply.code = 2  # AccessAccept
        mock_reply.AccessAccept = 2  # Same as reply.code
        mock_client.SendPacket.return_value = mock_reply
        mock_packet = MagicMock()
        mock_client.CreateAuthPacket.return_value = mock_packet

        with patch("app.services.radius_auth.Dictionary", return_value=mock_dict):
            with patch("app.services.radius_auth.Client", return_value=mock_client):
                # Should not raise
                radius_auth.authenticate(
                    db_session, "testuser", "testpass", str(radius_server.id)
                )
