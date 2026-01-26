"""Public web routes (unauthenticated)."""

from fastapi import APIRouter

from app.web.public.legal import router as legal_router
from app.web.public.crm_webhooks import router as crm_webhooks_router

router = APIRouter(tags=["web-public"])

router.include_router(legal_router)
router.include_router(crm_webhooks_router)

__all__ = ["router"]
