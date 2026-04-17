"""Tests for pessimistic locking utilities."""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.models.subscriber import Subscriber
from app.services.locking import (
    lock_for_update,
    lock_for_update_or_raise,
    lock_multiple,
)


class TestLockForUpdate:
    """Tests for lock_for_update function."""

    def test_lock_existing_entity(self, db_session, subscriber):
        """Should lock and return existing entity."""
        locked = lock_for_update(db_session, Subscriber, subscriber.id)
        assert locked is not None
        assert locked.id == subscriber.id
        assert locked.first_name == subscriber.first_name

    def test_lock_nonexistent_entity(self, db_session):
        """Should return None for nonexistent entity."""
        fake_id = uuid.uuid4()
        locked = lock_for_update(db_session, Subscriber, fake_id)
        assert locked is None

    def test_lock_with_string_id(self, db_session, subscriber):
        """Should work with string ID."""
        locked = lock_for_update(db_session, Subscriber, str(subscriber.id))
        assert locked is not None
        assert locked.id == subscriber.id


class TestLockMultiple:
    """Tests for lock_multiple function."""

    def test_lock_multiple_entities(self, db_session):
        """Should lock multiple entities in sorted order."""
        # Create test subscribers
        subs = []
        for i in range(3):
            sub = Subscriber(
                first_name=f"Test{i}",
                last_name="User",
                email=f"test-{uuid.uuid4().hex}@example.com",
            )
            db_session.add(sub)
            db_session.flush()
            subs.append(sub)
        db_session.commit()

        # Lock all three
        ids = [sub.id for sub in subs]
        locked = lock_multiple(db_session, Subscriber, ids)

        assert len(locked) == 3
        # Should be sorted by ID
        locked_ids = [s.id for s in locked]
        assert locked_ids == sorted(locked_ids, key=str)

    def test_lock_empty_list(self, db_session):
        """Should return empty list for empty input."""
        locked = lock_multiple(db_session, Subscriber, [])
        assert locked == []

    def test_lock_with_nonexistent_ids(self, db_session, subscriber):
        """Should return only existing entities."""
        fake_id = uuid.uuid4()
        locked = lock_multiple(db_session, Subscriber, [subscriber.id, fake_id])
        assert len(locked) == 1
        assert locked[0].id == subscriber.id

    def test_lock_deduplicates_ids(self, db_session, subscriber):
        """Should deduplicate IDs."""
        ids = [subscriber.id, subscriber.id, subscriber.id]
        locked = lock_multiple(db_session, Subscriber, ids)
        assert len(locked) == 1


class TestLockForUpdateOrRaise:
    """Tests for lock_for_update_or_raise function."""

    def test_returns_entity_when_found(self, db_session, subscriber):
        """Should return entity when it exists."""
        locked = lock_for_update_or_raise(db_session, Subscriber, subscriber.id)
        assert locked.id == subscriber.id

    def test_raises_404_when_not_found(self, db_session):
        """Should raise HTTPException 404 when entity not found."""
        fake_id = uuid.uuid4()
        with pytest.raises(HTTPException) as exc_info:
            lock_for_update_or_raise(db_session, Subscriber, fake_id)
        assert exc_info.value.status_code == 404

    def test_custom_not_found_message(self, db_session):
        """Should use custom message in 404."""
        fake_id = uuid.uuid4()
        with pytest.raises(HTTPException) as exc_info:
            lock_for_update_or_raise(
                db_session,
                Subscriber,
                fake_id,
                not_found_message="Subscriber not found",
            )
        assert exc_info.value.detail == "Subscriber not found"
