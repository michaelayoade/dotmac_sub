"""Compatibility wrapper for legacy contact service imports."""

from sqlalchemy.orm import Session

from app.services.subscriber import account_roles as subscriber_account_roles


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
        return subscriber_account_roles.list(
            db=db,
            account_id=account_id,
            person_id=None,
            order_by=order_by,
            order_dir=order_dir,
            limit=limit,
            offset=offset,
        )


contacts = Contacts()
