"""Public web routes (unauthenticated)."""

from fastapi import APIRouter

from app.web.public.branding import router as branding_router
from app.web.public.legal import router as legal_router
from app.web.public.network_graphs import router as network_graphs_router

router = APIRouter(tags=["web-public"])

router.include_router(branding_router)
router.include_router(legal_router)
router.include_router(network_graphs_router)

__all__ = ["router"]
