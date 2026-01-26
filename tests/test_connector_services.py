"""Tests for connector service."""

import uuid

import pytest
from fastapi import HTTPException

from app.models.connector import ConnectorConfig, ConnectorAuthType, ConnectorType
from app.schemas.connector import ConnectorConfigCreate, ConnectorConfigUpdate
from app.services import connector as connector_service
from app.services.common import apply_ordering, apply_pagination, validate_enum


# =============================================================================
# Helper Function Tests
# =============================================================================


class TestApplyOrdering:
    """Tests for _apply_ordering function."""

    def test_valid_order_by_asc(self, db_session):
        """Test valid order_by with asc direction."""
        query = db_session.query(ConnectorConfig)
        allowed = {"name": ConnectorConfig.name, "created_at": ConnectorConfig.created_at}
        result = apply_ordering(query, "name", "asc", allowed)
        assert result is not None

    def test_valid_order_by_desc(self, db_session):
        """Test valid order_by with desc direction."""
        query = db_session.query(ConnectorConfig)
        allowed = {"name": ConnectorConfig.name, "created_at": ConnectorConfig.created_at}
        result = apply_ordering(query, "name", "desc", allowed)
        assert result is not None

    def test_invalid_order_by(self, db_session):
        """Test invalid order_by raises HTTPException."""
        query = db_session.query(ConnectorConfig)
        allowed = {"name": ConnectorConfig.name}

        with pytest.raises(HTTPException) as exc_info:
            apply_ordering(query, "invalid_column", "asc", allowed)

        assert exc_info.value.status_code == 400
        assert "Invalid order_by" in exc_info.value.detail


class TestApplyPagination:
    """Tests for _apply_pagination function."""

    def test_applies_limit_and_offset(self, db_session):
        """Test applies limit and offset to query."""
        query = db_session.query(ConnectorConfig)
        result = apply_pagination(query, 10, 5)
        assert result is not None


class TestValidateEnum:
    """Tests for _validate_enum function."""

    def test_returns_none_for_none(self):
        """Test returns None for None input."""
        result = validate_enum(None, ConnectorType, "test")
        assert result is None

    def test_converts_valid_string(self):
        """Test converts valid string to enum."""
        result = validate_enum("http", ConnectorType, "type")
        assert result == ConnectorType.http

    def test_invalid_string_raises(self):
        """Test invalid string raises HTTPException."""
        with pytest.raises(HTTPException) as exc_info:
            validate_enum("invalid_type", ConnectorType, "type")

        assert exc_info.value.status_code == 400
        assert "Invalid type" in exc_info.value.detail


# =============================================================================
# ConnectorConfigs CRUD Tests
# =============================================================================


class TestConnectorConfigsCreate:
    """Tests for ConnectorConfigs.create."""

    def test_creates_config(self, db_session):
        """Test creates a connector config."""
        config = connector_service.connector_configs.create(
            db_session,
            ConnectorConfigCreate(
                name="Test Connector",
                connector_type=ConnectorType.http,
                auth_type=ConnectorAuthType.basic,
            ),
        )
        assert config.name == "Test Connector"
        assert config.connector_type == ConnectorType.http
        assert config.auth_type == ConnectorAuthType.basic
        assert config.is_active is True


class TestConnectorConfigsGet:
    """Tests for ConnectorConfigs.get."""

    def test_gets_config_by_id(self, db_session):
        """Test gets config by ID."""
        config = connector_service.connector_configs.create(
            db_session,
            ConnectorConfigCreate(
                name="Get Test",
                connector_type=ConnectorType.http,
            ),
        )
        fetched = connector_service.connector_configs.get(db_session, str(config.id))
        assert fetched.id == config.id

    def test_raises_for_not_found(self, db_session):
        """Test raises 404 for non-existent config."""
        with pytest.raises(HTTPException) as exc_info:
            connector_service.connector_configs.get(db_session, str(uuid.uuid4()))

        assert exc_info.value.status_code == 404
        assert "not found" in exc_info.value.detail


class TestConnectorConfigsList:
    """Tests for ConnectorConfigs.list."""

    def test_lists_all_active_configs(self, db_session):
        """Test lists all active configs."""
        connector_service.connector_configs.create(
            db_session,
            ConnectorConfigCreate(
                name="List Test 1",
                connector_type=ConnectorType.http,
            ),
        )
        connector_service.connector_configs.create(
            db_session,
            ConnectorConfigCreate(
                name="List Test 2",
                connector_type=ConnectorType.http,
            ),
        )

        configs = connector_service.connector_configs.list(
            db_session,
            connector_type=None,
            auth_type=None,
            is_active=None,
            order_by="created_at",
            order_dir="asc",
            limit=100,
            offset=0,
        )
        assert len(configs) >= 2

    def test_filters_by_connector_type(self, db_session):
        """Test filters by connector type."""
        connector_service.connector_configs.create(
            db_session,
            ConnectorConfigCreate(
                name="Radius Config",
                connector_type=ConnectorType.http,
            ),
        )

        configs = connector_service.connector_configs.list(
            db_session,
            connector_type="http",
            auth_type=None,
            is_active=None,
            order_by="created_at",
            order_dir="asc",
            limit=100,
            offset=0,
        )
        assert all(c.connector_type == ConnectorType.http for c in configs)

    def test_filters_by_auth_type(self, db_session):
        """Test filters by auth type."""
        connector_service.connector_configs.create(
            db_session,
            ConnectorConfigCreate(
                name="Auth Type Config",
                connector_type=ConnectorType.http,
                auth_type=ConnectorAuthType.basic,
            ),
        )

        configs = connector_service.connector_configs.list(
            db_session,
            connector_type=None,
            auth_type="basic",
            is_active=None,
            order_by="created_at",
            order_dir="asc",
            limit=100,
            offset=0,
        )
        assert all(c.auth_type == ConnectorAuthType.basic for c in configs)

    def test_filters_by_is_active_false(self, db_session):
        """Test filters by is_active=False."""
        config = connector_service.connector_configs.create(
            db_session,
            ConnectorConfigCreate(
                name="Inactive Config",
                connector_type=ConnectorType.http,
            ),
        )
        connector_service.connector_configs.delete(db_session, str(config.id))

        configs = connector_service.connector_configs.list(
            db_session,
            connector_type=None,
            auth_type=None,
            is_active=False,
            order_by="created_at",
            order_dir="asc",
            limit=100,
            offset=0,
        )
        assert all(not c.is_active for c in configs)


