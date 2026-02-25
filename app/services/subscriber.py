import builtins
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

from app.models.billing import TaxRate
from app.models.domain_settings import SettingDomain
from app.models.subscriber import (
    Address,
    AddressType,
    Organization,
    Reseller,
    Subscriber,
    SubscriberCategory,
    SubscriberCustomField,
    UserType,
)
from app.schemas.subscriber import (
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
from app.services import geocoding as geocoding_service
from app.services import numbering, settings_spec
from app.services.common import (
    apply_ordering,
    apply_pagination,
    coerce_uuid,
    validate_enum,
)
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.response import ListResponseMixin

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
        # Backwards-compat: some callers provide `person_id` to target an existing
        # subscriber row and just apply numbering/defaults.
        person_id = getattr(payload, "person_id", None)
        if person_id:
            subscriber = db.get(Subscriber, str(person_id))
            if not subscriber:
                raise HTTPException(status_code=404, detail="Subscriber not found")
            data = payload.model_dump(exclude_unset=True, exclude={"person_id"})
            category = data.pop("category", None)
            data.pop("organization_id", None)
            for key, value in data.items():
                setattr(subscriber, key, value)
            if category is not None:
                subscriber.category = (
                    category if isinstance(category, SubscriberCategory) else str(category)
                )
            if not subscriber.subscriber_number:
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
                    subscriber.subscriber_number = generated
            if not subscriber.account_number:
                generated_account = numbering.generate_number(
                    db,
                    SettingDomain.subscriber,
                    "account_number",
                    "account_number_enabled",
                    "account_number_prefix",
                    "account_number_padding",
                    "account_number_start",
                )
                if generated_account:
                    subscriber.account_number = generated_account
            db.commit()
            db.refresh(subscriber)
            return subscriber

        data = payload.model_dump()
        category = data.pop("category", None)
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
        if not data.get("account_number"):
            generated_account = numbering.generate_number(
                db,
                SettingDomain.subscriber,
                "account_number",
                "account_number_enabled",
                "account_number_prefix",
                "account_number_padding",
                "account_number_start",
            )
            if generated_account:
                data["account_number"] = generated_account
        subscriber = Subscriber(**data)
        if category is not None:
            subscriber.category = (
                category if isinstance(category, SubscriberCategory) else str(category)
            )
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
        person_id: str | None = None,
    ):
        query = db.query(Subscriber).options(
            selectinload(Subscriber.addresses),
        )
        query = query.filter(Subscriber.user_type != UserType.system_user)
        # Backwards-compat: allow filtering by legacy "person_id" keyword.
        if person_id:
            query = query.filter(Subscriber.id == coerce_uuid(person_id))
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
    def list_active_by_ids(
        db: Session, subscriber_ids: builtins.list[UUID]
    ) -> builtins.list[Subscriber]:
        """Return active subscribers whose ids are in the provided list."""
        if not subscriber_ids:
            return []
        return (
            db.query(Subscriber)
            .filter(Subscriber.id.in_(subscriber_ids))
            .filter(Subscriber.is_active.is_(True))
            .all()
        )

    @staticmethod
    def list_active_by_emails(
        db: Session, emails: builtins.list[str]
    ) -> builtins.list[Subscriber]:
        """Return active subscribers matching any email (case-insensitive)."""
        normalized = [email.strip().lower() for email in emails if email.strip()]
        if not normalized:
            return []
        return (
            db.query(Subscriber)
            .filter(func.lower(Subscriber.email).in_(normalized))
            .filter(Subscriber.is_active.is_(True))
            .all()
        )

    @staticmethod
    def update(db: Session, subscriber_id: str, payload: SubscriberUpdate):
        subscriber = db.get(Subscriber, subscriber_id)
        if not subscriber:
            raise HTTPException(status_code=404, detail="Subscriber not found")
        data = payload.model_dump(exclude_unset=True)
        category = data.pop("category", None)
        data.pop("organization_id", None)
        for key, value in data.items():
            setattr(subscriber, key, value)
        if category is not None:
            subscriber.category = (
                category if isinstance(category, SubscriberCategory) else str(category)
            )
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
        total = (
            db.query(func.count(Subscriber.id))
            .filter(Subscriber.user_type != UserType.system_user)
            .scalar()
            or 0
        )
        active = (
            db.query(func.count(Subscriber.id))
            .filter(Subscriber.is_active.is_(True))
            .filter(Subscriber.user_type != UserType.system_user)
            .scalar() or 0
        )
        # Count individuals (subscribers without organization)
        persons = (
            db.query(func.count(Subscriber.id))
            .filter(Subscriber.organization_id.is_(None))
            .filter(Subscriber.user_type != UserType.system_user)
            .scalar() or 0
        )
        # Count organizations (subscribers with organization)
        organizations = (
            db.query(func.count(Subscriber.id))
            .filter(Subscriber.organization_id.is_not(None))
            .filter(Subscriber.user_type != UserType.system_user)
            .scalar() or 0
        )
        return {
            "total": total,
            "active": active,
            "persons": persons,
            "organizations": organizations,
        }

    @staticmethod
    def count(
        db: Session,
        subscriber_type: str | None = None,
        organization_id: str | None = None,
    ) -> int:
        """Return total count of subscribers matching filters."""
        query = db.query(func.count(Subscriber.id)).filter(
            Subscriber.user_type != UserType.system_user
        )
        if organization_id:
            query = query.filter(
                Subscriber.organization_id == coerce_uuid(organization_id)
            )
        if subscriber_type:
            normalized = subscriber_type.strip().lower()
            if normalized == "person":
                query = query.filter(Subscriber.organization_id.is_(None))
            elif normalized == "organization":
                query = query.filter(Subscriber.organization_id.is_not(None))
        return query.scalar() or 0

    @staticmethod
    def get_dashboard_stats(db: Session) -> dict:
        """Build subscriber dashboard stats for admin overview.

        Returns:
            Dictionary with active_count, new_this_month, suspended_count,
            churn_rate, subscriber_status_chart, signup_trend, and
            recent_subscribers.
        """
        import calendar
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        all_subs = (
            db.query(Subscriber)
            .filter(Subscriber.user_type != UserType.system_user)
            .all()
        )
        total = len(all_subs)
        active_count = sum(1 for s in all_subs if s.is_active)
        inactive_count = total - active_count

        # New this month
        new_this_month = sum(
            1 for s in all_subs
            if s.created_at is not None and s.created_at >= month_start
        )

        # Suspended (subscribers with status=suspended on their account)
        from app.models.subscriber import SubscriberStatus as SubStatus

        suspended_count = 0
        canceled_count = 0
        for s in all_subs:
            acct_status = getattr(s, "status", None)
            if acct_status == SubStatus.suspended:
                suspended_count += 1
            elif acct_status == SubStatus.canceled:
                canceled_count += 1

        # Churn rate: canceled in last 30 days / active at start of period
        thirty_days_ago = now - timedelta(days=30)
        churned_recent = sum(
            1 for s in all_subs
            if (
                getattr(s, "status", None) == SubStatus.canceled
                and s.updated_at is not None
                and s.updated_at >= thirty_days_ago
            )
        )
        active_at_start = active_count + churned_recent
        churn_rate = (
            round(churned_recent / active_at_start * 100, 1)
            if active_at_start > 0
            else 0.0
        )

        # Status chart
        subscriber_status_chart = {
            "labels": ["Active", "Suspended", "Canceled", "Inactive"],
            "values": [active_count, suspended_count, canceled_count, inactive_count],
            "colors": ["#10b981", "#f59e0b", "#f43f5e", "#94a3b8"],
        }

        # Signup trend — last 12 months
        labels: list[str] = []
        values: list[int] = []
        for i in range(11, -1, -1):
            month = now.month - i
            year = now.year
            while month <= 0:
                month += 12
                year -= 1
            labels.append(calendar.month_abbr[month])
            count = sum(
                1 for s in all_subs
                if (
                    s.created_at is not None
                    and s.created_at.year == year
                    and s.created_at.month == month
                )
            )
            values.append(count)

        signup_trend = {"labels": labels, "values": values}

        # Recent subscribers
        recent = sorted(
            all_subs,
            key=lambda s: s.created_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )[:10]

        return {
            "active_count": active_count,
            "total_count": total,
            "new_this_month": new_this_month,
            "suspended_count": suspended_count,
            "churn_rate": churn_rate,
            "subscriber_status_chart": subscriber_status_chart,
            "signup_trend": signup_trend,
            "recent_subscribers": recent,
        }


