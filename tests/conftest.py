import os
import sqlite3
import uuid
from datetime import UTC
from typing import Any

import pytest
from geoalchemy2 import Geometry
from geoalchemy2.admin.dialects import sqlite as geoalchemy_sqlite_admin
from sqlalchemy import String, TypeDecorator, create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base


class _JoseDateTimeProxy:
    def utcnow(self):
        from datetime import datetime

        return datetime.now(UTC)

    def now(self, tz: Any | None = None):
        from datetime import datetime

        return datetime.now(tz)

    def __getattr__(self, name: str) -> Any:
        from datetime import datetime

        return getattr(datetime, name)


@pytest.fixture(autouse=True)
def _patch_jose_datetime(monkeypatch):
    import jose.jwt as jose_jwt

    monkeypatch.setattr(jose_jwt, "datetime", _JoseDateTimeProxy(), raising=False)

# Register UUID adapter for SQLite - store as string
sqlite3.register_adapter(uuid.UUID, lambda u: str(u))


class SQLiteUUID(TypeDecorator):
    """UUID type that works with SQLite by storing as string."""

    impl = String(36)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None:
            if isinstance(value, uuid.UUID):
                return str(value)
            return value
        return None

    def process_result_value(self, value, dialect):
        if value is not None:
            if not isinstance(value, uuid.UUID):
                return uuid.UUID(value)
            return value
        return None


# Monkey-patch SQLAlchemy's UUID type for SQLite compatibility
# This must happen before any models are imported
from sqlalchemy.sql import sqltypes

_original_uuid_bind_processor = sqltypes.Uuid.bind_processor
_original_uuid_result_processor = sqltypes.Uuid.result_processor


def _sqlite_uuid_bind_processor(self, dialect):
    if dialect.name == "sqlite":
        def process(value):
            if value is not None:
                if isinstance(value, uuid.UUID):
                    return str(value)
                return str(uuid.UUID(value)) if value else None
            return None
        return process
    return _original_uuid_bind_processor(self, dialect)


def _sqlite_uuid_result_processor(self, dialect, coltype):
    if dialect.name == "sqlite":
        def process(value):
            if value is not None:
                if isinstance(value, uuid.UUID):
                    return value
                return uuid.UUID(value) if value else None
            return None
        return process
    return _original_uuid_result_processor(self, dialect, coltype)


sqltypes.Uuid.bind_processor = _sqlite_uuid_bind_processor  # type: ignore[method-assign]
sqltypes.Uuid.result_processor = _sqlite_uuid_result_processor  # type: ignore[method-assign]


# Monkey-patch PostgreSQL JSONB type for SQLite compatibility
# SQLite uses JSON instead of JSONB

_original_jsonb_compile = None
_original_geometry_bind_expression = Geometry.bind_expression
_original_geometry_column_expression = Geometry.column_expression
_original_geometry_result_processor = Geometry.result_processor


def _patch_jsonb_for_sqlite():
    """Make JSONB compile as JSON for SQLite dialect."""
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

    if not hasattr(SQLiteTypeCompiler, '_original_visit_JSONB'):
        # Store original if it exists, otherwise create a fallback
        if hasattr(SQLiteTypeCompiler, 'visit_JSONB'):
            SQLiteTypeCompiler._original_visit_JSONB = SQLiteTypeCompiler.visit_JSONB

        def visit_JSONB(self, type_, **kw):
            return self.visit_JSON(type_, **kw)

        SQLiteTypeCompiler.visit_JSONB = visit_JSONB


_patch_jsonb_for_sqlite()


def _enable_sqlite_geometry_passthrough() -> None:
    """Avoid Spatialite-only wrappers when tests run on plain SQLite."""

    def _bind_expression(self, bindvalue):
        return bindvalue

    def _column_expression(self, col):
        return col

    def _result_processor(self, dialect, coltype):
        if dialect.name == "sqlite":
            return lambda value: value
        return _original_geometry_result_processor(self, dialect, coltype)

    Geometry.bind_expression = _bind_expression
    Geometry.column_expression = _column_expression
    Geometry.result_processor = _result_processor


def _restore_sqlite_geometry_passthrough() -> None:
    Geometry.bind_expression = _original_geometry_bind_expression
    Geometry.column_expression = _original_geometry_column_expression
    Geometry.result_processor = _original_geometry_result_processor


def _disable_sqlite_spatial_admin() -> None:
    """Avoid GeoAlchemy Spatialite admin calls when mod_spatialite is unavailable."""

    def _noop(*args, **kwargs):
        return None

    geoalchemy_sqlite_admin.after_create = _noop
    geoalchemy_sqlite_admin.before_drop = _noop


def _enable_sqlite_spatial_admin() -> None:
    """Restore default GeoAlchemy SQLite admin hooks."""
    import importlib

    importlib.reload(geoalchemy_sqlite_admin)

from app.models.catalog import AccessType, PriceBasis, RegionZone, ServiceType
from app.models.subscriber import Subscriber
from app.schemas.catalog import (
    CatalogOfferCreate,
    OfferVersionCreate,
    SubscriptionCreate,
)
from app.schemas.gis import GeoLayerCreate
from app.schemas.network import OLTDeviceCreate
from app.schemas.network_monitoring import NetworkDeviceCreate, PopSiteCreate
from app.schemas.radius import RadiusServerCreate
from app.schemas.tr069 import Tr069AcsServerCreate
from app.services import catalog as catalog_service
from app.services import gis as gis_service
from app.services import network as network_service
from app.services import network_monitoring as network_monitoring_service
from app.services import radius as radius_service
from app.services import tr069 as tr069_service