class TestConnectorConfigsListAll:
    """Tests for ConnectorConfigs.list_all."""

    def test_lists_all_configs_including_inactive(self, db_session):
        """Test lists all configs including inactive."""
        active = connector_service.connector_configs.create(
            db_session,
            ConnectorConfigCreate(
                name="Active Config",
                connector_type=ConnectorType.http,
            ),
        )
        inactive = connector_service.connector_configs.create(
            db_session,
            ConnectorConfigCreate(
                name="Inactive Config",
                connector_type=ConnectorType.http,
            ),
        )
        connector_service.connector_configs.delete(db_session, str(inactive.id))

        configs = connector_service.connector_configs.list_all(
            db_session,
            connector_type=None,
            auth_type=None,
            order_by="created_at",
            order_dir="asc",
            limit=100,
            offset=0,
        )
        config_ids = [c.id for c in configs]
        assert active.id in config_ids
        assert inactive.id in config_ids

    def test_filters_by_connector_type(self, db_session):
        """Test list_all filters by connector type."""
        connector_service.connector_configs.create(
            db_session,
            ConnectorConfigCreate(
                name="Radius All Config",
                connector_type=ConnectorType.http,
            ),
        )

        configs = connector_service.connector_configs.list_all(
            db_session,
            connector_type="http",
            auth_type=None,
            order_by="created_at",
            order_dir="asc",
            limit=100,
            offset=0,
        )
        assert all(c.connector_type == ConnectorType.http for c in configs)

    def test_filters_by_auth_type(self, db_session):
        """Test list_all filters by auth type."""
        connector_service.connector_configs.create(
            db_session,
            ConnectorConfigCreate(
                name="Auth Type All Config",
                connector_type=ConnectorType.http,
                auth_type=ConnectorAuthType.basic,
            ),
        )

        configs = connector_service.connector_configs.list_all(
            db_session,
            connector_type=None,
            auth_type="basic",
            order_by="created_at",
            order_dir="asc",
            limit=100,
            offset=0,
        )
        assert all(c.auth_type == ConnectorAuthType.basic for c in configs)


class TestConnectorConfigsUpdate:
    """Tests for ConnectorConfigs.update."""

    def test_updates_config(self, db_session):
        """Test updates config fields."""
        config = connector_service.connector_configs.create(
            db_session,
            ConnectorConfigCreate(
                name="Update Test",
                connector_type=ConnectorType.http,
            ),
        )
        updated = connector_service.connector_configs.update(
            db_session,
            str(config.id),
            ConnectorConfigUpdate(name="Updated Name"),
        )
        assert updated.name == "Updated Name"

    def test_raises_for_not_found(self, db_session):
        """Test raises 404 for non-existent config."""
        with pytest.raises(HTTPException) as exc_info:
            connector_service.connector_configs.update(
                db_session, str(uuid.uuid4()), ConnectorConfigUpdate(name="new")
            )

        assert exc_info.value.status_code == 404

    def test_merges_auth_config(self, db_session):
        """Test merges auth_config instead of replacing."""
        config = connector_service.connector_configs.create(
            db_session,
            ConnectorConfigCreate(
                name="Auth Config Test",
                connector_type=ConnectorType.http,
                auth_config={"key1": "value1"},
            ),
        )
        updated = connector_service.connector_configs.update(
            db_session,
            str(config.id),
            ConnectorConfigUpdate(auth_config={"key2": "value2"}),
        )
        assert updated.auth_config["key1"] == "value1"
        assert updated.auth_config["key2"] == "value2"


class TestConnectorConfigsDelete:
    """Tests for ConnectorConfigs.delete."""

    def test_soft_deletes_config(self, db_session):
        """Test soft deletes config (sets is_active=False)."""
        config = connector_service.connector_configs.create(
            db_session,
            ConnectorConfigCreate(
                name="Delete Test",
                connector_type=ConnectorType.http,
            ),
        )
        config_id = str(config.id)
        connector_service.connector_configs.delete(db_session, config_id)
        db_session.refresh(config)
        assert config.is_active is False

    def test_raises_for_not_found(self, db_session):
        """Test raises 404 for non-existent config."""
        with pytest.raises(HTTPException) as exc_info:
            connector_service.connector_configs.delete(db_session, str(uuid.uuid4()))

        assert exc_info.value.status_code == 404
