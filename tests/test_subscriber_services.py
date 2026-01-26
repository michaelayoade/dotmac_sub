"""Tests for subscriber service."""

import uuid
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.billing import TaxRate
from app.models.subscriber import (
    AccountRole,
    AccountRoleType,
    AccountStatus,
    Address,
    AddressType,
    Organization,
    Reseller,
    Subscriber,
    SubscriberAccount,
    SubscriberCustomField,
)
from app.models.subscription_engine import SettingValueType
from app.schemas.subscriber import (
    AddressCreate,
    AddressUpdate,
    AccountRoleCreate,
    AccountRoleUpdate,
    OrganizationCreate,
    OrganizationUpdate,
    ResellerCreate,
    ResellerUpdate,
    SubscriberAccountCreate,
    SubscriberAccountUpdate,
    SubscriberCreate,
    SubscriberCustomFieldCreate,
    SubscriberCustomFieldUpdate,
    SubscriberUpdate,
)
from app.services import subscriber as subscriber_service
from app.services.common import apply_ordering, apply_pagination, validate_enum


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture()
def organization(db_session):
    """Organization for subscriber tests."""
    org = Organization(name="Test Corp")
    db_session.add(org)
    db_session.commit()
    db_session.refresh(org)
    return org


@pytest.fixture()
def reseller(db_session):
    """Reseller for account tests."""
    reseller = Reseller(name="Partner Reseller", is_active=True)
    db_session.add(reseller)
    db_session.commit()
    db_session.refresh(reseller)
    return reseller


@pytest.fixture()
def tax_rate(db_session):
    """Tax rate for account/address tests."""
    rate = TaxRate(name="Standard VAT", rate=Decimal("0.2000"))
    db_session.add(rate)
    db_session.commit()
    db_session.refresh(rate)
    return rate


@pytest.fixture()
def address(db_session, subscriber):
    """Address for tests."""
    addr = Address(
        subscriber_id=subscriber.id,
        address_line1="123 Main St",
        city="Anytown",
        region="CA",
        postal_code="12345",
    )
    db_session.add(addr)
    db_session.commit()
    db_session.refresh(addr)
    return addr


@pytest.fixture()
def custom_field(db_session, subscriber):
    """Custom field for tests."""
    cf = SubscriberCustomField(
        subscriber_id=subscriber.id,
        key="custom_key",
        value_text="custom_value",
    )
    db_session.add(cf)
    db_session.commit()
    db_session.refresh(cf)
    return cf


# ============================================================================
# Helper Function Tests
# ============================================================================


class TestApplyOrdering:
    """Tests for _apply_ordering helper."""

    def test_orders_by_allowed_column_asc(self, db_session):
        """Test orders ascending by allowed column."""
        from sqlalchemy import Column, String

        query = db_session.query(Organization)
        allowed = {"name": Organization.name}
        result = apply_ordering(query, "name", "asc", allowed)
        # Just verify query is returned
        assert result is not None

    def test_orders_by_allowed_column_desc(self, db_session):
        """Test orders descending by allowed column."""
        query = db_session.query(Organization)
        allowed = {"name": Organization.name}
        result = apply_ordering(query, "name", "desc", allowed)
        assert result is not None

    def test_raises_for_invalid_column(self, db_session):
        """Test raises HTTPException for invalid order_by column."""
        query = db_session.query(Organization)
        allowed = {"name": Organization.name}
        with pytest.raises(HTTPException) as exc_info:
            apply_ordering(query, "invalid", "asc", allowed)
        assert exc_info.value.status_code == 400
        assert "Invalid order_by" in exc_info.value.detail


class TestApplyPagination:
    """Tests for _apply_pagination helper."""

    def test_applies_limit_and_offset(self, db_session):
        """Test applies limit and offset."""
        query = db_session.query(Organization)
        result = apply_pagination(query, limit=10, offset=5)
        assert result is not None


class TestValidateEnum:
    """Tests for _validate_enum helper."""

    def test_returns_none_for_none_value(self):
        """Test returns None for None value."""
        result = validate_enum(None, AccountStatus, "status")
        assert result is None

    def test_returns_enum_for_valid_value(self):
        """Test returns enum instance for valid value."""
        result = validate_enum("active", AccountStatus, "status")
        assert result == AccountStatus.active

    def test_raises_for_invalid_value(self):
        """Test raises HTTPException for invalid enum value."""
        with pytest.raises(HTTPException) as exc_info:
            validate_enum("invalid", AccountStatus, "status")
        assert exc_info.value.status_code == 400
        assert "Invalid status" in exc_info.value.detail


