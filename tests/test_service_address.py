"""Tests for the canonical subscriber service-address resolver."""

from __future__ import annotations

from app.models.subscriber import Address, AddressType
from app.services.service_address import service_address


def _add(db, subscriber_id, **kw):
    address = Address(subscriber_id=subscriber_id, address_line1="x", **kw)
    db.add(address)
    db.commit()
    return address


class TestServiceAddress:
    def test_none_when_no_address(self, db_session, subscriber):
        assert service_address(db_session, subscriber.id) is None

    def test_primary_service_address_wins(self, db_session, subscriber):
        _add(
            db_session, subscriber.id, address_type=AddressType.billing, is_primary=True
        )
        want = _add(
            db_session,
            subscriber.id,
            address_type=AddressType.service,
            is_primary=True,
        )
        assert service_address(db_session, subscriber.id).id == want.id

    def test_falls_back_to_any_address(self, db_session, subscriber):
        want = _add(
            db_session,
            subscriber.id,
            address_type=AddressType.service,
            is_primary=False,
        )
        assert service_address(db_session, subscriber.id).id == want.id
