from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.schemas.common import ListResponse
from app.schemas.customer_search import CustomerSearchItem
from app.services import customer_search as customer_search_service

router = APIRouter(prefix="/customers", tags=["customers"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("/search", response_model=ListResponse[CustomerSearchItem])
def search_customers(
    q: str = Query(min_length=2),
    limit: int = Query(default=20, ge=1, le=50),
    db: Session = Depends(get_db),
):
    return customer_search_service.search_response(db, q, limit)