class TestValidateTaxRate:
    """Tests for _validate_tax_rate helper."""

    def test_returns_none_for_empty_id(self, db_session):
        """Test returns None for empty tax_rate_id."""
        result = subscriber_service._validate_tax_rate(db_session, None)
        assert result is None

        result = subscriber_service._validate_tax_rate(db_session, "")
        assert result is None

    def test_returns_rate_for_valid_id(self, db_session, tax_rate):
        """Test returns TaxRate for valid id."""
        result = subscriber_service._validate_tax_rate(db_session, str(tax_rate.id))
        assert result == tax_rate

    def test_raises_for_invalid_id(self, db_session):
        """Test raises HTTPException for invalid tax_rate_id."""
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service._validate_tax_rate(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404
        assert "Tax rate not found" in exc_info.value.detail


# ============================================================================
# Organizations Tests
# ============================================================================


class TestOrganizationsCreate:
    """Tests for Organizations.create."""

    def test_creates_organization(self, db_session):
        """Test creates organization with required fields."""
        payload = OrganizationCreate(name="Acme Inc")
        result = subscriber_service.organizations.create(db_session, payload)
        assert result.id is not None
        assert result.name == "Acme Inc"

    def test_creates_with_optional_fields(self, db_session):
        """Test creates organization with optional fields."""
        payload = OrganizationCreate(
            name="Acme Inc",
            legal_name="Acme Incorporated",
            tax_id="123-456-789",
            domain="acme.com",
            website="https://acme.com",
            notes="A test org",
        )
        result = subscriber_service.organizations.create(db_session, payload)
        assert result.legal_name == "Acme Incorporated"
        assert result.tax_id == "123-456-789"
        assert result.domain == "acme.com"
        assert result.website == "https://acme.com"
        assert result.notes == "A test org"


class TestOrganizationsGet:
    """Tests for Organizations.get."""

    def test_gets_organization(self, db_session, organization):
        """Test gets organization by id."""
        result = subscriber_service.organizations.get(db_session, str(organization.id))
        assert result.id == organization.id
        assert result.name == organization.name

    def test_raises_for_not_found(self, db_session):
        """Test raises HTTPException for not found."""
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.organizations.get(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404
        assert "Organization not found" in exc_info.value.detail


class TestOrganizationsList:
    """Tests for Organizations.list."""

    def test_lists_organizations(self, db_session, organization):
        """Test lists organizations."""
        result = subscriber_service.organizations.list(
            db=db_session,
            name=None,
            order_by="name",
            order_dir="asc",
            limit=10,
            offset=0,
        )
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_filters_by_name(self, db_session, organization):
        """Test filters organizations by name."""
        result = subscriber_service.organizations.list(
            db=db_session,
            name="Test",
            order_by="name",
            order_dir="asc",
            limit=10,
            offset=0,
        )
        assert all("test" in org.name.lower() for org in result)

    def test_orders_descending(self, db_session):
        """Test orders descending."""
        # Create two orgs
        org1 = Organization(name="Alpha")
        org2 = Organization(name="Zeta")
        db_session.add_all([org1, org2])
        db_session.commit()

        result = subscriber_service.organizations.list(
            db=db_session,
            name=None,
            order_by="name",
            order_dir="desc",
            limit=10,
            offset=0,
        )
        # First should be Zeta
        assert result[0].name == "Zeta"


class TestOrganizationsUpdate:
    """Tests for Organizations.update."""

    def test_updates_organization(self, db_session, organization):
        """Test updates organization."""
        payload = OrganizationUpdate(name="Updated Corp")
        result = subscriber_service.organizations.update(
            db_session, str(organization.id), payload
        )
        assert result.name == "Updated Corp"

    def test_partial_update(self, db_session, organization):
        """Test partial update preserves other fields."""
        payload = OrganizationUpdate(legal_name="Legal Name")
        result = subscriber_service.organizations.update(
            db_session, str(organization.id), payload
        )
        assert result.name == organization.name
        assert result.legal_name == "Legal Name"

    def test_raises_for_not_found(self, db_session):
        """Test raises HTTPException for not found."""
        payload = OrganizationUpdate(name="Test")
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.organizations.update(
                db_session, str(uuid.uuid4()), payload
            )
        assert exc_info.value.status_code == 404


class TestOrganizationsDelete:
    """Tests for Organizations.delete."""

    def test_deletes_organization(self, db_session, organization):
        """Test deletes organization (hard delete)."""
        org_id = organization.id
        subscriber_service.organizations.delete(db_session, str(org_id))
        assert db_session.get(Organization, org_id) is None

    def test_raises_for_not_found(self, db_session):
        """Test raises HTTPException for not found."""
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.organizations.delete(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404


# ============================================================================
# Resellers Tests
# ============================================================================


class TestResellersCreate:
    """Tests for Resellers.create."""

    def test_creates_reseller(self, db_session):
        """Test creates reseller with required fields."""
        payload = ResellerCreate(name="New Partner")
        result = subscriber_service.resellers.create(db_session, payload)
        assert result.id is not None
        assert result.name == "New Partner"
        assert result.is_active is True

    def test_creates_with_optional_fields(self, db_session):
        """Test creates reseller with all fields."""
        payload = ResellerCreate(
            name="Full Partner",
            code="FP001",
            contact_email="partner@example.com",
            contact_phone="+1234567890",
            is_active=False,
            notes="Test partner",
        )
        result = subscriber_service.resellers.create(db_session, payload)
        assert result.code == "FP001"
        assert result.contact_email == "partner@example.com"
        assert result.is_active is False


class TestResellersGet:
    """Tests for Resellers.get."""

    def test_gets_reseller(self, db_session, reseller):
        """Test gets reseller by id."""
        result = subscriber_service.resellers.get(db_session, str(reseller.id))
        assert result.id == reseller.id

    def test_raises_for_not_found(self, db_session):
        """Test raises HTTPException for not found."""
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.resellers.get(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404
        assert "Reseller not found" in exc_info.value.detail


class TestResellersList:
    """Tests for Resellers.list."""

    def test_lists_active_by_default(self, db_session, reseller):
        """Test lists only active resellers by default."""
        # Create inactive reseller
        inactive = Reseller(name="Inactive Partner", is_active=False)
        db_session.add(inactive)
        db_session.commit()

        result = subscriber_service.resellers.list(
            db=db_session,
            is_active=None,  # Default to active only
            order_by="name",
            order_dir="asc",
            limit=100,
            offset=0,
        )
        assert all(r.is_active for r in result)

    def test_lists_inactive_when_specified(self, db_session):
        """Test lists inactive resellers when specified."""
        inactive = Reseller(name="Inactive Partner", is_active=False)
        db_session.add(inactive)
        db_session.commit()

        result = subscriber_service.resellers.list(
            db=db_session,
            is_active=False,
            order_by="name",
            order_dir="asc",
            limit=100,
            offset=0,
        )
        assert all(not r.is_active for r in result)


class TestResellersUpdate:
    """Tests for Resellers.update."""

    def test_updates_reseller(self, db_session, reseller):
        """Test updates reseller."""
        payload = ResellerUpdate(name="Updated Partner")
        result = subscriber_service.resellers.update(
            db_session, str(reseller.id), payload
        )
        assert result.name == "Updated Partner"

    def test_raises_for_not_found(self, db_session):
        """Test raises HTTPException for not found."""
        payload = ResellerUpdate(name="Test")
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.resellers.update(db_session, str(uuid.uuid4()), payload)
        assert exc_info.value.status_code == 404


class TestResellersDelete:
    """Tests for Resellers.delete."""

    def test_deletes_reseller(self, db_session, reseller):
        """Test deletes reseller (hard delete)."""
        reseller_id = reseller.id
        subscriber_service.resellers.delete(db_session, str(reseller_id))
        assert db_session.get(Reseller, reseller_id) is None

    def test_raises_for_not_found(self, db_session):
        """Test raises HTTPException for not found."""
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.resellers.delete(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404


# ============================================================================
# Subscribers Tests
# ============================================================================


class TestSubscribersCreate:
    """Tests for Subscribers.create."""

    def test_creates_person_subscriber(self, db_session, person):
        """Test creates subscriber for person."""
        payload = SubscriberCreate(
            person_id=person.id,
        )
        result = subscriber_service.subscribers.create(db_session, payload)
        assert result.id is not None
        assert result.person_id == person.id

    def test_raises_for_invalid_person(self, db_session):
        """Test raises HTTPException for invalid person_id."""
        payload = SubscriberCreate(
            person_id=uuid.uuid4(),
        )
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.subscribers.create(db_session, payload)
        assert exc_info.value.status_code == 404
        assert "Person not found" in exc_info.value.detail

    def test_auto_generates_subscriber_number(self, db_session, person, monkeypatch):
        """Test auto-generates subscriber number when enabled."""
        monkeypatch.setattr(
            "app.services.subscriber.numbering.generate_number",
            lambda *args, **kwargs: "SUB-001",
        )
        payload = SubscriberCreate(
            person_id=person.id,
        )
        result = subscriber_service.subscribers.create(db_session, payload)
        assert result.subscriber_number == "SUB-001"

    def test_uses_provided_subscriber_number(self, db_session, person):
        """Test uses provided subscriber number."""
        payload = SubscriberCreate(
            person_id=person.id,
            subscriber_number="CUSTOM-123",
        )
        result = subscriber_service.subscribers.create(db_session, payload)
        assert result.subscriber_number == "CUSTOM-123"


class TestSubscribersGet:
    """Tests for Subscribers.get."""

    def test_gets_subscriber_with_relations(self, db_session, subscriber):
        """Test gets subscriber with eager-loaded relations."""
        result = subscriber_service.subscribers.get(db_session, str(subscriber.id))
        assert result.id == subscriber.id
        # Relations should be accessible without additional queries
        assert hasattr(result, "accounts")
        assert hasattr(result, "addresses")

    def test_raises_for_not_found(self, db_session):
        """Test raises HTTPException for not found."""
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.subscribers.get(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404
        assert "Subscriber not found" in exc_info.value.detail


class TestSubscribersList:
    """Tests for Subscribers.list."""

    def test_lists_subscribers(self, db_session, subscriber):
        """Test lists subscribers."""
        result = subscriber_service.subscribers.list(
            db=db_session,
            person_id=None,
            organization_id=None,
            subscriber_type=None,
            order_by="created_at",
            order_dir="desc",
            limit=10,
            offset=0,
        )
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_filters_by_person_id(self, db_session, subscriber, person):
        """Test filters by person_id."""
        result = subscriber_service.subscribers.list(
            db=db_session,
            person_id=str(person.id),
            organization_id=None,
            subscriber_type=None,
            order_by="created_at",
            order_dir="asc",
            limit=10,
            offset=0,
        )
        assert all(s.person_id == person.id for s in result)


class TestSubscribersUpdate:
    """Tests for Subscribers.update."""

    def test_updates_subscriber(self, db_session, subscriber):
        """Test updates subscriber."""
        payload = SubscriberUpdate(notes="Updated notes")
        result = subscriber_service.subscribers.update(
            db_session, str(subscriber.id), payload
        )
        assert result.notes == "Updated notes"

    def test_raises_for_invalid_person_on_update(self, db_session, subscriber):
        """Test raises HTTPException for invalid person_id on update."""
        payload = SubscriberUpdate(person_id=uuid.uuid4(), organization_id=None)
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.subscribers.update(
                db_session, str(subscriber.id), payload
            )
        assert exc_info.value.status_code == 404
        assert "Person not found" in exc_info.value.detail

    def test_organization_id_is_ignored_on_update(self, db_session, subscriber):
        """Test that organization_id is ignored on update (legacy field)."""
        # organization_id is intentionally removed from updates
        original_person_id = subscriber.person_id
        payload = SubscriberUpdate(organization_id=uuid.uuid4(), notes="Updated")
        result = subscriber_service.subscribers.update(
            db_session, str(subscriber.id), payload
        )
        # organization_id should be ignored, person_id unchanged
        assert result.person_id == original_person_id
        assert result.notes == "Updated"

    def test_raises_for_not_found(self, db_session):
        """Test raises HTTPException for not found."""
        payload = SubscriberUpdate(notes="Test")
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.subscribers.update(
                db_session, str(uuid.uuid4()), payload
            )
        assert exc_info.value.status_code == 404


class TestSubscribersDelete:
    """Tests for Subscribers.delete."""

    def test_deletes_subscriber(self, db_session, subscriber):
        """Test deletes subscriber (hard delete)."""
        sub_id = subscriber.id
        subscriber_service.subscribers.delete(db_session, str(sub_id))
        assert db_session.get(Subscriber, sub_id) is None

    def test_raises_for_not_found(self, db_session):
        """Test raises HTTPException for not found."""
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.subscribers.delete(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404


# ============================================================================
# Accounts Tests
# ============================================================================


class TestAccountsCreate:
    """Tests for Accounts.create."""

    def test_creates_account(self, db_session, subscriber):
        """Test creates account for subscriber."""
        payload = SubscriberAccountCreate(subscriber_id=subscriber.id)
        result = subscriber_service.accounts.create(db_session, payload)
        assert result.id is not None
        assert result.subscriber_id == subscriber.id

    def test_creates_with_reseller(self, db_session, subscriber, reseller):
        """Test creates account with reseller."""
        payload = SubscriberAccountCreate(
            subscriber_id=subscriber.id,
            reseller_id=reseller.id,
        )
        result = subscriber_service.accounts.create(db_session, payload)
        assert result.reseller_id == reseller.id

    def test_creates_with_tax_rate(self, db_session, subscriber, tax_rate):
        """Test creates account with tax rate."""
        payload = SubscriberAccountCreate(
            subscriber_id=subscriber.id,
            tax_rate_id=tax_rate.id,
        )
        result = subscriber_service.accounts.create(db_session, payload)
        assert result.tax_rate_id == tax_rate.id

    def test_raises_for_invalid_subscriber(self, db_session):
        """Test raises HTTPException for invalid subscriber_id."""
        payload = SubscriberAccountCreate(subscriber_id=uuid.uuid4())
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.accounts.create(db_session, payload)
        assert exc_info.value.status_code == 404
        assert "Subscriber not found" in exc_info.value.detail

    def test_raises_for_invalid_reseller(self, db_session, subscriber):
        """Test raises HTTPException for invalid reseller_id."""
        payload = SubscriberAccountCreate(
            subscriber_id=subscriber.id,
            reseller_id=uuid.uuid4(),
        )
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.accounts.create(db_session, payload)
        assert exc_info.value.status_code == 404
        assert "Reseller not found" in exc_info.value.detail

    def test_raises_for_invalid_tax_rate(self, db_session, subscriber):
        """Test raises HTTPException for invalid tax_rate_id."""
        payload = SubscriberAccountCreate(
            subscriber_id=subscriber.id,
            tax_rate_id=uuid.uuid4(),
        )
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.accounts.create(db_session, payload)
        assert exc_info.value.status_code == 404
        assert "Tax rate not found" in exc_info.value.detail

    def test_auto_generates_account_number(self, db_session, subscriber, monkeypatch):
        """Test auto-generates account number when enabled."""
        monkeypatch.setattr(
            "app.services.subscriber.numbering.generate_number",
            lambda *args, **kwargs: "ACC-001",
        )
        payload = SubscriberAccountCreate(subscriber_id=subscriber.id)
        result = subscriber_service.accounts.create(db_session, payload)
        assert result.account_number == "ACC-001"

    def test_uses_default_status_from_settings(self, db_session, subscriber, monkeypatch):
        """Test uses default_account_status from settings."""
        monkeypatch.setattr(
            "app.services.subscriber.settings_spec.resolve_value",
            lambda db, domain, key: "suspended" if key == "default_account_status" else None,
        )
        payload = SubscriberAccountCreate(subscriber_id=subscriber.id)
        result = subscriber_service.accounts.create(db_session, payload)
        assert result.status == AccountStatus.suspended


class TestAccountsGet:
    """Tests for Accounts.get."""

    def test_gets_account_with_relations(self, db_session, subscriber_account):
        """Test gets account with eager-loaded contacts."""
        result = subscriber_service.accounts.get(db_session, str(subscriber_account.id))
        assert result.id == subscriber_account.id
        assert hasattr(result, "contacts")

    def test_raises_for_not_found(self, db_session):
        """Test raises HTTPException for not found."""
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.accounts.get(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404
        assert "Subscriber account not found" in exc_info.value.detail


class TestAccountsList:
    """Tests for Accounts.list."""

    def test_lists_accounts(self, db_session, subscriber_account):
        """Test lists accounts."""
        result = subscriber_service.accounts.list(
            db=db_session,
            subscriber_id=None,
            reseller_id=None,
            order_by="created_at",
            order_dir="desc",
            limit=10,
            offset=0,
        )
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_filters_by_subscriber_id(self, db_session, subscriber_account, subscriber):
        """Test filters by subscriber_id."""
        result = subscriber_service.accounts.list(
            db=db_session,
            subscriber_id=str(subscriber.id),
            reseller_id=None,
            order_by="created_at",
            order_dir="asc",
            limit=10,
            offset=0,
        )
        assert all(a.subscriber_id == subscriber.id for a in result)

    def test_filters_by_reseller_id(self, db_session, subscriber, reseller):
        """Test filters by reseller_id."""
        account = SubscriberAccount(
            subscriber_id=subscriber.id,
            reseller_id=reseller.id,
        )
        db_session.add(account)
        db_session.commit()

        result = subscriber_service.accounts.list(
            db=db_session,
            subscriber_id=None,
            reseller_id=str(reseller.id),
            order_by="created_at",
            order_dir="asc",
            limit=10,
            offset=0,
        )
        assert len(result) >= 1
        assert all(a.reseller_id == reseller.id for a in result)


class TestAccountsUpdate:
    """Tests for Accounts.update."""

    def test_updates_account(self, db_session, subscriber_account):
        """Test updates account."""
        payload = SubscriberAccountUpdate(notes="Updated notes")
        result = subscriber_service.accounts.update(
            db_session, str(subscriber_account.id), payload
        )
        assert result.notes == "Updated notes"

    def test_raises_for_invalid_subscriber_on_update(self, db_session, subscriber_account):
        """Test raises HTTPException for invalid subscriber_id on update."""
        payload = SubscriberAccountUpdate(subscriber_id=uuid.uuid4())
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.accounts.update(
                db_session, str(subscriber_account.id), payload
            )
        assert exc_info.value.status_code == 404
        assert "Subscriber not found" in exc_info.value.detail

    def test_raises_for_invalid_reseller_on_update(self, db_session, subscriber_account):
        """Test raises HTTPException for invalid reseller_id on update."""
        payload = SubscriberAccountUpdate(reseller_id=uuid.uuid4())
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.accounts.update(
                db_session, str(subscriber_account.id), payload
            )
        assert exc_info.value.status_code == 404
        assert "Reseller not found" in exc_info.value.detail

    def test_raises_for_invalid_tax_rate_on_update(self, db_session, subscriber_account):
        """Test raises HTTPException for invalid tax_rate_id on update."""
        payload = SubscriberAccountUpdate(tax_rate_id=uuid.uuid4())
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.accounts.update(
                db_session, str(subscriber_account.id), payload
            )
        assert exc_info.value.status_code == 404
        assert "Tax rate not found" in exc_info.value.detail

    def test_raises_for_not_found(self, db_session):
        """Test raises HTTPException for not found."""
        payload = SubscriberAccountUpdate(notes="Test")
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.accounts.update(db_session, str(uuid.uuid4()), payload)
        assert exc_info.value.status_code == 404


class TestAccountsDelete:
    """Tests for Accounts.delete."""

    def test_deletes_account(self, db_session, subscriber_account):
        """Test deletes account (hard delete)."""
        account_id = subscriber_account.id
        subscriber_service.accounts.delete(db_session, str(account_id))
        assert db_session.get(SubscriberAccount, account_id) is None

    def test_raises_for_not_found(self, db_session):
        """Test raises HTTPException for not found."""
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.accounts.delete(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404


# ============================================================================
# Account Roles Tests
# ============================================================================


class TestAccountRolesCreate:
    """Tests for AccountRoles.create."""

    def test_creates_account_role(self, db_session, subscriber_account, person):
        """Test creates account role for account."""
        payload = AccountRoleCreate(
            account_id=subscriber_account.id,
            person_id=person.id,
            role=AccountRoleType.billing,
            is_primary=True,
        )
        result = subscriber_service.account_roles.create(db_session, payload)
        assert result.id is not None
        assert result.person_id == person.id
        assert result.role == AccountRoleType.billing
        assert result.is_primary is True

    def test_raises_for_invalid_account(self, db_session, person):
        """Test raises HTTPException for invalid account_id."""
        payload = AccountRoleCreate(
            account_id=uuid.uuid4(),
            person_id=person.id,
            role=AccountRoleType.primary,
        )
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.account_roles.create(db_session, payload)
        assert exc_info.value.status_code == 404
        assert "Subscriber account not found" in exc_info.value.detail

    def test_raises_for_invalid_person(self, db_session, subscriber_account):
        """Test raises HTTPException for invalid person_id."""
        payload = AccountRoleCreate(
            account_id=subscriber_account.id,
            person_id=uuid.uuid4(),
            role=AccountRoleType.primary,
        )
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.account_roles.create(db_session, payload)
        assert exc_info.value.status_code == 404
        assert "Person not found" in exc_info.value.detail

    def test_clears_other_primaries_on_create(self, db_session, subscriber_account, person):
        """Test clears other primaries when creating with is_primary True."""
        from app.models.person import Person

        first = AccountRole(
            account_id=subscriber_account.id,
            person_id=person.id,
            role=AccountRoleType.primary,
            is_primary=True,
        )
        db_session.add(first)
        db_session.commit()

        second_person = Person(
            first_name="Primary",
            last_name="Two",
            email=f"primary-two-{uuid.uuid4().hex}@example.com",
        )
        db_session.add(second_person)
        db_session.commit()
        db_session.refresh(second_person)

        payload = AccountRoleCreate(
            account_id=subscriber_account.id,
            person_id=second_person.id,
            role=AccountRoleType.primary,
            is_primary=True,
        )
        result = subscriber_service.account_roles.create(db_session, payload)
        db_session.refresh(first)
        assert result.is_primary is True
        assert first.is_primary is False


class TestAccountRolesGet:
    """Tests for AccountRoles.get."""

    def test_gets_account_role(self, db_session, subscriber_account, person):
        """Test gets account role by ID."""
        role = AccountRole(
            account_id=subscriber_account.id,
            person_id=person.id,
            role=AccountRoleType.primary,
        )
        db_session.add(role)
        db_session.commit()
        result = subscriber_service.account_roles.get(db_session, str(role.id))
        assert result.id == role.id

    def test_raises_for_not_found(self, db_session):
        """Test raises HTTPException for not found."""
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.account_roles.get(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404
        assert "Account role not found" in exc_info.value.detail


class TestAccountRolesList:
    """Tests for AccountRoles.list."""

    def test_lists_account_roles(self, db_session, subscriber_account, person):
        """Test lists account roles."""
        payload = AccountRoleCreate(
            account_id=subscriber_account.id,
            person_id=person.id,
            role=AccountRoleType.primary,
        )
        subscriber_service.account_roles.create(db_session, payload)

        result = subscriber_service.account_roles.list(
            db_session,
            account_id=str(subscriber_account.id),
            person_id=None,
            order_by="created_at",
            order_dir="asc",
            limit=10,
            offset=0,
        )
        assert len(result) >= 1

    def test_filters_by_person(self, db_session, subscriber_account, person):
        """Test filters by person_id."""
        payload = AccountRoleCreate(
            account_id=subscriber_account.id,
            person_id=person.id,
            role=AccountRoleType.primary,
        )
        subscriber_service.account_roles.create(db_session, payload)

        result = subscriber_service.account_roles.list(
            db_session,
            account_id=None,
            person_id=str(person.id),
            order_by="created_at",
            order_dir="asc",
            limit=10,
            offset=0,
        )
        assert all(r.person_id == person.id for r in result)


class TestAccountRolesUpdate:
    """Tests for AccountRoles.update."""

    def test_updates_account_role(self, db_session, subscriber_account, person):
        """Test updates account role."""
        role = AccountRole(
            account_id=subscriber_account.id,
            person_id=person.id,
            role=AccountRoleType.primary,
        )
        db_session.add(role)
        db_session.commit()

        payload = AccountRoleUpdate(role=AccountRoleType.technical)
        result = subscriber_service.account_roles.update(db_session, str(role.id), payload)
        assert result.role == AccountRoleType.technical

    def test_raises_for_invalid_account(self, db_session, subscriber_account, person):
        """Test raises HTTPException for invalid account_id on update."""
        role = AccountRole(
            account_id=subscriber_account.id,
            person_id=person.id,
            role=AccountRoleType.primary,
        )
        db_session.add(role)
        db_session.commit()

        payload = AccountRoleUpdate(account_id=uuid.uuid4())
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.account_roles.update(db_session, str(role.id), payload)
        assert exc_info.value.status_code == 404
        assert "Subscriber account not found" in exc_info.value.detail

    def test_raises_for_invalid_person(self, db_session, subscriber_account, person):
        """Test raises HTTPException for invalid person_id on update."""
        role = AccountRole(
            account_id=subscriber_account.id,
            person_id=person.id,
            role=AccountRoleType.primary,
        )
        db_session.add(role)
        db_session.commit()

        payload = AccountRoleUpdate(person_id=uuid.uuid4())
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.account_roles.update(db_session, str(role.id), payload)
        assert exc_info.value.status_code == 404
        assert "Person not found" in exc_info.value.detail

    def test_clears_other_primaries_on_update(self, db_session, subscriber_account, person):
        """Test clears other primaries when updating is_primary to True."""
        from app.models.person import Person

        first = AccountRole(
            account_id=subscriber_account.id,
            person_id=person.id,
            role=AccountRoleType.primary,
            is_primary=True,
        )
        db_session.add(first)
        db_session.commit()

        second_person = Person(
            first_name="Second",
            last_name="Primary",
            email=f"second-primary-{uuid.uuid4().hex}@example.com",
        )
        db_session.add(second_person)
        db_session.commit()
        db_session.refresh(second_person)

        second = AccountRole(
            account_id=subscriber_account.id,
            person_id=second_person.id,
            role=AccountRoleType.primary,
            is_primary=False,
        )
        db_session.add(second)
        db_session.commit()

        payload = AccountRoleUpdate(is_primary=True)
        subscriber_service.account_roles.update(db_session, str(second.id), payload)

        db_session.refresh(first)
        assert first.is_primary is False

    def test_raises_for_not_found(self, db_session):
        """Test raises HTTPException for not found."""
        payload = AccountRoleUpdate(role=AccountRoleType.primary)
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.account_roles.update(db_session, str(uuid.uuid4()), payload)
        assert exc_info.value.status_code == 404


class TestAccountRolesDelete:
    """Tests for AccountRoles.delete."""

    def test_deletes_account_role(self, db_session, subscriber_account, person):
        """Test deletes account role (hard delete)."""
        role = AccountRole(
            account_id=subscriber_account.id,
            person_id=person.id,
            role=AccountRoleType.primary,
        )
        db_session.add(role)
        db_session.commit()
        role_id = role.id

        subscriber_service.account_roles.delete(db_session, str(role_id))
        assert db_session.get(AccountRole, role_id) is None

    def test_raises_for_not_found(self, db_session):
        """Test raises HTTPException for not found."""
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.account_roles.delete(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404


# ============================================================================
# Addresses Tests
# ============================================================================


class TestAddressesCreate:
    """Tests for Addresses.create."""

    def test_creates_address(self, db_session, subscriber, monkeypatch):
        """Test creates address for subscriber."""
        # Mock geocoding to return unchanged data
        monkeypatch.setattr(
            "app.services.subscriber.geocoding_service.geocode_address",
            lambda db, data: data,
        )
        payload = AddressCreate(
            subscriber_id=subscriber.id,
            address_line1="456 Oak Ave",
            city="Somewhere",
        )
        result = subscriber_service.addresses.create(db_session, payload)
        assert result.id is not None
        assert result.address_line1 == "456 Oak Ave"

    def test_raises_for_invalid_subscriber(self, db_session):
        """Test raises HTTPException for invalid subscriber_id."""
        payload = AddressCreate(
            subscriber_id=uuid.uuid4(),
            address_line1="123 Main St",
        )
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.addresses.create(db_session, payload)
        assert exc_info.value.status_code == 404
        assert "Subscriber not found" in exc_info.value.detail

    def test_creates_with_account(self, db_session, subscriber, subscriber_account, monkeypatch):
        """Test creates address linked to account."""
        monkeypatch.setattr(
            "app.services.subscriber.geocoding_service.geocode_address",
            lambda db, data: data,
        )
        payload = AddressCreate(
            subscriber_id=subscriber.id,
            account_id=subscriber_account.id,
            address_line1="789 Pine St",
        )
        result = subscriber_service.addresses.create(db_session, payload)
        assert result.account_id == subscriber_account.id

    def test_raises_for_invalid_account(self, db_session, subscriber):
        """Test raises HTTPException for invalid account_id."""
        payload = AddressCreate(
            subscriber_id=subscriber.id,
            account_id=uuid.uuid4(),
            address_line1="123 Main St",
        )
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.addresses.create(db_session, payload)
        assert exc_info.value.status_code == 404
        assert "Subscriber account not found" in exc_info.value.detail

    def test_raises_when_account_not_for_subscriber(self, db_session, subscriber, person):
        """Test raises HTTPException when account belongs to different subscriber."""
        # Create another subscriber and account
        other_sub = Subscriber(person_id=person.id)
        db_session.add(other_sub)
        db_session.commit()
        other_account = SubscriberAccount(subscriber_id=other_sub.id)
        db_session.add(other_account)
        db_session.commit()

        payload = AddressCreate(
            subscriber_id=subscriber.id,
            account_id=other_account.id,
            address_line1="123 Main St",
        )
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.addresses.create(db_session, payload)
        assert exc_info.value.status_code == 400
        assert "Account does not belong to subscriber" in exc_info.value.detail

    def test_validates_tax_rate(self, db_session, subscriber):
        """Test validates tax_rate_id."""
        payload = AddressCreate(
            subscriber_id=subscriber.id,
            tax_rate_id=uuid.uuid4(),
            address_line1="123 Main St",
        )
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.addresses.create(db_session, payload)
        assert exc_info.value.status_code == 404
        assert "Tax rate not found" in exc_info.value.detail

    def test_clears_other_primaries_when_setting_primary(self, db_session, subscriber, monkeypatch):
        """Test clears other primary addresses when setting is_primary."""
        monkeypatch.setattr(
            "app.services.subscriber.geocoding_service.geocode_address",
            lambda db, data: data,
        )
        first = Address(
            subscriber_id=subscriber.id,
            address_line1="First St",
            address_type=AddressType.service,
            is_primary=True,
        )
        db_session.add(first)
        db_session.commit()

        payload = AddressCreate(
            subscriber_id=subscriber.id,
            address_line1="Second St",
            address_type=AddressType.service,
            is_primary=True,
        )
        result = subscriber_service.addresses.create(db_session, payload)
        assert result.is_primary is True

        db_session.refresh(first)
        assert first.is_primary is False

    def test_uses_default_address_type_from_settings(self, db_session, subscriber, monkeypatch):
        """Test uses default_address_type from settings."""
        monkeypatch.setattr(
            "app.services.subscriber.geocoding_service.geocode_address",
            lambda db, data: data,
        )
        monkeypatch.setattr(
            "app.services.subscriber.settings_spec.resolve_value",
            lambda db, domain, key: "billing" if key == "default_address_type" else None,
        )
        payload = AddressCreate(
            subscriber_id=subscriber.id,
            address_line1="123 Main St",
        )
        result = subscriber_service.addresses.create(db_session, payload)
        assert result.address_type == AddressType.billing


class TestAddressesGet:
    """Tests for Addresses.get."""

    def test_gets_address(self, db_session, address):
        """Test gets address by id."""
        result = subscriber_service.addresses.get(db_session, str(address.id))
        assert result.id == address.id

    def test_raises_for_not_found(self, db_session):
        """Test raises HTTPException for not found."""
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.addresses.get(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404
        assert "Address not found" in exc_info.value.detail


class TestAddressesList:
    """Tests for Addresses.list."""

    def test_lists_addresses(self, db_session, address):
        """Test lists addresses."""
        result = subscriber_service.addresses.list(
            db=db_session,
            subscriber_id=None,
            account_id=None,
            order_by="created_at",
            order_dir="desc",
            limit=10,
            offset=0,
        )
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_filters_by_subscriber_id(self, db_session, address, subscriber):
        """Test filters by subscriber_id."""
        result = subscriber_service.addresses.list(
            db=db_session,
            subscriber_id=str(subscriber.id),
            account_id=None,
            order_by="created_at",
            order_dir="asc",
            limit=10,
            offset=0,
        )
        assert all(a.subscriber_id == subscriber.id for a in result)

    def test_filters_by_account_id(self, db_session, subscriber, subscriber_account):
        """Test filters by account_id."""
        addr = Address(
            subscriber_id=subscriber.id,
            account_id=subscriber_account.id,
            address_line1="Account Address",
        )
        db_session.add(addr)
        db_session.commit()

        result = subscriber_service.addresses.list(
            db=db_session,
            subscriber_id=None,
            account_id=str(subscriber_account.id),
            order_by="created_at",
            order_dir="asc",
            limit=10,
            offset=0,
        )
        assert len(result) >= 1
        assert all(a.account_id == subscriber_account.id for a in result)


class TestAddressesUpdate:
    """Tests for Addresses.update."""

    def test_updates_address(self, db_session, address, monkeypatch):
        """Test updates address."""
        monkeypatch.setattr(
            "app.services.subscriber.geocoding_service.geocode_address",
            lambda db, data: data,
        )
        payload = AddressUpdate(city="New City")
        result = subscriber_service.addresses.update(db_session, str(address.id), payload)
        assert result.city == "New City"

    def test_raises_for_invalid_subscriber_on_update(self, db_session, address):
        """Test raises HTTPException for invalid subscriber_id on update."""
        payload = AddressUpdate(subscriber_id=uuid.uuid4())
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.addresses.update(db_session, str(address.id), payload)
        assert exc_info.value.status_code == 404
        assert "Subscriber not found" in exc_info.value.detail

    def test_raises_for_invalid_account_on_update(self, db_session, address):
        """Test raises HTTPException for invalid account_id on update."""
        payload = AddressUpdate(account_id=uuid.uuid4())
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.addresses.update(db_session, str(address.id), payload)
        assert exc_info.value.status_code == 404
        assert "Subscriber account not found" in exc_info.value.detail

    def test_raises_when_account_not_for_subscriber_on_update(self, db_session, address, person):
        """Test raises HTTPException when account belongs to different subscriber."""
        other_sub = Subscriber(person_id=person.id)
        db_session.add(other_sub)
        db_session.commit()
        other_account = SubscriberAccount(subscriber_id=other_sub.id)
        db_session.add(other_account)
        db_session.commit()

        payload = AddressUpdate(account_id=other_account.id)
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.addresses.update(db_session, str(address.id), payload)
        assert exc_info.value.status_code == 400
        assert "Account does not belong to subscriber" in exc_info.value.detail

    def test_validates_tax_rate_on_update(self, db_session, address):
        """Test validates tax_rate_id on update."""
        payload = AddressUpdate(tax_rate_id=uuid.uuid4())
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.addresses.update(db_session, str(address.id), payload)
        assert exc_info.value.status_code == 404
        assert "Tax rate not found" in exc_info.value.detail

    def test_clears_other_primaries_on_update(self, db_session, subscriber, monkeypatch):
        """Test clears other primaries when updating is_primary to True."""
        monkeypatch.setattr(
            "app.services.subscriber.geocoding_service.geocode_address",
            lambda db, data: data,
        )
        first = Address(
            subscriber_id=subscriber.id,
            address_line1="First",
            address_type=AddressType.service,
            is_primary=True,
        )
        second = Address(
            subscriber_id=subscriber.id,
            address_line1="Second",
            address_type=AddressType.service,
            is_primary=False,
        )
        db_session.add_all([first, second])
        db_session.commit()

        payload = AddressUpdate(is_primary=True)
        subscriber_service.addresses.update(db_session, str(second.id), payload)

        db_session.refresh(first)
        assert first.is_primary is False

    def test_triggers_geocoding_when_no_coords(self, db_session, address, monkeypatch):
        """Test triggers geocoding when lat/lng not provided."""
        geocode_called = []

        def mock_geocode(db, data):
            geocode_called.append(True)
            data["latitude"] = 40.7128
            data["longitude"] = -74.0060
            return data

        monkeypatch.setattr(
            "app.services.subscriber.geocoding_service.geocode_address",
            mock_geocode,
        )
        payload = AddressUpdate(city="New York")
        result = subscriber_service.addresses.update(db_session, str(address.id), payload)
        assert len(geocode_called) == 1

    def test_raises_for_not_found(self, db_session):
        """Test raises HTTPException for not found."""
        payload = AddressUpdate(city="Test")
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.addresses.update(db_session, str(uuid.uuid4()), payload)
        assert exc_info.value.status_code == 404


class TestAddressesDelete:
    """Tests for Addresses.delete."""

    def test_deletes_address(self, db_session, address):
        """Test deletes address (hard delete)."""
        address_id = address.id
        subscriber_service.addresses.delete(db_session, str(address_id))
        assert db_session.get(Address, address_id) is None

    def test_raises_for_not_found(self, db_session):
        """Test raises HTTPException for not found."""
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.addresses.delete(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404


# ============================================================================
# SubscriberCustomFields Tests
# ============================================================================


class TestSubscriberCustomFieldsCreate:
    """Tests for SubscriberCustomFields.create."""

    def test_creates_custom_field(self, db_session, subscriber):
        """Test creates custom field for subscriber."""
        payload = SubscriberCustomFieldCreate(
            subscriber_id=subscriber.id,
            key="custom_key",
            value_text="custom_value",
        )
        result = subscriber_service.subscriber_custom_fields.create(db_session, payload)
        assert result.id is not None
        assert result.key == "custom_key"
        assert result.value_text == "custom_value"

    def test_raises_for_invalid_subscriber(self, db_session):
        """Test raises HTTPException for invalid subscriber_id."""
        payload = SubscriberCustomFieldCreate(
            subscriber_id=uuid.uuid4(),
            key="test_key",
        )
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.subscriber_custom_fields.create(db_session, payload)
        assert exc_info.value.status_code == 404
        assert "Subscriber not found" in exc_info.value.detail

    def test_creates_with_json_value(self, db_session, subscriber):
        """Test creates custom field with JSON value."""
        payload = SubscriberCustomFieldCreate(
            subscriber_id=subscriber.id,
            key="json_key",
            value_type=SettingValueType.json,
            value_json={"nested": "data"},
        )
        result = subscriber_service.subscriber_custom_fields.create(db_session, payload)
        assert result.value_json == {"nested": "data"}


class TestSubscriberCustomFieldsGet:
    """Tests for SubscriberCustomFields.get."""

    def test_gets_custom_field(self, db_session, custom_field):
        """Test gets custom field by id."""
        result = subscriber_service.subscriber_custom_fields.get(
            db_session, str(custom_field.id)
        )
        assert result.id == custom_field.id

    def test_raises_for_not_found(self, db_session):
        """Test raises HTTPException for not found."""
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.subscriber_custom_fields.get(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404
        assert "Subscriber custom field not found" in exc_info.value.detail


class TestSubscriberCustomFieldsList:
    """Tests for SubscriberCustomFields.list."""

    def test_lists_active_by_default(self, db_session, custom_field):
        """Test lists only active custom fields by default."""
        inactive = SubscriberCustomField(
            subscriber_id=custom_field.subscriber_id,
            key="inactive_key",
            is_active=False,
        )
        db_session.add(inactive)
        db_session.commit()

        result = subscriber_service.subscriber_custom_fields.list(
            db=db_session,
            subscriber_id=None,
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )
        assert all(cf.is_active for cf in result)

    def test_lists_inactive_when_specified(self, db_session, subscriber):
        """Test lists inactive custom fields when specified."""
        inactive = SubscriberCustomField(
            subscriber_id=subscriber.id,
            key="inactive_key",
            is_active=False,
        )
        db_session.add(inactive)
        db_session.commit()

        result = subscriber_service.subscriber_custom_fields.list(
            db=db_session,
            subscriber_id=None,
            is_active=False,
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )
        assert all(not cf.is_active for cf in result)

    def test_filters_by_subscriber_id(self, db_session, custom_field, subscriber):
        """Test filters by subscriber_id."""
        result = subscriber_service.subscriber_custom_fields.list(
            db=db_session,
            subscriber_id=str(subscriber.id),
            is_active=None,
            order_by="created_at",
            order_dir="asc",
            limit=10,
            offset=0,
        )
        assert all(cf.subscriber_id == subscriber.id for cf in result)

    def test_orders_by_key(self, db_session, subscriber):
        """Test orders by key."""
        cf1 = SubscriberCustomField(subscriber_id=subscriber.id, key="alpha_key")
        cf2 = SubscriberCustomField(subscriber_id=subscriber.id, key="zeta_key")
        db_session.add_all([cf1, cf2])
        db_session.commit()

        result = subscriber_service.subscriber_custom_fields.list(
            db=db_session,
            subscriber_id=str(subscriber.id),
            is_active=None,
            order_by="key",
            order_dir="asc",
            limit=10,
            offset=0,
        )
        keys = [cf.key for cf in result]
        assert keys == sorted(keys)


class TestSubscriberCustomFieldsUpdate:
    """Tests for SubscriberCustomFields.update."""

    def test_updates_custom_field(self, db_session, custom_field):
        """Test updates custom field."""
        payload = SubscriberCustomFieldUpdate(value_text="updated_value")
        result = subscriber_service.subscriber_custom_fields.update(
            db_session, str(custom_field.id), payload
        )
        assert result.value_text == "updated_value"

    def test_raises_for_invalid_subscriber_on_update(self, db_session, custom_field):
        """Test raises HTTPException for invalid subscriber_id on update."""
        payload = SubscriberCustomFieldUpdate(subscriber_id=uuid.uuid4())
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.subscriber_custom_fields.update(
                db_session, str(custom_field.id), payload
            )
        assert exc_info.value.status_code == 404
        assert "Subscriber not found" in exc_info.value.detail

    def test_raises_for_not_found(self, db_session):
        """Test raises HTTPException for not found."""
        payload = SubscriberCustomFieldUpdate(value_text="test")
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.subscriber_custom_fields.update(
                db_session, str(uuid.uuid4()), payload
            )
        assert exc_info.value.status_code == 404


class TestSubscriberCustomFieldsDelete:
    """Tests for SubscriberCustomFields.delete (soft delete)."""

    def test_soft_deletes_custom_field(self, db_session, custom_field):
        """Test soft deletes custom field (sets is_active=False)."""
        cf_id = custom_field.id
        subscriber_service.subscriber_custom_fields.delete(db_session, str(cf_id))

        # Record should still exist but is_active=False
        cf = db_session.get(SubscriberCustomField, cf_id)
        assert cf is not None
        assert cf.is_active is False

    def test_raises_for_not_found(self, db_session):
        """Test raises HTTPException for not found."""
        with pytest.raises(HTTPException) as exc_info:
            subscriber_service.subscriber_custom_fields.delete(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404


# ============================================================================
# ListResponseMixin Tests
# ============================================================================


class TestListResponseMixin:
    """Tests for ListResponseMixin functionality."""

    def test_organizations_list_response(self, db_session, organization):
        """Test organizations.list_response returns proper format."""
        result = subscriber_service.organizations.list_response(
            db_session,
            name=None,
            order_by="name",
            order_dir="asc",
            limit=10,
            offset=0,
        )
        assert "items" in result
        assert "limit" in result
        assert "offset" in result
        assert result["limit"] == 10
        assert result["offset"] == 0

    def test_accounts_list_response(self, db_session, subscriber_account):
        """Test accounts.list_response returns proper format."""
        result = subscriber_service.accounts.list_response(
            db_session,
            subscriber_id=None,
            reseller_id=None,
            order_by="created_at",
            order_dir="desc",
            limit=20,
            offset=5,
        )
        assert "items" in result
        assert result["limit"] == 20
        assert result["offset"] == 5
