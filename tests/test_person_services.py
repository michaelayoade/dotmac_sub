"""Tests for person service."""

import uuid

import pytest
from fastapi import HTTPException

from app.models.person import ChannelType, PartyStatus, PersonStatus
from app.schemas.person import (
    ChannelTypeEnum,
    PartyStatusEnum,
    PersonChannelCreate,
    PersonCreate,
    PersonUpdate,
)
from app.services import person as person_service
from app.services.person import InvalidTransitionError


def _unique_email() -> str:
    return f"test-{uuid.uuid4().hex}@example.com"


def test_create_person(db_session):
    """Test creating a person."""
    email = _unique_email()
    person = person_service.people.create(
        db_session,
        PersonCreate(
            first_name="John",
            last_name="Doe",
            email=email,
        ),
    )
    assert person.first_name == "John"
    assert person.last_name == "Doe"
    assert person.email == email
    assert person.is_active is True


def test_get_person_by_id(db_session):
    """Test getting a person by ID."""
    person = person_service.people.create(
        db_session,
        PersonCreate(
            first_name="Jane",
            last_name="Smith",
            email=_unique_email(),
        ),
    )
    fetched = person_service.people.get(db_session, str(person.id))
    assert fetched is not None
    assert fetched.id == person.id
    assert fetched.first_name == "Jane"


def test_list_people_filter_by_email(db_session):
    """Test listing people filtered by email."""
    email = _unique_email()
    person_service.people.create(
        db_session,
        PersonCreate(first_name="Alice", last_name="Test", email=email),
    )
    person_service.people.create(
        db_session,
        PersonCreate(first_name="Bob", last_name="Other", email=_unique_email()),
    )

    results = person_service.people.list(
        db_session,
        email=email,
        status=None,
        party_status=None,
        organization_id=None,
        is_active=None,
        order_by="created_at",
        order_dir="asc",
        limit=10,
        offset=0,
    )
    assert len(results) == 1
    assert results[0].first_name == "Alice"


def test_list_people_filter_by_status(db_session):
    """Test listing people filtered by status."""
    person1 = person_service.people.create(
        db_session,
        PersonCreate(first_name="Active", last_name="User", email=_unique_email()),
    )
    person2 = person_service.people.create(
        db_session,
        PersonCreate(first_name="Inactive", last_name="User", email=_unique_email()),
    )
    # Update second person to inactive
    person_service.people.update(
        db_session,
        str(person2.id),
        PersonUpdate(status=PersonStatus.inactive),
    )

    active_results = person_service.people.list(
        db_session,
        email=None,
        status="active",
        party_status=None,
        organization_id=None,
        is_active=None,
        order_by="created_at",
        order_dir="asc",
        limit=100,
        offset=0,
    )
    assert any(p.id == person1.id for p in active_results)


def test_list_people_active_only(db_session):
    """Test listing only active people."""
    person = person_service.people.create(
        db_session,
        PersonCreate(first_name="ToDelete", last_name="User", email=_unique_email()),
    )
    person_service.people.delete(db_session, str(person.id))

    results = person_service.people.list(
        db_session,
        email=None,
        status=None,
        party_status=None,
        organization_id=None,
        is_active=True,
        order_by="created_at",
        order_dir="asc",
        limit=100,
        offset=0,
    )
    ids = {p.id for p in results}
    assert person.id not in ids


def test_update_person(db_session):
    """Test updating a person."""
    person = person_service.people.create(
        db_session,
        PersonCreate(first_name="Original", last_name="Name", email=_unique_email()),
    )
    updated = person_service.people.update(
        db_session,
        str(person.id),
        PersonUpdate(first_name="Updated", last_name="Person"),
    )
    assert updated.first_name == "Updated"
    assert updated.last_name == "Person"


def test_delete_person(db_session):
    """Test deleting a person."""
    person = person_service.people.create(
        db_session,
        PersonCreate(first_name="ToDelete", last_name="User", email=_unique_email()),
    )
    person_id = person.id
    person_service.people.delete(db_session, str(person_id))

    # Verify person is deleted
    from fastapi import HTTPException
    import pytest
    with pytest.raises(HTTPException) as exc_info:
        person_service.people.get(db_session, str(person_id))
    assert exc_info.value.status_code == 404


