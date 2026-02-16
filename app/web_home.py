from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.services import subscriber as subscriber_service

templates = Jinja2Templates(directory="templates")

router = APIRouter()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("/", tags=["web"], response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    subscribers = subscriber_service.subscribers.list(
        db=db,
        organization_id=None,
        subscriber_type=None,
        order_by="created_at",
        order_dir="desc",
        limit=25,
        offset=0,
    )
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "title": "DotMac Subs - Subscription Management Platform", "subscribers": subscribers},
    )
