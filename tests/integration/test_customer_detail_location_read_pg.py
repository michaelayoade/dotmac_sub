"""Detail GET must preserve caller transactions on real migrated PostgreSQL."""

from collections.abc import Callable, Iterator
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from app.models.subscriber import Address, Reseller, Subscriber, UserType
from app.services import geocoding
from app.services.web_customer_details import build_customer_detail_snapshot


@pytest.fixture
def location_read_db(cloned_database: Callable[[str], URL]) -> Iterator[Session]:
    engine = create_engine(cloned_database("heads"))
    try:
        with Session(engine) as db:
            yield db
    finally:
        engine.dispose()


@pytest.mark.parametrize("stored_coordinates", [False, True])
def test_detail_get_preserves_flushed_unrelated_write_and_legacy_metadata(
    location_read_db: Session,
    monkeypatch: pytest.MonkeyPatch,
    stored_coordinates: bool,
) -> None:
    db = location_read_db
    metadata = {"latitude": "0", "longitude": 0} if stored_coordinates else {}
    customer = Subscriber(
        first_name="Location",
        last_name="Read",
        email=f"{uuid4().hex}@example.test",
        user_type=UserType.customer,
        address_line1="Legacy location street",
        metadata_=metadata,
        reseller=Reseller(name=f"Location fixture {uuid4().hex}"),
    )
    db.add(customer)
    db.commit()
    customer_id = customer.id
    unrelated = Reseller(name=f"Pending caller {uuid4().hex}")
    db.add(unrelated)
    db.flush()
    unrelated_id = unrelated.id

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail(
            "Detail read invoked a provider or completed the caller transaction"
        )

    monkeypatch.setattr(geocoding, "geocode_preview_from_request", forbidden)
    with monkeypatch.context() as guard:
        guard.setattr(db, "commit", forbidden)
        guard.setattr(db, "rollback", forbidden)
        context = build_customer_detail_snapshot(db, str(customer_id))
    assert db.in_transaction()
    assert (
        db.scalar(select(Reseller.id).where(Reseller.id == unrelated_id))
        == unrelated_id
    )
    assert customer.metadata_ == metadata
    assert (context["map_data"] is not None) is stored_coordinates
    assert (
        db.scalars(select(Address).where(Address.subscriber_id == customer_id)).all()
        == []
    )
    # A separate physical connection sees neither an implicit commit of the
    # unrelated write nor a promoted Address/modified legacy coordinate blob.
    with Session(db.get_bind()) as observer:
        assert observer.get(Reseller, unrelated_id) is None
        persisted = observer.get(Subscriber, customer_id)
        assert persisted is not None and persisted.metadata_ == metadata
        assert (
            observer.scalars(
                select(Address).where(Address.subscriber_id == customer_id)
            ).all()
            == []
        )
    db.rollback()
    assert db.get(Reseller, unrelated_id) is None
