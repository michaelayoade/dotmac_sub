"""Public web routes (unauthenticated)."""

from fastapi import APIRouter

from app.web.public.legal import router as legal_router

router = APIRouter(tags=["web-public"])

router.include_router(legal_router)

__all__ = ["router"]
