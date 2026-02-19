"""Service helpers for admin hub pages."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.auth import ApiKey, UserCredential
from app.models.integration import IntegrationJob
from app.models.rbac import Role
from app.models.scheduler import ScheduledTask


def get_admin_hub_counts(db: Session) -> dict[str, int]:
    """Return summary counts for admin hub overview cards."""
    def _count(model) -> int:
        return db.scalar(select(func.count()).select_from(model)) or 0

    return {
        "users_count": _count(UserCredential),
        "users_active_count": (
            db.scalar(
                select(func.count())
                .select_from(UserCredential)
                .where(UserCredential.is_active.is_(True))
            )
            or 0
        ),
        "roles_count": _count(Role),
        "api_keys_count": _count(ApiKey),
        "api_keys_active_count": (
            db.scalar(
                select(func.count())
                .select_from(ApiKey)
                .where(ApiKey.is_active.is_(True))
            )
            or 0
        ),
        "scheduled_tasks_count": _count(ScheduledTask),
        "integration_jobs_count": _count(IntegrationJob),
    }
