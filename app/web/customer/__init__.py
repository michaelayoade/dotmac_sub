"""Customer portal web routes."""

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.web.customer.routes import router as portal_router
from app.web.customer.auth import router as auth_router
from app.web.customer.contracts import router as contracts_router

router = APIRouter()


@router.get("/customer")
def customer_root_redirect(request: Request):
    return RedirectResponse(url="/portal", status_code=303)


@router.get("/customer/{path:path}")
def customer_legacy_redirect(request: Request, path: str):
    target = f"/portal/{path}" if path else "/portal"
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return RedirectResponse(url=target, status_code=303)


router.include_router(auth_router)
router.include_router(portal_router)
router.include_router(contracts_router)

__all__ = ["router"]
