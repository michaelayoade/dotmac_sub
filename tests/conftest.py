import os
import sqlite3
import uuid

import pytest
from sqlalchemy import create_engine, event, TypeDecorator, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base


class _JoseDateTimeProxy:
    @staticmethod
    def utcnow():
        from datetime import datetime, timezone

        return datetime.now(timezone.utc)

    @staticmethod
    def now(tz=None):
        from datetime import datetime

        return datetime.now(tz)

    @staticmethod
    def __getattr__(name):
        from datetime import datetime

        return getattr(datetime, name)


@pytest.fixture(autouse=True)
def _patch_jose_datetime(monkeypatch):
    import jose.jwt as jose_jwt

    monkeypatch.setattr(jose_jwt, "datetime", _JoseDateTimeProxy, raising=False)

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
from sqlalchemy import Uuid
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


sqltypes.Uuid.bind_processor = _sqlite_uuid_bind_processor
sqltypes.Uuid.result_processor = _sqlite_uuid_result_processor


# Monkey-patch PostgreSQL JSONB type for SQLite compatibility
# SQLite uses JSON instead of JSONB
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import JSON

_original_jsonb_compile = None


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

from app.models.person import Person
from app.models.subscriber import Subscriber, SubscriberAccount
from app.schemas.projects import ProjectCreate, ProjectTaskCreate
from app.schemas.tickets import TicketCreate
from app.schemas.workforce import WorkOrderCreate
from app.schemas.network_monitoring import PopSiteCreate, NetworkDeviceCreate
from app.schemas.network import OLTDeviceCreate
from app.schemas.catalog import CatalogOfferCreate, OfferVersionCreate, SubscriptionCreate
from app.models.catalog import ServiceType, AccessType, PriceBasis, RegionZone
from app.schemas.radius import RadiusServerCreate
from app.schemas.tr069 import Tr069AcsServerCreate
from app.schemas.gis import GeoLayerCreate
from app.services import projects as projects_service
from app.services import tickets as tickets_service
from app.services import workforce as workforce_service
from app.services import network_monitoring as network_monitoring_service
from app.services import network as network_service
from app.services import catalog as catalog_service
from app.services import radius as radius_service
from app.services import tr069 as tr069_service
from app.services import gis as gis_service

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
                "detect_types": sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
            },
            poolclass=StaticPool,
        )

        @event.listens_for(engine, "connect")
        def _load_spatialite(dbapi_connection, _connection_record):
            dbapi_connection.enable_load_extension(True)
            try:
                dbapi_connection.load_extension("mod_spatialite")
            except Exception:
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
        transaction.rollback()
        connection.close()


def _unique_email() -> str:
    return f"test-{uuid.uuid4().hex}@example.com"


@pytest.fixture()
def person(db_session):
    person = Person(
        first_name="Test",
        last_name="User",
        email=_unique_email(),
    )
    db_session.add(person)
    db_session.commit()
    db_session.refresh(person)
    return person


@pytest.fixture()
def subscriber(person, db_session):
    subscriber = Subscriber(
        person_id=person.id,
    )
    db_session.add(subscriber)
    db_session.commit()
    db_session.refresh(subscriber)
    return subscriber


@pytest.fixture()
def subscriber_account(subscriber, db_session):
    account = SubscriberAccount(
        subscriber_id=subscriber.id,
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)
    return account


@pytest.fixture()
def ticket(db_session, subscriber_account):
    ticket = tickets_service.tickets.create(
        db_session,
        TicketCreate(
            account_id=subscriber_account.id,
            title="Connectivity issue",
        ),
    )
    return ticket


@pytest.fixture()
def project(db_session, subscriber_account):
    project = projects_service.projects.create(
        db_session,
        ProjectCreate(
            name="Fiber rollout",
            account_id=subscriber_account.id,
        ),
    )
    return project


@pytest.fixture()
def project_task(db_session, project):
    task = projects_service.project_tasks.create(
        db_session,
        ProjectTaskCreate(
            project_id=project.id,
            title="Splice segment A",
        ),
    )
    return task


@pytest.fixture()
def work_order(db_session, subscriber_account, project, ticket):
    work_order = workforce_service.work_orders.create(
        db_session,
        WorkOrderCreate(
            title="Install ONT",
            account_id=subscriber_account.id,
            project_id=project.id,
            ticket_id=ticket.id,
        ),
    )
    return work_order


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
def subscription(db_session, subscriber_account, catalog_offer):
    """Active subscription for usage tests."""
    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber_account.id,
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
# CRM Fixtures
# ============================================================================

from app.models.crm.team import CrmTeam, CrmAgent, CrmAgentTeam, CrmTeamChannel, CrmRoutingRule
from app.models.person import Person, PersonChannel, ChannelType


@pytest.fixture()
def crm_contact(db_session):
    """CRM contact for conversation tests."""
    person = Person(
        first_name="Test",
        last_name="Contact",
        display_name="Test Contact",
        email=_unique_email(),
        phone="+1555123456",
    )
    db_session.add(person)
    db_session.commit()
    db_session.refresh(person)
    return person


@pytest.fixture()
def crm_contact_channel(db_session, crm_contact):
    """CRM contact channel for messaging tests."""
    channel = PersonChannel(
        person_id=crm_contact.id,
        channel_type=ChannelType.email,
        address=crm_contact.email,
        is_primary=True,
    )
    db_session.add(channel)
    db_session.commit()
    db_session.refresh(channel)
    return channel


@pytest.fixture()
def crm_team(db_session):
    """CRM team for routing tests."""
    team = CrmTeam(
        name="Support Team",
    )
    db_session.add(team)
    db_session.commit()
    db_session.refresh(team)
    return team


@pytest.fixture()
def crm_agent(db_session, person):
    """CRM agent for team tests."""
    agent = CrmAgent(
        person_id=person.id,
        title="Support Agent",
    )
    db_session.add(agent)
    db_session.commit()
    db_session.refresh(agent)
    return agent


@pytest.fixture()
def crm_agent_team(db_session, crm_agent, crm_team):
    """Agent-team link for routing tests."""
    link = CrmAgentTeam(
        agent_id=crm_agent.id,
        team_id=crm_team.id,
    )
    db_session.add(link)
    db_session.commit()
    db_session.refresh(link)
    return link


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
