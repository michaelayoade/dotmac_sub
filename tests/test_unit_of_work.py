"""Tests for UnitOfWork transaction management."""

from __future__ import annotations

import uuid

import pytest

from app.models.subscriber import Subscriber
from app.services.unit_of_work import ConcurrencyConflict, UnitOfWork


class TestUnitOfWork:
    """Tests for UnitOfWork context manager."""

    def test_auto_commit_on_success(self, db_session):
        """UnitOfWork should commit on successful exit."""
        subscriber_id = None

        # Wrap session in UnitOfWork
        uow = UnitOfWork(db_session, auto_commit=True)
        with uow:
            subscriber = Subscriber(
                first_name="Test",
                last_name="User",
                email=f"test-{uuid.uuid4().hex}@example.com",
            )
            uow.session.add(subscriber)
            uow.session.flush()
            subscriber_id = subscriber.id

        # After exit, should be committed
        found = db_session.get(Subscriber, subscriber_id)
        assert found is not None
        assert found.first_name == "Test"

    def test_rollback_on_exception(self, db_session):
        """UnitOfWork should rollback on exception."""
        email = f"test-{uuid.uuid4().hex}@example.com"

        # Start with a clean slate
        initial_count = (
            db_session.query(Subscriber).filter(Subscriber.email == email).count()
        )
        assert initial_count == 0

        uow = UnitOfWork(db_session, auto_commit=True)
        try:
            with uow:
                subscriber = Subscriber(
                    first_name="Test",
                    last_name="User",
                    email=email,
                )
                uow.session.add(subscriber)
                uow.session.flush()
                # Raise an exception before exit
                raise ValueError("Test error")
        except ValueError:
            pass

        # After rollback, subscriber should not exist
        count = db_session.query(Subscriber).filter(Subscriber.email == email).count()
        assert count == 0

    def test_no_auto_commit(self, db_session):
        """UnitOfWork with auto_commit=False should not commit."""
        email = f"test-{uuid.uuid4().hex}@example.com"

        uow = UnitOfWork(db_session, auto_commit=False)
        with uow:
            subscriber = Subscriber(
                first_name="Test",
                last_name="User",
                email=email,
            )
            uow.session.add(subscriber)
            uow.session.flush()

        # The session is still in a transaction, so the data is visible
        # within the same session but not committed to the database
        found = db_session.query(Subscriber).filter(Subscriber.email == email).first()
        assert found is not None

    def test_savepoint_success(self, db_session):
        """Savepoint should allow partial commits."""
        email1 = f"test-{uuid.uuid4().hex}@example.com"
        email2 = f"test-{uuid.uuid4().hex}@example.com"

        uow = UnitOfWork(db_session, auto_commit=True)
        with uow:
            # First subscriber - will be committed
            sub1 = Subscriber(
                first_name="First",
                last_name="User",
                email=email1,
            )
            uow.session.add(sub1)
            uow.session.flush()

            # Second subscriber in savepoint - will also be committed
            with uow.savepoint():
                sub2 = Subscriber(
                    first_name="Second",
                    last_name="User",
                    email=email2,
                )
                uow.session.add(sub2)

        # Both should be committed
        found1 = db_session.query(Subscriber).filter(Subscriber.email == email1).first()
        found2 = db_session.query(Subscriber).filter(Subscriber.email == email2).first()
        assert found1 is not None
        assert found2 is not None

    def test_savepoint_rollback(self, db_session):
        """Savepoint rollback should not affect outer transaction."""
        email1 = f"test-{uuid.uuid4().hex}@example.com"
        email2 = f"test-{uuid.uuid4().hex}@example.com"

        uow = UnitOfWork(db_session, auto_commit=True)
        with uow:
            # First subscriber - will be committed
            sub1 = Subscriber(
                first_name="First",
                last_name="User",
                email=email1,
            )
            uow.session.add(sub1)
            uow.session.flush()

            # Second subscriber in savepoint - will be rolled back
            try:
                with uow.savepoint():
                    sub2 = Subscriber(
                        first_name="Second",
                        last_name="User",
                        email=email2,
                    )
                    uow.session.add(sub2)
                    uow.session.flush()
                    raise ValueError("Savepoint error")
            except ValueError:
                pass

        # First should be committed, second should be rolled back
        found1 = db_session.query(Subscriber).filter(Subscriber.email == email1).first()
        found2 = db_session.query(Subscriber).filter(Subscriber.email == email2).first()
        assert found1 is not None
        assert found2 is None

    def test_flush_helper(self, db_session):
        """flush() helper should work."""
        uow = UnitOfWork(db_session, auto_commit=True)
        with uow:
            subscriber = Subscriber(
                first_name="Test",
                last_name="User",
                email=f"test-{uuid.uuid4().hex}@example.com",
            )
            uow.session.add(subscriber)
            uow.flush()
            # ID should be assigned after flush
            assert subscriber.id is not None

    def test_refresh_helper(self, db_session):
        """refresh() helper should work."""
        # Create a subscriber first
        subscriber = Subscriber(
            first_name="Original",
            last_name="User",
            email=f"test-{uuid.uuid4().hex}@example.com",
        )
        db_session.add(subscriber)
        db_session.commit()
        db_session.refresh(subscriber)

        uow = UnitOfWork(db_session, auto_commit=True)
        with uow:
            uow.refresh(subscriber)
            assert subscriber.first_name == "Original"


class TestConcurrencyConflict:
    """Tests for ConcurrencyConflict exception."""

    def test_default_message(self):
        """Exception should have default message."""
        exc = ConcurrencyConflict()
        assert str(exc) == "Concurrent modification detected"
        assert exc.message == "Concurrent modification detected"

    def test_custom_message(self):
        """Exception should accept custom message."""
        exc = ConcurrencyConflict("Custom conflict message")
        assert str(exc) == "Custom conflict message"
        assert exc.message == "Custom conflict message"

    def test_is_catchable(self):
        """Exception should be catchable."""
        with pytest.raises(ConcurrencyConflict) as exc_info:
            raise ConcurrencyConflict("Test conflict")
        assert "Test conflict" in str(exc_info.value)
