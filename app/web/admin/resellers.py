"""Admin reseller portal web routes."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.orm import Session, selectinload

from app.db import SessionLocal
from app.models.auth import AuthProvider
from app.models.subscriber import Reseller, ResellerUser
from app.schemas.auth import UserCredentialCreate
# TODO: PersonCreate schema no longer exists - use subscriber service instead
# from app.schemas.person import PersonCreate
from app.schemas.subscriber import ResellerCreate, ResellerUpdate
from app.schemas.rbac import PersonRoleCreate
from app.services import auth as auth_service
# TODO: person service no longer exists - use subscriber service instead
# from app.services import person as person_service
from app.services import rbac as rbac_service
from app.services import subscriber as subscriber_service
from app.services.auth_flow import hash_password
from app.services.common import coerce_uuid
from app.models.rbac import Role

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/resellers", tags=["web-admin-resellers"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _base_context(request: Request, db: Session, active_page: str):
    from app.web.admin import get_sidebar_stats, get_current_user
    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "resellers",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


# TODO: _create_subscriber_credential - PersonCreate schema and person_service removed
def _create_subscriber_credential(
    db: Session,
    first_name: str,
    last_name: str,
    email: str,
    username: str,
    password: str,
):
    # TODO: PersonCreate schema no longer exists - create subscriber directly
    # person_payload = PersonCreate(...)
    # person = person_service.people.create(db=db, payload=person_payload)
    # Use subscriber_service.subscribers.create instead
    raise NotImplementedError("PersonCreate schema removed - use subscriber_service.subscribers.create")
    # credential_payload = UserCredentialCreate(
    #     subscriber_id=subscriber.id,
    #     provider=AuthProvider.local,
    #     username=username,
    #     password_hash=hash_password(password),
    # )
    # auth_service.user_credentials.create(db=db, payload=credential_payload)
    # return subscriber


@router.get("", response_class=HTMLResponse)
def resellers_list(request: Request, db: Session = Depends(get_db)):
    resellers = subscriber_service.resellers.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    context = _base_context(request, db, active_page="resellers")
    context.update({"resellers": resellers})
    return templates.TemplateResponse("admin/resellers/index.html", context)


@router.get("/new", response_class=HTMLResponse)
def reseller_new(request: Request, db: Session = Depends(get_db)):
    context = _base_context(request, db, active_page="resellers")
    roles = rbac_service.roles.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    context.update({"reseller": None, "action_url": "/admin/resellers", "roles": roles})
    return templates.TemplateResponse("admin/resellers/reseller_form.html", context)

@router.get("/{reseller_id}/edit", response_class=HTMLResponse)
def reseller_edit(reseller_id: str, request: Request, db: Session = Depends(get_db)):
    reseller = subscriber_service.resellers.get(db=db, reseller_id=reseller_id)
    context = _base_context(request, db, active_page="resellers")
    context.update(
        {
            "reseller": reseller,
            "action_url": f"/admin/resellers/{reseller.id}",
        }
    )
    return templates.TemplateResponse("admin/resellers/reseller_form.html", context)


@router.post("", response_class=HTMLResponse)
async def reseller_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    create_user = bool(form.get("create_user"))
    payload = {
        "name": (form.get("name") or "").strip(),
        "code": (form.get("code") or "").strip() or None,
        "contact_email": (form.get("contact_email") or "").strip() or None,
        "contact_phone": (form.get("contact_phone") or "").strip() or None,
        "notes": (form.get("notes") or "").strip() or None,
        "is_active": bool(form.get("is_active")),
    }
    user_payload = None
    if create_user:
        user_payload = {
            "first_name": (form.get("user_first_name") or "").strip(),
            "last_name": (form.get("user_last_name") or "").strip(),
            "email": (form.get("user_email") or "").strip(),
            "username": (form.get("user_username") or "").strip(),
            "password": (form.get("user_password") or "").strip(),
            "role": (form.get("user_role") or "").strip() or None,
        }
        missing = [key for key, value in user_payload.items() if key != "role" and not value]
        if missing:
            context = _base_context(request, db, active_page="resellers")
            roles = rbac_service.roles.list(
                db=db,
                is_active=True,
                order_by="name",
                order_dir="asc",
                limit=500,
                offset=0,
            )
            context.update(
                {
                    "reseller": payload,
                    "action_url": "/admin/resellers",
                    "roles": roles,
                    "error": "Provide all user fields to create a login.",
                }
            )
            return templates.TemplateResponse("admin/resellers/reseller_form.html", context, status_code=400)
    try:
        data = ResellerCreate(**payload)
    except ValidationError as exc:
        context = _base_context(request, db, active_page="resellers")
        roles = rbac_service.roles.list(
            db=db,
            is_active=True,
            order_by="name",
            order_dir="asc",
            limit=500,
            offset=0,
        )
        context.update(
            {
                "reseller": payload,
                "action_url": "/admin/resellers",
                "roles": roles,
                "error": exc.errors()[0].get("msg", "Invalid reseller details."),
            }
        )
        return templates.TemplateResponse("admin/resellers/reseller_form.html", context, status_code=400)
    reseller = subscriber_service.resellers.create(db=db, payload=data)
    if user_payload:
        try:
            # TODO: _create_subscriber_credential function needs reimplementation
            subscriber = _create_subscriber_credential(
                db=db,
                first_name=user_payload["first_name"],
                last_name=user_payload["last_name"],
                email=user_payload["email"],
                username=user_payload["username"],
                password=user_payload["password"],
            )
            if user_payload["role"]:
                role = db.query(Role).filter(Role.name == user_payload["role"]).first()
                if role:
                    rbac_service.person_roles.create(
                        db,
                        PersonRoleCreate(subscriber_id=subscriber.id, role_id=role.id),
                    )
            link = ResellerUser(
                reseller_id=reseller.id,
                subscriber_id=subscriber.id,
                is_active=True,
            )
            db.add(link)
            db.commit()
        except Exception as exc:
            context = _base_context(request, db, active_page="resellers")
            roles = rbac_service.roles.list(
                db=db,
                is_active=True,
                order_by="name",
                order_dir="asc",
                limit=500,
                offset=0,
            )
            context.update(
                {
                    "reseller": payload,
                    "action_url": "/admin/resellers",
                    "roles": roles,
                    "error": str(exc) or "Unable to create login user.",
                }
            )
            return templates.TemplateResponse("admin/resellers/reseller_form.html", context, status_code=400)
    return RedirectResponse(url="/admin/resellers", status_code=303)

@router.post("/{reseller_id}", response_class=HTMLResponse)
async def reseller_update(reseller_id: str, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    payload = {
        "name": (form.get("name") or "").strip(),
        "code": (form.get("code") or "").strip() or None,
        "contact_email": (form.get("contact_email") or "").strip() or None,
        "contact_phone": (form.get("contact_phone") or "").strip() or None,
        "notes": (form.get("notes") or "").strip() or None,
        "is_active": bool(form.get("is_active")),
    }
    try:
        data = ResellerUpdate(**payload)
    except ValidationError as exc:
        context = _base_context(request, db, active_page="resellers")
        payload.update({"id": reseller_id})
        context.update(
            {
                "reseller": payload,
                "action_url": f"/admin/resellers/{reseller_id}",
                "error": exc.errors()[0].get("msg", "Invalid reseller details."),
            }
        )
        return templates.TemplateResponse("admin/resellers/reseller_form.html", context, status_code=400)
    try:
        subscriber_service.resellers.update(db=db, reseller_id=reseller_id, payload=data)
    except Exception as exc:
        context = _base_context(request, db, active_page="resellers")
        payload.update({"id": reseller_id})
        context.update(
            {
                "reseller": payload,
                "action_url": f"/admin/resellers/{reseller_id}",
                "error": str(exc) or "Unable to update reseller.",
            }
        )
        return templates.TemplateResponse("admin/resellers/reseller_form.html", context, status_code=400)
    return RedirectResponse(url="/admin/resellers", status_code=303)


@router.get("/{reseller_id}", response_class=HTMLResponse)
def reseller_detail(reseller_id: str, request: Request, db: Session = Depends(get_db)):
    reseller = (
        db.query(Reseller)
        .options(selectinload(Reseller.users).selectinload(ResellerUser.subscriber))
        .filter(Reseller.id == coerce_uuid(reseller_id))
        .first()
    )
    if not reseller:
        return RedirectResponse(url="/admin/resellers", status_code=303)
    # TODO: person_service removed - use subscriber_service instead
    subscribers = subscriber_service.subscribers.list(
        db=db,
        organization_id=None,
        reseller_id=None,
        status=None,
        is_active=True,
        order_by="last_name",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    context = _base_context(request, db, active_page="resellers")
    context.update(
        {
            "reseller": reseller,
            "reseller_users": reseller.users,
            "people": subscribers,  # Keep template variable name for compatibility
        }
    )
    return templates.TemplateResponse("admin/resellers/detail.html", context)


@router.post("/{reseller_id}/users/link", response_class=HTMLResponse)
async def reseller_user_link(reseller_id: str, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    subscriber_id = (form.get("subscriber_id") or form.get("person_id") or "").strip()
    if not subscriber_id:
        return RedirectResponse(url=f"/admin/resellers/{reseller_id}", status_code=303)
    existing = (
        db.query(ResellerUser)
        .filter(ResellerUser.reseller_id == coerce_uuid(reseller_id))
        .filter(ResellerUser.subscriber_id == coerce_uuid(subscriber_id))
        .first()
    )
    if existing:
        return RedirectResponse(url=f"/admin/resellers/{reseller_id}", status_code=303)
    link = ResellerUser(
        reseller_id=coerce_uuid(reseller_id),
        subscriber_id=coerce_uuid(subscriber_id),
        is_active=True,
    )
    db.add(link)
    db.commit()
    return RedirectResponse(url=f"/admin/resellers/{reseller_id}", status_code=303)


@router.post("/{reseller_id}/users/create", response_class=HTMLResponse)
async def reseller_user_create(reseller_id: str, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    fields = {
        "first_name": (form.get("first_name") or "").strip(),
        "last_name": (form.get("last_name") or "").strip(),
        "email": (form.get("email") or "").strip(),
        "username": (form.get("username") or "").strip(),
        "password": (form.get("password") or "").strip(),
    }
    if not all([fields["first_name"], fields["last_name"], fields["email"], fields["username"], fields["password"]]):
        context = _base_context(request, db, active_page="resellers")
        reseller = db.get(Reseller, coerce_uuid(reseller_id))
        # TODO: person_service removed - use subscriber_service instead
        subscribers = subscriber_service.subscribers.list(
            db=db,
            organization_id=None,
            reseller_id=None,
            status=None,
            is_active=True,
            order_by="last_name",
            order_dir="asc",
            limit=500,
            offset=0,
        )
        context.update(
            {
                "reseller": reseller,
                "reseller_users": reseller.users if reseller else [],
                "people": subscribers,  # Keep template variable name for compatibility
                "error": "All user fields are required to create a login.",
            }
        )
        return templates.TemplateResponse("admin/resellers/detail.html", context, status_code=400)
    try:
        # TODO: _create_subscriber_credential function needs reimplementation
        subscriber = _create_subscriber_credential(
            db=db,
            first_name=fields["first_name"],
            last_name=fields["last_name"],
            email=fields["email"],
            username=fields["username"],
            password=fields["password"],
        )
        link = ResellerUser(
            reseller_id=coerce_uuid(reseller_id),
            subscriber_id=subscriber.id,
            is_active=True,
        )
        db.add(link)
        db.commit()
    except Exception as exc:
        context = _base_context(request, db, active_page="resellers")
        reseller = db.get(Reseller, coerce_uuid(reseller_id))
        # TODO: person_service removed - use subscriber_service instead
        subscribers = subscriber_service.subscribers.list(
            db=db,
            organization_id=None,
            reseller_id=None,
            status=None,
            is_active=True,
            order_by="last_name",
            order_dir="asc",
            limit=500,
            offset=0,
        )
        context.update(
            {
                "reseller": reseller,
                "reseller_users": reseller.users if reseller else [],
                "people": subscribers,  # Keep template variable name for compatibility
                "error": str(exc) or "Unable to create reseller user.",
            }
        )
        return templates.TemplateResponse("admin/resellers/detail.html", context, status_code=400)
    return RedirectResponse(url=f"/admin/resellers/{reseller_id}", status_code=303)
