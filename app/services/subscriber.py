from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

from app.models.subscriber import (
    AccountRole,
    AccountRoleType,
    AccountStatus,
    Address,
    AddressType,
    Organization,
    Reseller,
    Subscriber,
    SubscriberAccount,
    SubscriberCustomField,
)
from app.models.domain_settings import SettingDomain
from app.services.common import apply_ordering, apply_pagination, coerce_uuid, validate_enum
from app.services.response import ListResponseMixin
from app.models.billing import TaxRate
from app.schemas.subscriber import (
    AccountRoleCreate,
    AccountRoleUpdate,
    AddressCreate,
    AddressUpdate,
    OrganizationCreate,
    OrganizationUpdate,
    ResellerCreate,
    ResellerUpdate,
    SubscriberAccountCreate,
    SubscriberAccountUpdate,
    SubscriberCreate,
    SubscriberCustomFieldCreate,
    SubscriberCustomFieldUpdate,
    SubscriberUpdate,
)
from app.validators import subscriber as subscriber_validators
from app.services import settings_spec
from app.services import numbering
from app.services import geocoding as geocoding_service
from app.services.events import emit_event
from app.services.events.types import EventType

_validate_enum = validate_enum


def _validate_tax_rate(db: Session, tax_rate_id: str | None):
    if not tax_rate_id:
        return None
    rate = db.get(TaxRate, tax_rate_id)
    if not rate:
        raise HTTPException(status_code=404, detail="Tax rate not found")
    return rate