class Accounts(ListResponseMixin):
    """Compatibility layer: accounts are now subscribers."""

    @staticmethod
    def create(db: Session, payload: SubscriberAccountCreate):
        if not payload.subscriber_id:
            raise HTTPException(status_code=400, detail="subscriber_id is required")
        subscriber = db.get(Subscriber, payload.subscriber_id)
        if not subscriber:
            raise HTTPException(status_code=404, detail="Subscriber not found")
        if not subscriber.account_number:
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
                subscriber.account_number = generated
                db.commit()
                db.refresh(subscriber)
        return subscriber

    @staticmethod
    def get(db: Session, account_id: str):
        subscriber = db.get(Subscriber, account_id)
        if not subscriber:
            raise HTTPException(status_code=404, detail="Subscriber not found")
        return subscriber

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
        query = db.query(Subscriber)
        if subscriber_id:
            query = query.filter(Subscriber.id == coerce_uuid(subscriber_id))
        if reseller_id:
            query = query.filter(Subscriber.reseller_id == coerce_uuid(reseller_id))
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
    def update(db: Session, account_id: str, payload: SubscriberAccountUpdate):
        subscriber = db.get(Subscriber, account_id)
        if not subscriber:
            raise HTTPException(status_code=404, detail="Subscriber not found")
        return subscriber

    @staticmethod
    def delete(db: Session, account_id: str):
        subscriber = db.get(Subscriber, account_id)
        if not subscriber:
            raise HTTPException(status_code=404, detail="Subscriber not found")
        db.delete(subscriber)
        db.commit()


class Addresses(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: AddressCreate):
        subscriber = db.get(Subscriber, payload.subscriber_id)
        if not subscriber:
            raise HTTPException(status_code=404, detail="Subscriber not found")
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
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(Address)
        if subscriber_id:
            query = query.filter(Address.subscriber_id == subscriber_id)
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
        if "tax_rate_id" in data and data["tax_rate_id"]:
            _validate_tax_rate(db, str(data["tax_rate_id"]))
        if data.get("is_primary"):
            subscriber_id = data.get("subscriber_id", address.subscriber_id)
            address_type = data.get("address_type", address.address_type)
            db.query(Address).filter(
                Address.subscriber_id == subscriber_id,
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
addresses = Addresses()
subscriber_custom_fields = SubscriberCustomFields()
