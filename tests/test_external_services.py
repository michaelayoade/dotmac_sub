"""Tests for external service."""

import uuid

import pytest
from fastapi import HTTPException

from app.models.connector import ConnectorConfig, ConnectorType
from app.models.external import ExternalEntityType, ExternalReference
from app.schemas.external import (
    ExternalReferenceCreate,
    ExternalReferenceSync,
    ExternalReferenceUpdate,
)
from app.services import external as external_service
from app.services.common import apply_ordering, apply_pagination, validate_enum

# =============================================================================
# Helper Function Tests
# =============================================================================


class TestApplyOrdering:
    """Tests for _apply_ordering function."""

    def test_valid_order_by_asc(self, db_session):
        """Test valid order_by with asc direction."""
        query = db_session.query(ExternalReference)
        allowed = {"external_id": ExternalReference.external_id}
        result = apply_ordering(query, "external_id", "asc", allowed)
        assert result is not None

    def test_valid_order_by_desc(self, db_session):
        """Test valid order_by with desc direction."""
        query = db_session.query(ExternalReference)
        allowed = {"external_id": ExternalReference.external_id}
        result = apply_ordering(query, "external_id", "desc", allowed)
        assert result is not None

    def test_invalid_order_by(self, db_session):
        """Test invalid order_by raises HTTPException."""
        query = db_session.query(ExternalReference)
        allowed = {"external_id": ExternalReference.external_id}

        with pytest.raises(HTTPException) as exc_info:
            apply_ordering(query, "invalid_column", "asc", allowed)

        assert exc_info.value.status_code == 400
        assert "Invalid order_by" in exc_info.value.detail


class TestApplyPagination:
    """Tests for _apply_pagination function."""

    def test_applies_limit_and_offset(self, db_session):
        """Test applies limit and offset to query."""
        query = db_session.query(ExternalReference)
        result = apply_pagination(query, 10, 5)
        assert result is not None


class TestValidateEnum:
    """Tests for _validate_enum function."""

    def test_returns_none_for_none(self):
        """Test returns None for None input."""
        result = validate_enum(None, ExternalEntityType, "test")
        assert result is None

    def test_converts_valid_string(self):
        """Test converts valid string to enum."""
        result = validate_enum("ticket", ExternalEntityType, "type")
        assert result == ExternalEntityType.ticket

    def test_invalid_string_raises(self):
        """Test invalid string raises HTTPException."""
        with pytest.raises(HTTPException) as exc_info:
            validate_enum("invalid_type", ExternalEntityType, "type")

        assert exc_info.value.status_code == 400
        assert "Invalid type" in exc_info.value.detail


# =============================================================================
# ExternalReferences CRUD Tests
# =============================================================================


@pytest.fixture
def connector_config(db_session):
    """Create a connector config for tests."""
    config = ConnectorConfig(
        name="Test Connector",
        connector_type=ConnectorType.http,
        is_active=True,
    )
    db_session.add(config)
    db_session.commit()
    return config


class TestExternalReferencesCreate:
    """Tests for ExternalReferences.create."""

    def test_creates_reference(self, db_session, connector_config):
        """Test creates an external reference."""
        ref = external_service.external_references.create(
            db_session,
            ExternalReferenceCreate(
                connector_config_id=connector_config.id,
                entity_type=ExternalEntityType.ticket,
                entity_id=uuid.uuid4(),
                external_id="ext-123",
            ),
        )
        assert ref.connector_config_id == connector_config.id
        assert ref.entity_type == ExternalEntityType.ticket
        assert ref.external_id == "ext-123"
        assert ref.is_active is True

    def test_raises_for_invalid_connector(self, db_session):
        """Test raises for non-existent connector."""
        with pytest.raises(HTTPException) as exc_info:
            external_service.external_references.create(
                db_session,
                ExternalReferenceCreate(
                    connector_config_id=uuid.uuid4(),
                    entity_type=ExternalEntityType.ticket,
                    entity_id=uuid.uuid4(),
                    external_id="ext-456",
                ),
            )

        assert exc_info.value.status_code == 404
        assert "Connector config not found" in exc_info.value.detail


class TestExternalReferencesGet:
    """Tests for ExternalReferences.get."""

    def test_gets_reference_by_id(self, db_session, connector_config):
        """Test gets reference by ID."""
        ref = external_service.external_references.create(
            db_session,
            ExternalReferenceCreate(
                connector_config_id=connector_config.id,
                entity_type=ExternalEntityType.ticket,
                entity_id=uuid.uuid4(),
                external_id="get-test",
            ),
        )
        fetched = external_service.external_references.get(db_session, str(ref.id))
        assert fetched.id == ref.id

    def test_raises_for_not_found(self, db_session):
        """Test raises 404 for non-existent reference."""
        with pytest.raises(HTTPException) as exc_info:
            external_service.external_references.get(db_session, str(uuid.uuid4()))

        assert exc_info.value.status_code == 404
        assert "not found" in exc_info.value.detail


