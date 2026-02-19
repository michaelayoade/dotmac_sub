"""Public legal document viewing routes."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_public_legal as legal_web_service

router = APIRouter(prefix="/legal", tags=["public-legal"])


@router.get("/privacy", response_class=HTMLResponse)
def privacy_policy(request: Request, db: Session = Depends(get_db)):
    """View privacy policy."""
    return legal_web_service.privacy_policy(request, db)


@router.get("/terms", response_class=HTMLResponse)
def terms_of_service(request: Request, db: Session = Depends(get_db)):
    """View terms of service."""
    return legal_web_service.terms_of_service(request, db)


@router.get("/acceptable-use", response_class=HTMLResponse)
def acceptable_use(request: Request, db: Session = Depends(get_db)):
    """View acceptable use policy."""
    return legal_web_service.acceptable_use(request, db)


@router.get("/sla", response_class=HTMLResponse)
def service_level_agreement(request: Request, db: Session = Depends(get_db)):
    """View service level agreement."""
    return legal_web_service.service_level_agreement(request, db)


@router.get("/cookies", response_class=HTMLResponse)
def cookie_policy(request: Request, db: Session = Depends(get_db)):
    """View cookie policy."""
    return legal_web_service.cookie_policy(request, db)


@router.get("/refunds", response_class=HTMLResponse)
def refund_policy(request: Request, db: Session = Depends(get_db)):
    """View refund policy."""
    return legal_web_service.refund_policy(request, db)


@router.get("/{slug}", response_class=HTMLResponse)
def legal_document_by_slug(
    request: Request, slug: str, db: Session = Depends(get_db)
):
    """View any legal document by slug."""
    return legal_web_service.legal_document_by_slug(request, db, slug)


@router.get("/{document_id}/download")
def download_document(document_id: str, db: Session = Depends(get_db)):
    """Download a legal document file."""
    return legal_web_service.download_document(db, document_id)
