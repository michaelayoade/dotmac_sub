"""Tests for analytics service."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.analytics import KPIConfig
from app.schemas.analytics import KPIAggregateCreate, KPIConfigCreate, KPIConfigUpdate
from app.services import analytics as analytics_service
from app.services.common import apply_ordering, apply_pagination

# =============================================================================
# Helper Function Tests
# =============================================================================


class TestApplyOrdering:
    """Tests for _apply_ordering function."""

    def test_valid_order_by_asc(self, db_session):
        """Test valid order_by with asc direction."""
        query = db_session.query(KPIConfig)
        allowed = {"key": KPIConfig.key, "created_at": KPIConfig.created_at}
        result = apply_ordering(query, "key", "asc", allowed)
        assert result is not None

    def test_valid_order_by_desc(self, db_session):
        """Test valid order_by with desc direction."""
        query = db_session.query(KPIConfig)
        allowed = {"key": KPIConfig.key, "created_at": KPIConfig.created_at}
        result = apply_ordering(query, "key", "desc", allowed)
        assert result is not None

    def test_invalid_order_by(self, db_session):
        """Test invalid order_by raises HTTPException."""
        query = db_session.query(KPIConfig)
        allowed = {"key": KPIConfig.key}

        with pytest.raises(HTTPException) as exc_info:
            apply_ordering(query, "invalid_column", "asc", allowed)

        assert exc_info.value.status_code == 400
        assert "Invalid order_by" in exc_info.value.detail


class TestApplyPagination:
    """Tests for _apply_pagination function."""

    def test_applies_limit_and_offset(self, db_session):
        """Test applies limit and offset to query."""
        query = db_session.query(KPIConfig)
        result = apply_pagination(query, 10, 5)
        assert result is not None


# =============================================================================
# KPIConfigs CRUD Tests
# =============================================================================


class TestKPIConfigsCreate:
    """Tests for KPIConfigs.create."""

    def test_creates_config(self, db_session):
        """Test creates a KPI config."""
        config = analytics_service.kpi_configs.create(
            db_session,
            KPIConfigCreate(
                key="revenue_target",
                name="Revenue Target",
            ),
        )
        assert config.key == "revenue_target"
        assert config.name == "Revenue Target"
        assert config.is_active is True


class TestKPIConfigsGet:
    """Tests for KPIConfigs.get."""

    def test_gets_config_by_id(self, db_session):
        """Test gets config by ID."""
        config = analytics_service.kpi_configs.create(
            db_session,
            KPIConfigCreate(key="get_test", name="Get Test"),
        )
        fetched = analytics_service.kpi_configs.get(db_session, str(config.id))
        assert fetched.id == config.id

    def test_raises_for_not_found(self, db_session):
        """Test raises 404 for non-existent config."""
        with pytest.raises(HTTPException) as exc_info:
            analytics_service.kpi_configs.get(db_session, str(uuid.uuid4()))

        assert exc_info.value.status_code == 404
        assert "not found" in exc_info.value.detail


class TestKPIConfigsList:
    """Tests for KPIConfigs.list."""

    def test_lists_active_configs(self, db_session):
        """Test lists active configs by default."""
        analytics_service.kpi_configs.create(
            db_session,
            KPIConfigCreate(key="list_test_1", name="List Test 1"),
        )
        analytics_service.kpi_configs.create(
            db_session,
            KPIConfigCreate(key="list_test_2", name="List Test 2"),
        )

        configs = analytics_service.kpi_configs.list(
            db_session,
            is_active=None,
            order_by="created_at",
            order_dir="asc",
            limit=100,
            offset=0,
        )
        assert len(configs) >= 2
        assert all(c.is_active for c in configs)

    def test_filters_by_is_active_false(self, db_session):
        """Test filters by is_active=False."""
        config = analytics_service.kpi_configs.create(
            db_session,
            KPIConfigCreate(key="inactive_test", name="Inactive Test"),
        )
        analytics_service.kpi_configs.delete(db_session, str(config.id))

        configs = analytics_service.kpi_configs.list(
            db_session,
            is_active=False,
            order_by="created_at",
            order_dir="asc",
            limit=100,
            offset=0,
        )
        assert all(not c.is_active for c in configs)


class TestKPIConfigsUpdate:
    """Tests for KPIConfigs.update."""

    def test_updates_config(self, db_session):
        """Test updates config fields."""
        config = analytics_service.kpi_configs.create(
            db_session,
            KPIConfigCreate(key="update_test", name="Update Test"),
        )
        updated = analytics_service.kpi_configs.update(
            db_session,
            str(config.id),
            KPIConfigUpdate(name="Updated Name"),
        )
        assert updated.name == "Updated Name"

    def test_raises_for_not_found(self, db_session):
        """Test raises 404 for non-existent config."""
        with pytest.raises(HTTPException) as exc_info:
            analytics_service.kpi_configs.update(
                db_session, str(uuid.uuid4()), KPIConfigUpdate(name="new")
            )

        assert exc_info.value.status_code == 404


class TestKPIConfigsDelete:
    """Tests for KPIConfigs.delete."""

    def test_soft_deletes_config(self, db_session):
        """Test soft deletes config."""
        config = analytics_service.kpi_configs.create(
            db_session,
            KPIConfigCreate(key="delete_test", name="Delete Test"),
        )
        config_id = str(config.id)
        analytics_service.kpi_configs.delete(db_session, config_id)
        db_session.refresh(config)
        assert config.is_active is False

    def test_raises_for_not_found(self, db_session):
        """Test raises 404 for non-existent config."""
        with pytest.raises(HTTPException) as exc_info:
            analytics_service.kpi_configs.delete(db_session, str(uuid.uuid4()))

        assert exc_info.value.status_code == 404


# =============================================================================
# KPIAggregates CRUD Tests
# =============================================================================


class TestKPIAggregatesCreate:
    """Tests for KPIAggregates.create."""

    def test_creates_aggregate(self, db_session):
        """Test creates a KPI aggregate."""
        now = datetime.now(UTC)
        aggregate = analytics_service.kpi_aggregates.create(
            db_session,
            KPIAggregateCreate(
                key="monthly_revenue",
                value=Decimal("50000.00"),
                period_start=now,
                period_end=now,
            ),
        )
        assert aggregate.key == "monthly_revenue"
        assert aggregate.value == Decimal("50000.00")


class TestKPIAggregatesGet:
    """Tests for KPIAggregates.get."""

    def test_gets_aggregate_by_id(self, db_session):
        """Test gets aggregate by ID."""
        now = datetime.now(UTC)
        aggregate = analytics_service.kpi_aggregates.create(
            db_session,
            KPIAggregateCreate(
                key="get_agg_test",
                value=Decimal("100.00"),
                period_start=now,
                period_end=now,
            ),
        )
        fetched = analytics_service.kpi_aggregates.get(db_session, str(aggregate.id))
        assert fetched.id == aggregate.id

    def test_raises_for_not_found(self, db_session):
        """Test raises 404 for non-existent aggregate."""
        with pytest.raises(HTTPException) as exc_info:
            analytics_service.kpi_aggregates.get(db_session, str(uuid.uuid4()))

        assert exc_info.value.status_code == 404
        assert "not found" in exc_info.value.detail


class TestKPIAggregatesList:
    """Tests for KPIAggregates.list."""

    def test_lists_all_aggregates(self, db_session):
        """Test lists all aggregates."""
        now = datetime.now(UTC)
        analytics_service.kpi_aggregates.create(
            db_session,
            KPIAggregateCreate(
                key="list_agg_1",
                value=Decimal("10.00"),
                period_start=now,
                period_end=now,
            ),
        )
        analytics_service.kpi_aggregates.create(
            db_session,
            KPIAggregateCreate(
                key="list_agg_2",
                value=Decimal("20.00"),
                period_start=now,
                period_end=now,
            ),
        )

        aggregates = analytics_service.kpi_aggregates.list(
            db_session,
            key=None,
            order_by="created_at",
            order_dir="asc",
            limit=100,
            offset=0,
        )
        assert len(aggregates) >= 2

    def test_filters_by_key(self, db_session):
        """Test filters by key."""
        now = datetime.now(UTC)
        analytics_service.kpi_aggregates.create(
            db_session,
            KPIAggregateCreate(
                key="filter_key_test",
                value=Decimal("30.00"),
                period_start=now,
                period_end=now,
            ),
        )

        aggregates = analytics_service.kpi_aggregates.list(
            db_session,
            key="filter_key_test",
            order_by="created_at",
            order_dir="asc",
            limit=100,
            offset=0,
        )
        assert all(a.key == "filter_key_test" for a in aggregates)


# =============================================================================
# compute_kpis Tests
# =============================================================================


def test_compute_kpis_returns_empty_after_crm_removal(db_session):
    """Test compute_kpis returns empty list after CRM/SLA removal."""
    kpis = analytics_service.compute_kpis(db_session)
    assert kpis == []