class TestExternalReferencesList:
    """Tests for ExternalReferences.list."""

    def test_lists_active_references(self, db_session, connector_config):
        """Test lists active references by default."""
        external_service.external_references.create(
            db_session,
            ExternalReferenceCreate(
                connector_config_id=connector_config.id,
                entity_type=ExternalEntityType.ticket,
                entity_id=uuid.uuid4(),
                external_id="list-1",
            ),
        )
        external_service.external_references.create(
            db_session,
            ExternalReferenceCreate(
                connector_config_id=connector_config.id,
                entity_type=ExternalEntityType.ticket,
                entity_id=uuid.uuid4(),
                external_id="list-2",
            ),
        )

        refs = external_service.external_references.list(
            db_session,
            connector_config_id=None,
            entity_type=None,
            entity_id=None,
            external_id=None,
            is_active=None,
            order_by="created_at",
            order_dir="asc",
            limit=100,
            offset=0,
        )
        assert len(refs) >= 2
        assert all(r.is_active for r in refs)

    def test_filters_by_connector_config_id(self, db_session, connector_config):
        """Test filters by connector_config_id."""
        external_service.external_references.create(
            db_session,
            ExternalReferenceCreate(
                connector_config_id=connector_config.id,
                entity_type=ExternalEntityType.ticket,
                entity_id=uuid.uuid4(),
                external_id="filter-connector",
            ),
        )

        refs = external_service.external_references.list(
            db_session,
            connector_config_id=str(connector_config.id),
            entity_type=None,
            entity_id=None,
            external_id=None,
            is_active=None,
            order_by="created_at",
            order_dir="asc",
            limit=100,
            offset=0,
        )
        assert all(r.connector_config_id == connector_config.id for r in refs)

    def test_filters_by_entity_type(self, db_session, connector_config):
        """Test filters by entity_type."""
        external_service.external_references.create(
            db_session,
            ExternalReferenceCreate(
                connector_config_id=connector_config.id,
                entity_type=ExternalEntityType.ticket,
                entity_id=uuid.uuid4(),
                external_id="filter-type",
            ),
        )

        refs = external_service.external_references.list(
            db_session,
            connector_config_id=None,
            entity_type="ticket",
            entity_id=None,
            external_id=None,
            is_active=None,
            order_by="created_at",
            order_dir="asc",
            limit=100,
            offset=0,
        )
        assert all(r.entity_type == ExternalEntityType.ticket for r in refs)

    def test_filters_by_entity_id(self, db_session, connector_config):
        """Test filters by entity_id."""
        entity_id = uuid.uuid4()
        external_service.external_references.create(
            db_session,
            ExternalReferenceCreate(
                connector_config_id=connector_config.id,
                entity_type=ExternalEntityType.ticket,
                entity_id=entity_id,
                external_id="filter-entity",
            ),
        )

        refs = external_service.external_references.list(
            db_session,
            connector_config_id=None,
            entity_type=None,
            entity_id=str(entity_id),
            external_id=None,
            is_active=None,
            order_by="created_at",
            order_dir="asc",
            limit=100,
            offset=0,
        )
        assert all(str(r.entity_id) == str(entity_id) for r in refs)

    def test_filters_by_external_id(self, db_session, connector_config):
        """Test filters by external_id."""
        external_service.external_references.create(
            db_session,
            ExternalReferenceCreate(
                connector_config_id=connector_config.id,
                entity_type=ExternalEntityType.ticket,
                entity_id=uuid.uuid4(),
                external_id="unique-external-id",
            ),
        )

        refs = external_service.external_references.list(
            db_session,
            connector_config_id=None,
            entity_type=None,
            entity_id=None,
            external_id="unique-external-id",
            is_active=None,
            order_by="created_at",
            order_dir="asc",
            limit=100,
            offset=0,
        )
        assert all(r.external_id == "unique-external-id" for r in refs)

    def test_filters_by_is_active_false(self, db_session, connector_config):
        """Test filters by is_active=False."""
        ref = external_service.external_references.create(
            db_session,
            ExternalReferenceCreate(
                connector_config_id=connector_config.id,
                entity_type=ExternalEntityType.ticket,
                entity_id=uuid.uuid4(),
                external_id="inactive-ref",
            ),
        )
        external_service.external_references.delete(db_session, str(ref.id))

        refs = external_service.external_references.list(
            db_session,
            connector_config_id=None,
            entity_type=None,
            entity_id=None,
            external_id=None,
            is_active=False,
            order_by="created_at",
            order_dir="asc",
            limit=100,
            offset=0,
        )
        assert all(not r.is_active for r in refs)


