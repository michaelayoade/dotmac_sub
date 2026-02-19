"""Web routes package combining admin, customer, auth, and public routes."""

from fastapi import APIRouter

from app.web.admin import router as admin_router
from app.web.auth import router as auth_router
from app.web.customer import router as customer_router
from app.web.public import router as public_router
from app.web.reseller import router as reseller_router

router = APIRouter(tags=["web"])

# Include all web sub-routers
router.include_router(auth_router)
router.include_router(admin_router)
router.include_router(customer_router)
router.include_router(reseller_router)
router.include_router(public_router)

__all__ = ["router"]
