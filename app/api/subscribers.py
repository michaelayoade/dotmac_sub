from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session
from app.schemas.common import ListResponse

from app.db import SessionLocal
from app.schemas.subscriber import (
    AccountRoleCreate,
    AccountRoleRead,
    AccountRoleUpdate,
    AddressCreate,
    AddressRead,
    AddressUpdate,
    OrganizationCreate,
    OrganizationRead,
    OrganizationUpdate,
    ResellerCreate,
    ResellerRead,
    ResellerUpdate,
    SubscriberAccountCreate,
    SubscriberAccountRead,
    SubscriberAccountUpdate,
    SubscriberCreate,
    SubscriberCustomFieldCreate,
    SubscriberCustomFieldRead,
    SubscriberCustomFieldUpdate,
    SubscriberRead,
    SubscriberUpdate,
)
from app.services import subscriber as subscriber_service

router = APIRouter()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.post(
    "/organizations",
    response_model=OrganizationRead,
    status_code=status.HTTP_201_CREATED,
    tags=["organizations"],
)
def create_organization(payload: OrganizationCreate, db: Session = Depends(get_db)):
    return subscriber_service.organizations.create(db, payload)


@router.get(
    "/organizations/{organization_id}",
    response_model=OrganizationRead,
    tags=["organizations"],
)
def get_organization(organization_id: str, db: Session = Depends(get_db)):
    return subscriber_service.organizations.get(db, organization_id)