class TestExternalReferencesUpdate:
    """Tests for ExternalReferences.update."""

    def test_updates_reference(self, db_session, connector_config):
        """Test updates reference fields."""
        ref = external_service.external_references.create(
            db_session,
            ExternalReferenceCreate(
                connector_config_id=connector_config.id,
                entity_type=ExternalEntityType.ticket,
                entity_id=uuid.uuid4(),
                external_id="update-test",
            ),
        )
        updated = external_service.external_references.update(
            db_session,
            str(ref.id),
            ExternalReferenceUpdate(external_id="updated-external-id"),
        )
        assert updated.external_id == "updated-external-id"

    def test_raises_for_not_found(self, db_session):
        """Test raises 404 for non-existent reference."""
        with pytest.raises(HTTPException) as exc_info:
            external_service.external_references.update(
                db_session, str(uuid.uuid4()), ExternalReferenceUpdate(external_id="new")
            )

        assert exc_info.value.status_code == 404

    def test_validates_connector_on_update(self, db_session, connector_config):
        """Test validates connector exists when updating connector_config_id."""
        ref = external_service.external_references.create(
            db_session,
            ExternalReferenceCreate(
                connector_config_id=connector_config.id,
                entity_type=ExternalEntityType.ticket,
                entity_id=uuid.uuid4(),
                external_id="validate-connector",
            ),
        )

        with pytest.raises(HTTPException) as exc_info:
            external_service.external_references.update(
                db_session,
                str(ref.id),
                ExternalReferenceUpdate(connector_config_id=uuid.uuid4()),
            )

        assert exc_info.value.status_code == 404
        assert "Connector config not found" in exc_info.value.detail


class TestExternalReferencesDelete:
    """Tests for ExternalReferences.delete."""

    def test_soft_deletes_reference(self, db_session, connector_config):
        """Test soft deletes reference."""
        ref = external_service.external_references.create(
            db_session,
            ExternalReferenceCreate(
                connector_config_id=connector_config.id,
                entity_type=ExternalEntityType.ticket,
                entity_id=uuid.uuid4(),
                external_id="delete-test",
            ),
        )
        ref_id = str(ref.id)
        external_service.external_references.delete(db_session, ref_id)
        db_session.refresh(ref)
        assert ref.is_active is False

    def test_raises_for_not_found(self, db_session):
        """Test raises 404 for non-existent reference."""
        with pytest.raises(HTTPException) as exc_info:
            external_service.external_references.delete(db_session, str(uuid.uuid4()))

        assert exc_info.value.status_code == 404


# =============================================================================
# Sync Reference Tests
# =============================================================================


class TestSyncReference:
    """Tests for sync_reference function."""

    def test_creates_new_reference(self, db_session, connector_config):
        """Test creates new reference if not found."""
        entity_id = uuid.uuid4()
        ref = external_service.sync_reference(
            db_session,
            ExternalReferenceSync(
                connector_config_id=connector_config.id,
                entity_type=ExternalEntityType.ticket,
                entity_id=entity_id,
                external_id="sync-new",
            ),
        )
        assert ref.connector_config_id == connector_config.id
        assert ref.external_id == "sync-new"
        assert ref.last_synced_at is not None

    def test_updates_existing_reference(self, db_session, connector_config):
        """Test updates existing reference if found."""
        entity_id = uuid.uuid4()
        # Create initial reference
        external_service.sync_reference(
            db_session,
            ExternalReferenceSync(
                connector_config_id=connector_config.id,
                entity_type=ExternalEntityType.ticket,
                entity_id=entity_id,
                external_id="sync-existing",
            ),
        )
        # Sync again with updated external_id
        ref = external_service.sync_reference(
            db_session,
            ExternalReferenceSync(
                connector_config_id=connector_config.id,
                entity_type=ExternalEntityType.ticket,
                entity_id=entity_id,
                external_id="sync-updated",
            ),
        )
        assert ref.external_id == "sync-updated"

    def test_raises_for_invalid_connector(self, db_session):
        """Test raises for non-existent connector."""
        with pytest.raises(HTTPException) as exc_info:
            external_service.sync_reference(
                db_session,
                ExternalReferenceSync(
                    connector_config_id=uuid.uuid4(),
                    entity_type=ExternalEntityType.ticket,
                    entity_id=uuid.uuid4(),
                    external_id="sync-invalid",
                ),
            )

        assert exc_info.value.status_code == 404
        assert "Connector config not found" in exc_info.value.detail