@pytest.fixture(scope="session")
def engine():
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url:
        # Use PostgreSQL for tests (recommended)
        engine = create_engine(database_url)
    else:
        # Fall back to SQLite with Spatialite
        engine = create_engine(
            "sqlite+pysqlite://",
            connect_args={
                "check_same_thread": False,
            },
            poolclass=StaticPool,
        )

        @event.listens_for(engine, "connect")
        def _load_spatialite(dbapi_connection, _connection_record):
            dbapi_connection.enable_load_extension(True)
            try:
                dbapi_connection.load_extension("mod_spatialite")
                _enable_sqlite_spatial_admin()
                _restore_sqlite_geometry_passthrough()
            except Exception:
                _disable_sqlite_spatial_admin()
                _enable_sqlite_geometry_passthrough()
                pass  # Spatialite not available, some tests may fail
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            try:
                cursor.execute("SELECT InitSpatialMetaData(1)")
            except Exception:
                pass  # Already initialized or spatialite not available
            cursor.close()

        # Create a connection first to initialize spatialite
        with engine.connect() as conn:
            pass

    Base.metadata.create_all(engine)

    return engine


@pytest.fixture()
def db_session(engine):
    connection = engine.connect()
    transaction = connection.begin()
    SessionLocal = sessionmaker(bind=connection, autoflush=False, autocommit=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()


def _unique_email() -> str:
    return f"test-{uuid.uuid4().hex}@example.com"


@pytest.fixture()
def subscriber(db_session):
    """Unified subscriber fixture - combines identity, account, and billing."""
    subscriber = Subscriber(
        first_name="Test",
        last_name="User",
        email=_unique_email(),
    )
    db_session.add(subscriber)
    db_session.commit()
    db_session.refresh(subscriber)
    return subscriber


# Legacy alias for backwards compatibility with tests expecting 'person'
@pytest.fixture()
def person(subscriber):
    """Alias for subscriber fixture for backward compatibility."""
    return subscriber


@pytest.fixture()
def subscriber_account(subscriber):
    """Legacy alias for unified Subscriber (formerly SubscriberAccount)."""
    return subscriber


@pytest.fixture()
def work_order():
    """Lightweight fixture for comms tests (legacy name for service order)."""
    from types import SimpleNamespace
    return SimpleNamespace(id=uuid.uuid4())


@pytest.fixture()
def ticket():
    """Lightweight fixture for comms tests."""
    from types import SimpleNamespace
    return SimpleNamespace(id=uuid.uuid4())


@pytest.fixture(autouse=True)
def auth_env(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", os.getenv("JWT_SECRET", "test-secret"))
    monkeypatch.setenv("JWT_ALGORITHM", os.getenv("JWT_ALGORITHM", "HS256"))


@pytest.fixture()
def pop_site(db_session):
    """Point of Presence for network tests."""
    pop_site = network_monitoring_service.pop_sites.create(
        db_session,
        PopSiteCreate(
            name="Test POP",
            code="POP001",
        ),
    )
    return pop_site


@pytest.fixture()
def network_device(db_session, pop_site):
    """Network device for monitoring tests."""
    device = network_monitoring_service.network_devices.create(
        db_session,
        NetworkDeviceCreate(
            name="Test Router",
            hostname="router-01.test.local",
            pop_site_id=pop_site.id,
        ),
    )
    return device


@pytest.fixture()
def olt_device(db_session):
    """OLT device for fiber tests."""
    olt = network_service.olt_devices.create(
        db_session,
        OLTDeviceCreate(
            name="Test OLT",
            hostname="olt-01.test.local",
        ),
    )
    return olt


@pytest.fixture()
def catalog_offer(db_session):
    """Catalog offer for subscription tests."""
    offer = catalog_service.offers.create(
        db_session,
        CatalogOfferCreate(
            name="Standard Internet",
            code="STD-INT",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
        ),
    )
    # Create offer version linking to offer
    catalog_service.offer_versions.create(
        db_session,
        OfferVersionCreate(
            offer_id=offer.id,
            version_number=1,
            name="Standard Internet v1",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
        ),
    )
    return offer


@pytest.fixture()
def subscription(db_session, subscriber, catalog_offer):
    """Active subscription for usage tests."""
    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=catalog_offer.id,
        ),
    )
    return subscription


@pytest.fixture()
def radius_server(db_session):
    """RADIUS server for auth tests."""
    server = radius_service.radius_servers.create(
        db_session,
        RadiusServerCreate(
            name="Test RADIUS",
            host="radius.test.local",
        ),
    )
    return server


@pytest.fixture()
def acs_server(db_session):
    """TR-069 ACS server for CPE tests."""
    server = tr069_service.acs_servers.create(
        db_session,
        Tr069AcsServerCreate(
            name="Test ACS",
            cwmp_url="https://acs.test.local/cwmp",
            cwmp_username="acs-user",
            cwmp_password="acs-pass",
            connection_request_username="cr-user",
            connection_request_password="cr-pass",
            base_url="https://acs.test.local",
        ),
    )
    return server


@pytest.fixture()
def geo_layer(db_session):
    """GIS layer for geo tests."""
    layer = gis_service.geo_layers.create(
        db_session,
        GeoLayerCreate(
            name="Test Layer",
            layer_type="boundary",
        ),
    )
    return layer


# ============================================================================
# Network Fixtures
# ============================================================================

@pytest.fixture()
def region(db_session):
    """RegionZone for VLAN tests."""
    rz = RegionZone(
        name="Test Region",
        code="TEST",
    )
    db_session.add(rz)
    db_session.commit()
    db_session.refresh(rz)
    return rz


# Skip test modules that reference models/functions not yet available on this branch
collect_ignore_glob = [
    "test_admin_actor_ids.py",
    "test_customer_location_requests.py",
    "test_log_regressions.py",
    "test_transactional_audit_events.py",
    "test_web_system_audit_service.py",
]