@router.get("/organizations", response_model=ListResponse[OrganizationRead], tags=["organizations"])
def list_organizations(
    name: str | None = Query(default=None, max_length=160),
    order_by: str = Query(default="name"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return subscriber_service.organizations.list_response(
        db, name, order_by, order_dir, limit, offset
    )


@router.patch(
    "/organizations/{organization_id}",
    response_model=OrganizationRead,
    tags=["organizations"],
)
def update_organization(
    organization_id: str, payload: OrganizationUpdate, db: Session = Depends(get_db)
):
    return subscriber_service.organizations.update(db, organization_id, payload)


@router.delete(
    "/organizations/{organization_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["organizations"],
)
def delete_organization(organization_id: str, db: Session = Depends(get_db)):
    subscriber_service.organizations.delete(db, organization_id)


@router.post(
    "/resellers",
    response_model=ResellerRead,
    status_code=status.HTTP_201_CREATED,
    tags=["resellers"],
)
def create_reseller(payload: ResellerCreate, db: Session = Depends(get_db)):
    return subscriber_service.resellers.create(db, payload)


@router.get(
    "/resellers/{reseller_id}",
    response_model=ResellerRead,
    tags=["resellers"],
)
def get_reseller(reseller_id: str, db: Session = Depends(get_db)):
    return subscriber_service.resellers.get(db, reseller_id)


@router.get("/resellers", response_model=ListResponse[ResellerRead], tags=["resellers"])
def list_resellers(
    is_active: bool | None = None,
    order_by: str = Query(default="name"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return subscriber_service.resellers.list_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/resellers/{reseller_id}",
    response_model=ResellerRead,
    tags=["resellers"],
)
def update_reseller(
    reseller_id: str, payload: ResellerUpdate, db: Session = Depends(get_db)
):
    return subscriber_service.resellers.update(db, reseller_id, payload)


@router.delete(
    "/resellers/{reseller_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["resellers"],
)
def delete_reseller(reseller_id: str, db: Session = Depends(get_db)):
    subscriber_service.resellers.delete(db, reseller_id)


@router.post(
    "/subscribers",
    response_model=SubscriberRead,
    status_code=status.HTTP_201_CREATED,
    tags=["subscribers"],
)
def create_subscriber(payload: SubscriberCreate, db: Session = Depends(get_db)):
    return subscriber_service.subscribers.create(db, payload)


@router.get(
    "/subscribers/{subscriber_id}",
    response_model=SubscriberRead,
    tags=["subscribers"],
)
def get_subscriber(subscriber_id: str, db: Session = Depends(get_db)):
    return subscriber_service.subscribers.get(db, subscriber_id)


@router.get(
    "/subscribers",
    response_model=ListResponse[SubscriberRead],
    tags=["subscribers"],
)
def list_subscribers(
    subscriber_type: str | None = None,
    person_id: str | None = None,
    organization_id: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return subscriber_service.subscribers.list_response(
        db, person_id, organization_id, subscriber_type, order_by, order_dir, limit, offset
    )


@router.patch(
    "/subscribers/{subscriber_id}",
    response_model=SubscriberRead,
    tags=["subscribers"],
)
def update_subscriber(
    subscriber_id: str, payload: SubscriberUpdate, db: Session = Depends(get_db)
):
    return subscriber_service.subscribers.update(db, subscriber_id, payload)


@router.delete(
    "/subscribers/{subscriber_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["subscribers"],
)
def delete_subscriber(subscriber_id: str, db: Session = Depends(get_db)):
    subscriber_service.subscribers.delete(db, subscriber_id)


@router.post(
    "/subscriber-accounts",
    response_model=SubscriberAccountRead,
    status_code=status.HTTP_201_CREATED,
    tags=["subscriber-accounts"],
)
def create_subscriber_account(
    payload: SubscriberAccountCreate, db: Session = Depends(get_db)
):
    return subscriber_service.accounts.create(db, payload)


@router.get(
    "/subscriber-accounts/{account_id}",
    response_model=SubscriberAccountRead,
    tags=["subscriber-accounts"],
)
def get_subscriber_account(account_id: str, db: Session = Depends(get_db)):
    return subscriber_service.accounts.get(db, account_id)


@router.get(
    "/subscriber-accounts",
    response_model=ListResponse[SubscriberAccountRead],
    tags=["subscriber-accounts"],
)
def list_subscriber_accounts(
    subscriber_id: str | None = None,
    reseller_id: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return subscriber_service.accounts.list_response(
        db, subscriber_id, reseller_id, order_by, order_dir, limit, offset
    )


@router.patch(
    "/subscriber-accounts/{account_id}",
    response_model=SubscriberAccountRead,
    tags=["subscriber-accounts"],
)
def update_subscriber_account(
    account_id: str, payload: SubscriberAccountUpdate, db: Session = Depends(get_db)
):
    return subscriber_service.accounts.update(db, account_id, payload)


@router.delete(
    "/subscriber-accounts/{account_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["subscriber-accounts"],
)
def delete_subscriber_account(account_id: str, db: Session = Depends(get_db)):
    subscriber_service.accounts.delete(db, account_id)


@router.post(
    "/account-roles",
    response_model=AccountRoleRead,
    status_code=status.HTTP_201_CREATED,
    tags=["account-roles"],
)
def create_account_role(payload: AccountRoleCreate, db: Session = Depends(get_db)):
    return subscriber_service.account_roles.create(db, payload)


@router.get(
    "/account-roles/{role_id}",
    response_model=AccountRoleRead,
    tags=["account-roles"],
)
def get_account_role(role_id: str, db: Session = Depends(get_db)):
    return subscriber_service.account_roles.get(db, role_id)


@router.get(
    "/account-roles",
    response_model=ListResponse[AccountRoleRead],
    tags=["account-roles"],
)
def list_account_roles(
    account_id: str | None = None,
    person_id: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return subscriber_service.account_roles.list_response(
        db, account_id, person_id, order_by, order_dir, limit, offset
    )


@router.patch(
    "/account-roles/{role_id}",
    response_model=AccountRoleRead,
    tags=["account-roles"],
)
def update_account_role(
    role_id: str, payload: AccountRoleUpdate, db: Session = Depends(get_db)
):
    return subscriber_service.account_roles.update(db, role_id, payload)


@router.delete(
    "/account-roles/{role_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["account-roles"],
)
def delete_account_role(role_id: str, db: Session = Depends(get_db)):
    subscriber_service.account_roles.delete(db, role_id)


@router.post(
    "/addresses",
    response_model=AddressRead,
    status_code=status.HTTP_201_CREATED,
    tags=["addresses"],
)
def create_address(payload: AddressCreate, db: Session = Depends(get_db)):
    return subscriber_service.addresses.create(db, payload)


@router.get(
    "/addresses/{address_id}",
    response_model=AddressRead,
    tags=["addresses"],
)
def get_address(address_id: str, db: Session = Depends(get_db)):
    return subscriber_service.addresses.get(db, address_id)


@router.get(
    "/addresses",
    response_model=ListResponse[AddressRead],
    tags=["addresses"],
)
def list_addresses(
    subscriber_id: str | None = None,
    account_id: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return subscriber_service.addresses.list_response(
        db, subscriber_id, account_id, order_by, order_dir, limit, offset
    )


@router.patch(
    "/addresses/{address_id}",
    response_model=AddressRead,
    tags=["addresses"],
)
def update_address(
    address_id: str, payload: AddressUpdate, db: Session = Depends(get_db)
):
    return subscriber_service.addresses.update(db, address_id, payload)


@router.delete(
    "/addresses/{address_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["addresses"],
)
def delete_address(address_id: str, db: Session = Depends(get_db)):
    subscriber_service.addresses.delete(db, address_id)


@router.post(
    "/subscriber-custom-fields",
    response_model=SubscriberCustomFieldRead,
    status_code=status.HTTP_201_CREATED,
    tags=["subscriber-custom-fields"],
)
def create_subscriber_custom_field(
    payload: SubscriberCustomFieldCreate, db: Session = Depends(get_db)
):
    return subscriber_service.subscriber_custom_fields.create(db, payload)


@router.get(
    "/subscriber-custom-fields/{custom_field_id}",
    response_model=SubscriberCustomFieldRead,
    tags=["subscriber-custom-fields"],
)
def get_subscriber_custom_field(custom_field_id: str, db: Session = Depends(get_db)):
    return subscriber_service.subscriber_custom_fields.get(db, custom_field_id)


@router.get(
    "/subscriber-custom-fields",
    response_model=ListResponse[SubscriberCustomFieldRead],
    tags=["subscriber-custom-fields"],
)
def list_subscriber_custom_fields(
    subscriber_id: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return subscriber_service.subscriber_custom_fields.list_response(
        db, subscriber_id, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/subscriber-custom-fields/{custom_field_id}",
    response_model=SubscriberCustomFieldRead,
    tags=["subscriber-custom-fields"],
)
def update_subscriber_custom_field(
    custom_field_id: str,
    payload: SubscriberCustomFieldUpdate,
    db: Session = Depends(get_db),
):
    return subscriber_service.subscriber_custom_fields.update(
        db, custom_field_id, payload
    )


@router.delete(
    "/subscriber-custom-fields/{custom_field_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["subscriber-custom-fields"],
)
def delete_subscriber_custom_field(
    custom_field_id: str, db: Session = Depends(get_db)
):
    subscriber_service.subscriber_custom_fields.delete(db, custom_field_id)
