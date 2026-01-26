"""Reseller portal web routes."""

from fastapi import APIRouter

from app.web.reseller.auth import router as reseller_auth_router
from app.web.reseller.routes import router as reseller_routes_router

router = APIRouter(tags=["web-reseller"])
router.include_router(reseller_auth_router)
router.include_router(reseller_routes_router)

__all__ = ["router"]
