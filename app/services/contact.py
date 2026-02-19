"""Compatibility wrapper for legacy contact service imports."""

from sqlalchemy.orm import Session


class Contacts:
    @staticmethod
    def list(
        db: Session,
        account_id: str | None,
        subscriber_id: str | None = None,
        is_primary: bool | None = None,
        is_active: bool | None = None,
        order_by: str = "created_at",
        order_dir: str = "desc",
        limit: int = 100,
        offset: int = 0,
    ):
        # Person/account role compatibility layer is deprecated.
        return []


contacts = Contacts()