def test_list_people_pagination(db_session):
    """Test pagination of people list."""
    # Create multiple people
    for i in range(5):
        person_service.people.create(
            db_session,
            PersonCreate(
                first_name=f"Person{i}",
                last_name="Test",
                email=_unique_email(),
            ),
        )

    page1 = person_service.people.list(
        db_session,
        email=None,
        status=None,
        party_status=None,
        organization_id=None,
        is_active=None,
        order_by="created_at",
        order_dir="asc",
        limit=2,
        offset=0,
    )
    page2 = person_service.people.list(
        db_session,
        email=None,
        status=None,
        party_status=None,
        organization_id=None,
        is_active=None,
        order_by="created_at",
        order_dir="asc",
        limit=2,
        offset=2,
    )

    assert len(page1) == 2
    assert len(page2) == 2
    # Pages should have different people
    page1_ids = {p.id for p in page1}
    page2_ids = {p.id for p in page2}
    assert page1_ids.isdisjoint(page2_ids)


# ============= Unified Party Model Tests =============


def test_create_person_with_party_status(db_session):
    """Test creating a person with party status."""
    person = person_service.people.create(
        db_session,
        PersonCreate(
            first_name="Lead",
            last_name="Person",
            email=_unique_email(),
            party_status=PartyStatusEnum.lead,
        ),
    )
    assert person.party_status == PartyStatus.lead


def test_create_person_with_channels(db_session):
    """Test creating a person with communication channels."""
    email = _unique_email()
    person = person_service.people.create(
        db_session,
        PersonCreate(
            first_name="Multi",
            last_name="Channel",
            email=email,
            channels=[
                PersonChannelCreate(
                    channel_type=ChannelTypeEnum.phone,
                    address="+1234567890",
                    label="Work",
                    is_primary=True,
                ),
            ],
        ),
    )
    # Should have 2 channels: email (auto-created) + phone
    assert len(person.channels) == 2
    email_channel = next((c for c in person.channels if c.channel_type == ChannelType.email), None)
    phone_channel = next((c for c in person.channels if c.channel_type == ChannelType.phone), None)
    assert email_channel is not None
    assert email_channel.address == email
    assert email_channel.is_primary is True
    assert phone_channel is not None
    assert phone_channel.address == "+1234567890"
    assert phone_channel.label == "Work"


def test_party_status_transition_valid(db_session):
    """Test valid party status transitions."""
    person = person_service.people.create(
        db_session,
        PersonCreate(
            first_name="Transition",
            last_name="Test",
            email=_unique_email(),
            party_status=PartyStatusEnum.lead,
        ),
    )

    # lead -> contact is valid
    updated = person_service.people.transition_status(
        db_session, str(person.id), PartyStatus.contact, reason="Verified contact info"
    )
    assert updated.party_status == PartyStatus.contact

    # contact -> customer is valid
    updated = person_service.people.transition_status(
        db_session, str(person.id), PartyStatus.customer, reason="Accepted quote"
    )
    assert updated.party_status == PartyStatus.customer

    # customer -> subscriber is valid
    updated = person_service.people.transition_status(
        db_session, str(person.id), PartyStatus.subscriber, reason="First account activated"
    )
    assert updated.party_status == PartyStatus.subscriber


def test_party_status_transition_invalid(db_session):
    """Test invalid party status transitions raise error."""
    person = person_service.people.create(
        db_session,
        PersonCreate(
            first_name="Invalid",
            last_name="Transition",
            email=_unique_email(),
            party_status=PartyStatusEnum.lead,
        ),
    )

    # lead -> subscriber is invalid (skips contact/customer)
    with pytest.raises(InvalidTransitionError):
        person_service.people.transition_status(
            db_session, str(person.id), PartyStatus.subscriber
        )