class Organizations(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: OrganizationCreate):
        organization = Organization(**payload.model_dump())
        db.add(organization)
        db.commit()
        db.refresh(organization)
        return organization

    @staticmethod
    def get(db: Session, organization_id: str):
        organization = db.get(Organization, organization_id)
        if not organization:
            raise HTTPException(status_code=404, detail="Organization not found")
        return organization

    @staticmethod
    def list(
        db: Session,
        name: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(Organization)
        if name:
            query = query.filter(Organization.name.ilike(f"%{name}%"))
        query = apply_ordering(query, order_by, order_dir, {"name": Organization.name})
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, organization_id: str, payload: OrganizationUpdate):
        organization = db.get(Organization, organization_id)
        if not organization:
            raise HTTPException(status_code=404, detail="Organization not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(organization, key, value)
        db.commit()
        db.refresh(organization)
        return organization

    @staticmethod
    def delete(db: Session, organization_id: str):
        organization = db.get(Organization, organization_id)
        if not organization:
            raise HTTPException(status_code=404, detail="Organization not found")
        db.delete(organization)
        db.commit()


class Resellers(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: ResellerCreate):
        reseller = Reseller(**payload.model_dump())
        db.add(reseller)
        db.commit()
        db.refresh(reseller)
        return reseller

    @staticmethod
    def get(db: Session, reseller_id: str):
        reseller = db.get(Reseller, reseller_id)
        if not reseller:
            raise HTTPException(status_code=404, detail="Reseller not found")
        return reseller

    @staticmethod
    def list(
        db: Session,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(Reseller)
        if is_active is None:
            query = query.filter(Reseller.is_active.is_(True))
        else:
            query = query.filter(Reseller.is_active == is_active)
        query = apply_ordering(query, order_by, order_dir, {"name": Reseller.name})
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, reseller_id: str, payload: ResellerUpdate):
        reseller = db.get(Reseller, reseller_id)
        if not reseller:
            raise HTTPException(status_code=404, detail="Reseller not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(reseller, key, value)
        db.commit()
        db.refresh(reseller)
        return reseller

    @staticmethod
    def delete(db: Session, reseller_id: str):
        reseller = db.get(Reseller, reseller_id)
        if not reseller:
            raise HTTPException(status_code=404, detail="Reseller not found")
        db.delete(reseller)
        db.commit()


class Subscribers(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: SubscriberCreate):
        data = payload.model_dump()
        data.pop("organization_id", None)
        if not data.get("subscriber_number"):
            generated = numbering.generate_number(
                db,
                SettingDomain.subscriber,
                "subscriber_number",
                "subscriber_number_enabled",
                "subscriber_number_prefix",
                "subscriber_number_padding",
                "subscriber_number_start",
            )
            if generated:
                data["subscriber_number"] = generated
        subscriber = Subscriber(**data)
        db.add(subscriber)
        db.commit()
        db.refresh(subscriber)

        # Emit subscriber.created event
        emit_event(
            db,
            EventType.subscriber_created,
            {
                "subscriber_id": str(subscriber.id),
                "subscriber_number": subscriber.subscriber_number,
            },
            subscriber_id=subscriber.id,
        )

        return subscriber

    @staticmethod
    def get(db: Session, subscriber_id: str):
        subscriber = db.get(
            Subscriber,
            subscriber_id,
            options=[
                selectinload(Subscriber.accounts)
                .selectinload(SubscriberAccount.account_roles)
                .selectinload(AccountRole.subscriber),
                selectinload(Subscriber.accounts)
                .selectinload(SubscriberAccount.account_roles)
                .selectinload(AccountRole.subscriber),
                selectinload(Subscriber.addresses),
            ],
        )
        if not subscriber:
            raise HTTPException(status_code=404, detail="Subscriber not found")
        return subscriber

    @staticmethod
    def list(
        db: Session,
        organization_id: str | None,
        subscriber_type: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(Subscriber).options(
            selectinload(Subscriber.accounts)
            .selectinload(SubscriberAccount.account_roles)
            .selectinload(AccountRole.subscriber),
            selectinload(Subscriber.accounts)
            .selectinload(SubscriberAccount.account_roles)
            .selectinload(AccountRole.subscriber),
            selectinload(Subscriber.addresses),
        )
        if organization_id:
            query = query.filter(Subscriber.organization_id == coerce_uuid(organization_id))
        if subscriber_type:
            normalized = subscriber_type.strip().lower()
            if normalized == "person":
                query = query.filter(Subscriber.organization_id.is_(None))
            elif normalized == "organization":
                query = query.filter(Subscriber.organization_id.is_not(None))
            else:
                raise HTTPException(status_code=400, detail="Invalid subscriber_type")
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": Subscriber.created_at,
                "updated_at": Subscriber.updated_at,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, subscriber_id: str, payload: SubscriberUpdate):
        subscriber = db.get(Subscriber, subscriber_id)
        if not subscriber:
            raise HTTPException(status_code=404, detail="Subscriber not found")
        data = payload.model_dump(exclude_unset=True)
        data.pop("organization_id", None)
        for key, value in data.items():
            setattr(subscriber, key, value)
        db.commit()
        db.refresh(subscriber)

        # Emit subscriber.updated event
        emit_event(
            db,
            EventType.subscriber_updated,
            {
                "subscriber_id": str(subscriber.id),
                "subscriber_number": subscriber.subscriber_number,
                "updated_fields": list(data.keys()),
            },
            subscriber_id=subscriber.id,
        )

        return subscriber

    @staticmethod
    def delete(db: Session, subscriber_id: str):
        subscriber = db.get(Subscriber, subscriber_id)
        if not subscriber:
            raise HTTPException(status_code=404, detail="Subscriber not found")
        db.delete(subscriber)
        db.commit()

    @staticmethod
    def count_stats(db: Session) -> dict:
        """Return subscriber counts for dashboard stats."""
        total = db.query(func.count(Subscriber.id)).scalar() or 0
        active = (
            db.query(func.count(Subscriber.id))
            .filter(Subscriber.is_active.is_(True))
            .scalar() or 0
        )
        # Count individuals (subscribers without organization)
        persons = (
            db.query(func.count(Subscriber.id))
            .filter(Subscriber.organization_id.is_(None))
            .scalar() or 0
        )
        # Count organizations (subscribers with organization)
        organizations = (
            db.query(func.count(Subscriber.id))
            .filter(Subscriber.organization_id.is_not(None))
            .scalar() or 0
        )
        return {
            "total": total,
            "active": active,
            "persons": persons,
            "organizations": organizations,
        }


class Accounts(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: SubscriberAccountCreate):
        subscriber = db.get(Subscriber, payload.subscriber_id)
        if not subscriber:
            raise HTTPException(status_code=404, detail="Subscriber not found")
        if payload.reseller_id:
            reseller = db.get(Reseller, payload.reseller_id)
            if not reseller:
                raise HTTPException(status_code=404, detail="Reseller not found")
        if payload.tax_rate_id:
            _validate_tax_rate(db, str(payload.tax_rate_id))
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "status" not in fields_set:
            default_status = settings_spec.resolve_value(
                db, SettingDomain.subscriber, "default_account_status"
            )
            if default_status:
                data["status"] = _validate_enum(
                    default_status, AccountStatus, "status"
                )
        if not data.get("account_number"):
            generated = numbering.generate_number(
                db,
                SettingDomain.subscriber,
                "account_number",
                "account_number_enabled",
                "account_number_prefix",
                "account_number_padding",
                "account_number_start",
            )
            if generated:
                data["account_number"] = generated
        account = SubscriberAccount(**data)
        db.add(account)
        db.commit()
        db.refresh(account)
        return account

    @staticmethod
    def get(db: Session, account_id: str):
        account = db.get(
            SubscriberAccount,
            account_id,
            options=[
                selectinload(SubscriberAccount.account_roles).selectinload(AccountRole.subscriber),
            ],
        )
        if not account:
            raise HTTPException(status_code=404, detail="Subscriber account not found")
        return account

    @staticmethod
    def list(
        db: Session,
        subscriber_id: str | None,
        reseller_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(SubscriberAccount).options(
            selectinload(SubscriberAccount.account_roles).selectinload(AccountRole.subscriber),
        )
        if subscriber_id:
            query = query.filter(SubscriberAccount.subscriber_id == subscriber_id)
        if reseller_id:
            query = query.filter(SubscriberAccount.reseller_id == reseller_id)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": SubscriberAccount.created_at,
                "updated_at": SubscriberAccount.updated_at,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, account_id: str, payload: SubscriberAccountUpdate):
        account = db.get(SubscriberAccount, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="Subscriber account not found")
        data = payload.model_dump(exclude_unset=True)
        if "subscriber_id" in data and data["subscriber_id"]:
            subscriber = db.get(Subscriber, data["subscriber_id"])
            if not subscriber:
                raise HTTPException(status_code=404, detail="Subscriber not found")
        if "reseller_id" in data and data["reseller_id"]:
            reseller = db.get(Reseller, data["reseller_id"])
            if not reseller:
                raise HTTPException(status_code=404, detail="Reseller not found")
        if "tax_rate_id" in data and data["tax_rate_id"]:
            _validate_tax_rate(db, str(data["tax_rate_id"]))
        for key, value in data.items():
            setattr(account, key, value)
        db.commit()
        db.refresh(account)
        return account

    @staticmethod
    def delete(db: Session, account_id: str):
        account = db.get(SubscriberAccount, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="Subscriber account not found")
        db.delete(account)
        db.commit()


class AccountRoles(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: AccountRoleCreate):
        account = db.get(SubscriberAccount, payload.account_id)
        if not account:
            raise HTTPException(status_code=404, detail="Subscriber account not found")
        subscriber = db.get(Subscriber, payload.subscriber_id)
        if not subscriber:
            raise HTTPException(status_code=404, detail="Subscriber not found")
        if payload.is_primary:
            db.query(AccountRole).filter(
                AccountRole.account_id == payload.account_id,
                AccountRole.is_primary.is_(True),
            ).update({"is_primary": False})
        role = AccountRole(**payload.model_dump())
        db.add(role)
        db.commit()
        db.refresh(role)
        return role

    @staticmethod
    def get(db: Session, role_id: str):
        role = db.get(AccountRole, role_id)
        if not role:
            raise HTTPException(status_code=404, detail="Account role not found")
        return role

    @staticmethod
    def list(
        db: Session,
        account_id: str | None,
        subscriber_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(AccountRole).options(selectinload(AccountRole.subscriber))
        if account_id:
            query = query.filter(AccountRole.account_id == account_id)
        if subscriber_id:
            query = query.filter(AccountRole.subscriber_id == subscriber_id)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": AccountRole.created_at, "updated_at": AccountRole.updated_at},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, role_id: str, payload: AccountRoleUpdate):
        role = db.get(AccountRole, role_id)
        if not role:
            raise HTTPException(status_code=404, detail="Account role not found")
        data = payload.model_dump(exclude_unset=True)
        if "account_id" in data and data["account_id"]:
            account = db.get(SubscriberAccount, data["account_id"])
            if not account:
                raise HTTPException(status_code=404, detail="Subscriber account not found")
        if "subscriber_id" in data and data["subscriber_id"]:
            subscriber = db.get(Subscriber, data["subscriber_id"])
            if not subscriber:
                raise HTTPException(status_code=404, detail="Subscriber not found")
        if data.get("is_primary"):
            account_id = data.get("account_id", role.account_id)
            db.query(AccountRole).filter(
                AccountRole.account_id == account_id,
                AccountRole.id != role.id,
                AccountRole.is_primary.is_(True),
            ).update({"is_primary": False})
        for key, value in data.items():
            setattr(role, key, value)
        db.commit()
        db.refresh(role)
        return role

    @staticmethod
    def delete(db: Session, role_id: str):
        role = db.get(AccountRole, role_id)
        if not role:
            raise HTTPException(status_code=404, detail="Account role not found")
        db.delete(role)
        db.commit()


class Addresses(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: AddressCreate):
        subscriber = db.get(Subscriber, payload.subscriber_id)
        if not subscriber:
            raise HTTPException(status_code=404, detail="Subscriber not found")
        if payload.account_id:
            account = db.get(SubscriberAccount, payload.account_id)
            if not account:
                raise HTTPException(status_code=404, detail="Subscriber account not found")
            if account.subscriber_id != payload.subscriber_id:
                raise HTTPException(status_code=400, detail="Account does not belong to subscriber")
        if payload.tax_rate_id:
            _validate_tax_rate(db, str(payload.tax_rate_id))
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "address_type" not in fields_set:
            default_type = settings_spec.resolve_value(
                db, SettingDomain.subscriber, "default_address_type"
            )
            if default_type:
                data["address_type"] = _validate_enum(
                    default_type, AddressType, "address_type"
                )
        if data.get("is_primary"):
            db.query(Address).filter(
                Address.subscriber_id == data["subscriber_id"],
                Address.account_id == data.get("account_id"),
                Address.address_type == data["address_type"],
                Address.is_primary.is_(True),
            ).update({"is_primary": False})
        data = geocoding_service.geocode_address(db, data)
        address = Address(**data)
        db.add(address)
        db.commit()
        db.refresh(address)
        return address

    @staticmethod
    def get(db: Session, address_id: str):
        address = db.get(Address, address_id)
        if not address:
            raise HTTPException(status_code=404, detail="Address not found")
        return address

    @staticmethod
    def list(
        db: Session,
        subscriber_id: str | None,
        account_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(Address)
        if subscriber_id:
            query = query.filter(Address.subscriber_id == subscriber_id)
        if account_id:
            query = query.filter(Address.account_id == account_id)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": Address.created_at, "updated_at": Address.updated_at},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, address_id: str, payload: AddressUpdate):
        address = db.get(Address, address_id)
        if not address:
            raise HTTPException(status_code=404, detail="Address not found")
        data = payload.model_dump(exclude_unset=True)
        if "subscriber_id" in data:
            subscriber = db.get(Subscriber, data["subscriber_id"])
            if not subscriber:
                raise HTTPException(status_code=404, detail="Subscriber not found")
        if "account_id" in data and data["account_id"]:
            subscriber_id = data.get("subscriber_id", address.subscriber_id)
            account = db.get(SubscriberAccount, data["account_id"])
            if not account:
                raise HTTPException(status_code=404, detail="Subscriber account not found")
            if account.subscriber_id != subscriber_id:
                raise HTTPException(status_code=400, detail="Account does not belong to subscriber")
        if "tax_rate_id" in data and data["tax_rate_id"]:
            _validate_tax_rate(db, str(data["tax_rate_id"]))
        if data.get("is_primary"):
            subscriber_id = data.get("subscriber_id", address.subscriber_id)
            account_id = data.get("account_id", address.account_id)
            address_type = data.get("address_type", address.address_type)
            db.query(Address).filter(
                Address.subscriber_id == subscriber_id,
                Address.account_id == account_id,
                Address.address_type == address_type,
                Address.id != address.id,
                Address.is_primary.is_(True),
            ).update({"is_primary": False})
        if data.get("latitude") is None or data.get("longitude") is None:
            merged = {
                "address_line1": address.address_line1,
                "address_line2": address.address_line2,
                "city": address.city,
                "region": address.region,
                "postal_code": address.postal_code,
                "country_code": address.country_code,
                "latitude": address.latitude,
                "longitude": address.longitude,
            }
            merged.update(data)
            data = geocoding_service.geocode_address(db, merged)
        for key, value in data.items():
            setattr(address, key, value)
        db.commit()
        db.refresh(address)
        return address

    @staticmethod
    def delete(db: Session, address_id: str):
        address = db.get(Address, address_id)
        if not address:
            raise HTTPException(status_code=404, detail="Address not found")
        db.delete(address)
        db.commit()


class SubscriberCustomFields(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: SubscriberCustomFieldCreate):
        subscriber = db.get(Subscriber, payload.subscriber_id)
        if not subscriber:
            raise HTTPException(status_code=404, detail="Subscriber not found")
        custom_field = SubscriberCustomField(**payload.model_dump())
        db.add(custom_field)
        db.commit()
        db.refresh(custom_field)
        return custom_field

    @staticmethod
    def get(db: Session, custom_field_id: str):
        custom_field = db.get(SubscriberCustomField, custom_field_id)
        if not custom_field:
            raise HTTPException(status_code=404, detail="Subscriber custom field not found")
        return custom_field

    @staticmethod
    def list(
        db: Session,
        subscriber_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(SubscriberCustomField)
        if subscriber_id:
            query = query.filter(SubscriberCustomField.subscriber_id == subscriber_id)
        if is_active is None:
            query = query.filter(SubscriberCustomField.is_active.is_(True))
        else:
            query = query.filter(SubscriberCustomField.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": SubscriberCustomField.created_at,
                "key": SubscriberCustomField.key,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, custom_field_id: str, payload: SubscriberCustomFieldUpdate):
        custom_field = db.get(SubscriberCustomField, custom_field_id)
        if not custom_field:
            raise HTTPException(status_code=404, detail="Subscriber custom field not found")
        data = payload.model_dump(exclude_unset=True)
        if "subscriber_id" in data:
            subscriber = db.get(Subscriber, data["subscriber_id"])
            if not subscriber:
                raise HTTPException(status_code=404, detail="Subscriber not found")
        for key, value in data.items():
            setattr(custom_field, key, value)
        db.commit()
        db.refresh(custom_field)
        return custom_field

    @staticmethod
    def delete(db: Session, custom_field_id: str):
        custom_field = db.get(SubscriberCustomField, custom_field_id)
        if not custom_field:
            raise HTTPException(status_code=404, detail="Subscriber custom field not found")
        custom_field.is_active = False
        db.commit()


organizations = Organizations()
resellers = Resellers()
subscribers = Subscribers()
accounts = Accounts()
account_roles = AccountRoles()
addresses = Addresses()
subscriber_custom_fields = SubscriberCustomFields()
