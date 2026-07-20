from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.branding import BrandProfileRead, BrandProfileUpdate, ResolvedBrandRead
from app.services import brand_profiles

router = APIRouter(prefix="/branding", tags=["branding"])


@router.get("/resolve", response_model=ResolvedBrandRead)
def resolve_brand(
    subscriber_id: uuid.UUID | None = None,
    reseller_id: uuid.UUID | None = None,
    organization_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
):
    return brand_profiles.resolve_brand(
        db,
        subscriber_id=subscriber_id,
        reseller_id=reseller_id,
        organization_id=organization_id,
    )


@router.get("/profiles", response_model=list[BrandProfileRead])
def list_profiles(
    scope_type: str | None = Query(default=None),
    include_inactive: bool = False,
    db: Session = Depends(get_db),
):
    return brand_profiles.list_brand_profiles(
        db, scope_type=scope_type, active_only=not include_inactive
    )


@router.put("/profiles/{scope_type}", response_model=BrandProfileRead)
def upsert_profile(
    scope_type: str,
    payload: BrandProfileUpdate,
    db: Session = Depends(get_db),
):
    try:
        return brand_profiles.upsert_brand_profile_committed(
            db,
            scope_type=scope_type,
            scope_id=payload.scope_id,
            values=payload.model_dump(exclude={"scope_id"}, exclude_unset=True),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete(
    "/profiles/{scope_type}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def deactivate_profile(
    scope_type: str,
    scope_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
):
    try:
        brand_profiles.deactivate_brand_profile_committed(
            db, scope_type=scope_type, scope_id=scope_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
