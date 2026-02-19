"""Tests for subscription engine services."""

import uuid

import pytest
from fastapi import HTTPException

from app.schemas.subscription_engine import (
    SubscriptionEngineCreate,
    SubscriptionEngineSettingCreate,
    SubscriptionEngineSettingUpdate,
    SubscriptionEngineUpdate,
)
from app.services import subscription_engine
from app.services.common import apply_ordering

# =============================================================================
# Helper Functions Tests
# =============================================================================


class TestApplyOrdering:
    """Tests for _apply_ordering helper."""

    def test_invalid_order_by(self, db_session):
        """Test invalid order_by raises HTTPException."""
        from app.models.subscription_engine import SubscriptionEngine

        query = db_session.query(SubscriptionEngine)

        with pytest.raises(HTTPException) as exc_info:
            apply_ordering(
                query,
                order_by="invalid_column",
                order_dir="asc",
                allowed_columns={"name": SubscriptionEngine.name},
            )

        assert exc_info.value.status_code == 400
        assert "Invalid order_by" in exc_info.value.detail

    def test_valid_order_by_asc(self, db_session):
        """Test valid order_by with asc direction."""
        from app.models.subscription_engine import SubscriptionEngine

        query = db_session.query(SubscriptionEngine)

        result = apply_ordering(
            query,
            order_by="name",
            order_dir="asc",
            allowed_columns={"name": SubscriptionEngine.name},
        )

        # Should return a query without error
        assert result is not None

    def test_valid_order_by_desc(self, db_session):
        """Test valid order_by with desc direction."""
        from app.models.subscription_engine import SubscriptionEngine

        query = db_session.query(SubscriptionEngine)

        result = apply_ordering(
            query,
            order_by="name",
            order_dir="desc",
            allowed_columns={"name": SubscriptionEngine.name},
        )

        assert result is not None


# =============================================================================
# Engines CRUD Tests
# =============================================================================


class TestEngines:
    """Tests for Engines service class."""

    def test_create_engine(self, db_session):
        """Test creating a subscription engine."""
        payload = SubscriptionEngineCreate(
            name="Test Engine",
            code="TEST-ENGINE",
            description="A test engine",
        )

        result = subscription_engine.engines.create(db_session, payload)

        assert result.id is not None
        assert result.name == "Test Engine"
        assert result.code == "TEST-ENGINE"
        assert result.description == "A test engine"
        assert result.is_active is True

    def test_get_engine_success(self, db_session):
        """Test getting an existing engine."""
        payload = SubscriptionEngineCreate(
            name="Engine to Get",
            code="GET-ENGINE",
        )
        created = subscription_engine.engines.create(db_session, payload)

        result = subscription_engine.engines.get(db_session, str(created.id))

        assert result.id == created.id
        assert result.name == "Engine to Get"

    def test_get_engine_not_found(self, db_session):
        """Test getting a non-existent engine."""
        with pytest.raises(HTTPException) as exc_info:
            subscription_engine.engines.get(db_session, str(uuid.uuid4()))

        assert exc_info.value.status_code == 404
        assert "not found" in exc_info.value.detail

    def test_list_engines_default_active(self, db_session):
        """Test listing engines filters to active by default."""
        # Create active engine
        active = subscription_engine.engines.create(
            db_session,
            SubscriptionEngineCreate(name="Active Engine", code="ACTIVE-1"),
        )
        # Create inactive engine
        inactive = subscription_engine.engines.create(
            db_session,
            SubscriptionEngineCreate(name="Inactive Engine", code="INACTIVE-1"),
        )
        inactive.is_active = False
        db_session.commit()

        results = subscription_engine.engines.list(
            db_session,
            is_active=None,  # Default behavior
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )

        # Should only include active engines
        result_ids = [str(e.id) for e in results]
        assert str(active.id) in result_ids
        assert str(inactive.id) not in result_ids

    def test_list_engines_filter_inactive(self, db_session):
        """Test listing only inactive engines."""
        # Create active engine
        active = subscription_engine.engines.create(
            db_session,
            SubscriptionEngineCreate(name="Active Engine 2", code="ACTIVE-2"),
        )
        # Create inactive engine
        inactive = subscription_engine.engines.create(
            db_session,
            SubscriptionEngineCreate(name="Inactive Engine 2", code="INACTIVE-2"),
        )
        inactive.is_active = False
        db_session.commit()

        results = subscription_engine.engines.list(
            db_session,
            is_active=False,
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )

        result_ids = [str(e.id) for e in results]
        assert str(inactive.id) in result_ids

    def test_update_engine_success(self, db_session):
        """Test updating an engine."""
        created = subscription_engine.engines.create(
            db_session,
            SubscriptionEngineCreate(name="Original Name", code="UPDATE-ENGINE"),
        )

        payload = SubscriptionEngineUpdate(name="Updated Name")
        result = subscription_engine.engines.update(db_session, str(created.id), payload)

        assert result.name == "Updated Name"
        assert result.code == "UPDATE-ENGINE"  # Unchanged

    def test_update_engine_not_found(self, db_session):
        """Test updating a non-existent engine."""
        payload = SubscriptionEngineUpdate(name="New Name")

        with pytest.raises(HTTPException) as exc_info:
            subscription_engine.engines.update(db_session, str(uuid.uuid4()), payload)

        assert exc_info.value.status_code == 404

    def test_delete_engine_soft_delete(self, db_session):
        """Test deleting an engine (soft delete)."""
        created = subscription_engine.engines.create(
            db_session,
            SubscriptionEngineCreate(name="To Delete", code="DELETE-ENGINE"),
        )
        assert created.is_active is True

        subscription_engine.engines.delete(db_session, str(created.id))

        db_session.refresh(created)
        assert created.is_active is False

    def test_delete_engine_not_found(self, db_session):
        """Test deleting a non-existent engine."""
        with pytest.raises(HTTPException) as exc_info:
            subscription_engine.engines.delete(db_session, str(uuid.uuid4()))

        assert exc_info.value.status_code == 404


