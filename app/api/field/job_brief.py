"""Assignment-scoped field job brief transport."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.field.execution import field_domain_errors, field_system_user_id
from app.services.auth_dependencies import require_user_auth
from app.services.field.job_brief import (
    FieldJobBrief,
    FieldJobBriefQuery,
    get_job_brief,
)
from app.services.topology.outage_audience_read import AudienceScope

router = APIRouter(tags=["field-job-brief"])


@router.get("/jobs/{public_id}/brief", response_model=FieldJobBrief)
def read_field_job_brief(
    public_id: str,
    audience_link_id: UUID | None = None,
    audience_scope: AudienceScope = AudienceScope.current,
    expected_revision_id: UUID | None = None,
    after_subscription_id: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    auth: dict[str, object] = Depends(require_user_auth),
    db: Session = Depends(get_db),
) -> FieldJobBrief:
    with field_domain_errors():
        return get_job_brief(
            db=db,
            query=FieldJobBriefQuery(
                requester_system_user_id=field_system_user_id(auth),
                public_id=public_id,
                audience_link_id=audience_link_id,
                audience_scope=audience_scope,
                expected_revision_id=expected_revision_id,
                after_subscription_id=after_subscription_id,
                limit=limit,
            ),
        )