def test_party_status_downgrade(db_session):
    """Test party status downgrades are allowed."""
    person = person_service.people.create(
        db_session,
        PersonCreate(
            first_name="Downgrade",
            last_name="Test",
            email=_unique_email(),
            party_status=PartyStatusEnum.subscriber,
        ),
    )

    # subscriber -> customer is valid (downgrade)
    updated = person_service.people.transition_status(
        db_session, str(person.id), PartyStatus.customer, reason="Subscription canceled"
    )
    assert updated.party_status == PartyStatus.customer


def test_add_channel_to_person(db_session):
    """Test adding a channel to an existing person."""
    person = person_service.people.create(
        db_session,
        PersonCreate(
            first_name="Add",
            last_name="Channel",
            email=_unique_email(),
        ),
    )

    channel = person_service.people.add_channel(
        db_session,
        str(person.id),
        PersonChannelCreate(
            channel_type=ChannelTypeEnum.whatsapp,
            address="+9876543210",
            label="Personal WhatsApp",
        ),
    )

    assert channel.channel_type == ChannelType.whatsapp
    assert channel.address == "9876543210"
    assert channel.person_id == person.id


def test_search_people(db_session):
    """Test searching people by various fields."""
    email = f"searchable-{uuid.uuid4().hex}@example.com"
    person = person_service.people.create(
        db_session,
        PersonCreate(
            first_name="Searchable",
            last_name="Person",
            email=email,
            phone="+1555000999",
        ),
    )

    # Search by name
    results = person_service.people.search(db_session, "Searchable", limit=10)
    assert any(p.id == person.id for p in results)

    # Search by email
    results = person_service.people.search(db_session, "searchable", limit=10)
    assert any(p.id == person.id for p in results)

    # Search by phone
    results = person_service.people.search(db_session, "1555000999", limit=10)
    assert any(p.id == person.id for p in results)


def test_list_people_by_party_status(db_session):
    """Test listing people filtered by party status."""
    email1 = _unique_email()
    email2 = _unique_email()

    person_service.people.create(
        db_session,
        PersonCreate(
            first_name="Lead",
            last_name="Status",
            email=email1,
            party_status=PartyStatusEnum.lead,
        ),
    )
    person_service.people.create(
        db_session,
        PersonCreate(
            first_name="Customer",
            last_name="Status",
            email=email2,
            party_status=PartyStatusEnum.customer,
        ),
    )

    leads = person_service.people.list(
        db_session,
        email=None,
        status=None,
        party_status="lead",
        organization_id=None,
        is_active=None,
        order_by="created_at",
        order_dir="asc",
        limit=100,
        offset=0,
    )

    # Should find the lead
    assert any(p.email == email1 for p in leads)
    # Should not find the customer
    assert not any(p.email == email2 for p in leads)


def test_person_merge(db_session):
    """Test merging two person records."""
    from app.models.crm.sales import Lead

    email1 = _unique_email()
    email2 = _unique_email()

    source = person_service.people.create(
        db_session,
        PersonCreate(first_name="Source", last_name="Person", email=email1),
    )
    target = person_service.people.create(
        db_session,
        PersonCreate(first_name="Target", last_name="Person", email=email2),
    )

    # Create a lead for the source person
    lead = Lead(
        person_id=source.id,
        title="Test Lead",
    )
    db_session.add(lead)
    db_session.commit()

    # Merge source into target
    merged = person_service.people.merge(
        db_session,
        source.id,
        target.id,
    )

    assert merged.id == target.id

    # Lead should now belong to target
    db_session.refresh(lead)
    assert lead.person_id == target.id

    # Source should be archived
    db_session.refresh(source)
    assert source.is_active is False
    assert source.status == PersonStatus.archived


def test_person_merge_same_id_fails(db_session):
    """Test that merging a person with itself fails."""
    person = person_service.people.create(
        db_session,
        PersonCreate(first_name="Self", last_name="Merge", email=_unique_email()),
    )

    with pytest.raises(HTTPException) as exc_info:
        person_service.people.merge(db_session, person.id, person.id)
    assert exc_info.value.status_code == 400