# =============================================================================
# Engine Settings CRUD Tests
# =============================================================================


class TestEngineSettings:
    """Tests for EngineSettings service class."""

    @pytest.fixture
    def test_engine(self, db_session):
        """Create a test engine for settings."""
        return subscription_engine.engines.create(
            db_session,
            SubscriptionEngineCreate(name="Settings Engine", code="SETTINGS-ENG"),
        )

    def test_create_setting(self, db_session, test_engine):
        """Test creating an engine setting."""
        payload = SubscriptionEngineSettingCreate(
            engine_id=test_engine.id,
            key="api_url",
            value_text="https://api.example.com",
        )

        result = subscription_engine.engine_settings.create(db_session, payload)

        assert result.id is not None
        assert result.engine_id == test_engine.id
        assert result.key == "api_url"
        assert result.value_text == "https://api.example.com"

    def test_get_setting_success(self, db_session, test_engine):
        """Test getting an existing setting."""
        created = subscription_engine.engine_settings.create(
            db_session,
            SubscriptionEngineSettingCreate(
                engine_id=test_engine.id,
                key="test_key",
                value_text="test_value",
            ),
        )

        result = subscription_engine.engine_settings.get(db_session, str(created.id))

        assert result.id == created.id
        assert result.key == "test_key"

    def test_get_setting_not_found(self, db_session):
        """Test getting a non-existent setting."""
        with pytest.raises(HTTPException) as exc_info:
            subscription_engine.engine_settings.get(db_session, str(uuid.uuid4()))

        assert exc_info.value.status_code == 404
        assert "not found" in exc_info.value.detail

    def test_list_settings_all(self, db_session, test_engine):
        """Test listing all settings."""
        subscription_engine.engine_settings.create(
            db_session,
            SubscriptionEngineSettingCreate(
                engine_id=test_engine.id,
                key="key_1",
                value_text="value_1",
            ),
        )
        subscription_engine.engine_settings.create(
            db_session,
            SubscriptionEngineSettingCreate(
                engine_id=test_engine.id,
                key="key_2",
                value_text="value_2",
            ),
        )

        results = subscription_engine.engine_settings.list(
            db_session,
            engine_id=None,
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )

        assert len(results) >= 2

    def test_list_settings_by_engine(self, db_session, test_engine):
        """Test listing settings filtered by engine."""
        # Create settings for our test engine
        setting = subscription_engine.engine_settings.create(
            db_session,
            SubscriptionEngineSettingCreate(
                engine_id=test_engine.id,
                key="filtered_key",
                value_text="filtered_value",
            ),
        )

        # Create another engine with settings
        other_engine = subscription_engine.engines.create(
            db_session,
            SubscriptionEngineCreate(name="Other Engine", code="OTHER-ENG"),
        )
        subscription_engine.engine_settings.create(
            db_session,
            SubscriptionEngineSettingCreate(
                engine_id=other_engine.id,
                key="other_key",
                value_text="other_value",
            ),
        )

        results = subscription_engine.engine_settings.list(
            db_session,
            engine_id=str(test_engine.id),
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )

        # Should only include settings for test_engine
        result_engine_ids = [str(s.engine_id) for s in results]
        assert all(eid == str(test_engine.id) for eid in result_engine_ids)

    def test_update_setting_success(self, db_session, test_engine):
        """Test updating a setting."""
        created = subscription_engine.engine_settings.create(
            db_session,
            SubscriptionEngineSettingCreate(
                engine_id=test_engine.id,
                key="update_key",
                value_text="original_value",
            ),
        )

        payload = SubscriptionEngineSettingUpdate(value_text="updated_value")
        result = subscription_engine.engine_settings.update(db_session, str(created.id), payload)

        assert result.value_text == "updated_value"
        assert result.key == "update_key"  # Unchanged

    def test_update_setting_not_found(self, db_session):
        """Test updating a non-existent setting."""
        payload = SubscriptionEngineSettingUpdate(value_text="new_value")

        with pytest.raises(HTTPException) as exc_info:
            subscription_engine.engine_settings.update(db_session, str(uuid.uuid4()), payload)

        assert exc_info.value.status_code == 404

    def test_delete_setting_hard_delete(self, db_session, test_engine):
        """Test deleting a setting (hard delete)."""
        created = subscription_engine.engine_settings.create(
            db_session,
            SubscriptionEngineSettingCreate(
                engine_id=test_engine.id,
                key="delete_key",
                value_text="delete_value",
            ),
        )
        setting_id = str(created.id)

        subscription_engine.engine_settings.delete(db_session, setting_id)

        # Should no longer exist
        with pytest.raises(HTTPException) as exc_info:
            subscription_engine.engine_settings.get(db_session, setting_id)

        assert exc_info.value.status_code == 404

    def test_delete_setting_not_found(self, db_session):
        """Test deleting a non-existent setting."""
        with pytest.raises(HTTPException) as exc_info:
            subscription_engine.engine_settings.delete(db_session, str(uuid.uuid4()))

        assert exc_info.value.status_code == 404


# =============================================================================
# Module Alias Tests
# =============================================================================


def test_module_aliases():
    """Test that module aliases are correctly set."""
    assert subscription_engine.subscription_engines is subscription_engine.engines
    assert subscription_engine.subscription_engine_settings is subscription_engine.engine_settings
