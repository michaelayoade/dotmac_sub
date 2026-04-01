import builtins
import logging
from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, case, func, not_, or_
from sqlalchemy.orm import Session, selectinload

from app.models.billing import TaxRate
from app.models.domain_settings import SettingDomain
from app.models.subscriber import (
    Address,
    AddressType,
    Reseller,
    Subscriber,
    SubscriberCategory,
    SubscriberCustomField,
    SubscriberStatus,
    UserType,
)
from app.schemas.subscriber import (
    AddressCreate,
    AddressUpdate,
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

logger = logging.getLogger(__name__)

_validate_enum = validate_enum
_RESTRICTED_STATUSES = {
    SubscriberStatus.blocked,
    SubscriberStatus.suspended,
    SubscriberStatus.disabled,
}


def _subscriber_category_clause():
    return func.lower(
        func.coalesce(Subscriber.metadata_["subscriber_category"].as_string(), "")
    )


def _is_business_clause():
    return _subscriber_category_clause() == SubscriberCategory.business.value


def _metadata_flag(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, int):
        return value == 1
    return False


def is_splynx_deleted_import(subscriber: Subscriber) -> bool:
    """Return whether a subscriber represents a Splynx soft-deleted import."""
    metadata = subscriber.metadata_ or {}
    if _metadata_flag(metadata.get("splynx_deleted")):
        return True
    if not getattr(subscriber, "splynx_customer_id", None):
        return False
    if subscriber.is_active:
        return False
    if subscriber.status != SubscriberStatus.canceled:
        return False
    raw_status = str(metadata.get("splynx_status") or "").strip().lower()
    return raw_status not in {"", "deleted", "canceled"}


def _metadata_text_clause(key: str):
    return func.lower(
        func.trim(func.coalesce(Subscriber.metadata_[key].as_string(), ""))
    )


def splynx_deleted_import_clause():
    """Return a SQL clause matching Splynx soft-deleted imported subscribers."""
    splynx_deleted = _metadata_text_clause("splynx_deleted")
    splynx_status = _metadata_text_clause("splynx_status")
    return or_(
        splynx_deleted.in_(("1", "true", "yes", "on")),
        and_(
            Subscriber.splynx_customer_id.is_not(None),
            Subscriber.is_active.is_(False),
            Subscriber.status == SubscriberStatus.canceled,
            not_(splynx_status.in_(("", "deleted", "canceled"))),
        ),
    )


def visible_subscriber_clause():
    """Return a SQL clause for subscribers that should appear in admin stats."""
    return and_(
        Subscriber.user_type != UserType.system_user,
        not_(splynx_deleted_import_clause()),
    )


def _coerce_utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _metadata_datetime(metadata: dict | None, key: str) -> datetime | None:
    if not metadata:
        return None
    value = metadata.get(key)
    if isinstance(value, datetime):
        return _coerce_utc_datetime(value)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        logger.debug("Could not parse metadata datetime key=%s value=%r", key, value)
        return None
    return _coerce_utc_datetime(parsed)


def _update_restricted_status_metadata(
    subscriber: Subscriber,
    *,
    previous_status: SubscriberStatus | None,
    next_status: SubscriberStatus | None,
) -> None:
    metadata = dict(subscriber.metadata_ or {})
    was_restricted = previous_status in _RESTRICTED_STATUSES
    is_restricted = next_status in _RESTRICTED_STATUSES
    now = datetime.now(UTC)

    if is_restricted:
        if not was_restricted or not _metadata_datetime(metadata, "restricted_since"):
            metadata["restricted_since"] = now.isoformat()
        metadata["restricted_status"] = next_status.value if next_status else None
    elif was_restricted:
        metadata["last_restricted_status"] = (
            previous_status.value if previous_status else None
        )
        metadata["last_restricted_ended_at"] = now.isoformat()

    subscriber.metadata_ = metadata


def get_effective_created_at(subscriber: Subscriber) -> datetime | None:
    metadata = subscriber.metadata_ or {}
    source_created = _metadata_datetime(metadata, "splynx_date_add")
    if source_created is not None:
        return source_created
    if (
        getattr(subscriber, "splynx_customer_id", None)
        and subscriber.account_start_date
    ):
        return _coerce_utc_datetime(subscriber.account_start_date)
    return _coerce_utc_datetime(subscriber.created_at)


def get_effective_updated_at(subscriber: Subscriber) -> datetime | None:
    metadata = subscriber.metadata_ or {}
    source_updated = _metadata_datetime(metadata, "splynx_last_update")
    if source_updated is not None:
        return source_updated
    return _coerce_utc_datetime(subscriber.updated_at)


def _validate_tax_rate(db: Session, tax_rate_id: str | None):
    if not tax_rate_id:
        return None
    rate = db.get(TaxRate, tax_rate_id)
    if not rate:
        raise HTTPException(status_code=404, detail="Tax rate not found")
    return rate


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
    def count(db: Session, is_active: bool | None) -> int:
        query = db.query(func.count(Reseller.id))
        if is_active is None:
            query = query.filter(Reseller.is_active.is_(True))
        else:
            query = query.filter(Reseller.is_active == is_active)
        return query.scalar() or 0

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


def _apply_billing_defaults(db: Session, subscriber: Subscriber) -> None:
    """Auto-populate billing_day, payment_due_days, grace_period_days,
    min_balance from global settings based on subscriber's billing_mode."""
    from decimal import Decimal

    mode = subscriber.billing_mode.value  # "prepaid" or "postpaid"
    prefix = f"{mode}_default"

    if subscriber.billing_day is None:
        val = settings_spec.resolve_value(
            db, SettingDomain.billing, f"{prefix}_billing_day"
        )
        if val is not None:
            billing_day = int(str(val))
            # 0 means "day of activation" — use today's day
            subscriber.billing_day = (
                billing_day if billing_day > 0 else datetime.now(UTC).day
            )

    if subscriber.payment_due_days is None:
        val = settings_spec.resolve_value(
            db, SettingDomain.billing, f"{prefix}_payment_due_days"
        )
        if val is not None:
            subscriber.payment_due_days = int(str(val))

    if subscriber.grace_period_days is None:
        val = settings_spec.resolve_value(
            db, SettingDomain.billing, f"{prefix}_grace_period_days"
        )
        if val is not None:
            subscriber.grace_period_days = int(str(val))

    if subscriber.min_balance is None:
        val = settings_spec.resolve_value(
            db, SettingDomain.billing, f"{prefix}_min_balance"
        )
        if val is not None:
            subscriber.min_balance = Decimal(str(val))


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
                    category
                    if isinstance(category, SubscriberCategory)
                    else str(category)
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
            _apply_billing_defaults(db, subscriber)
            db.commit()
            db.refresh(subscriber)
            return subscriber

        data = payload.model_dump()
        category = data.pop("category", None)
        data.pop("organization_id", None)
        if data.get("user_type") is None:
            data["user_type"] = UserType.customer
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
        _apply_billing_defaults(db, subscriber)
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
        person_id: str | None = None,
        business_account_id: str | None = None,
        subscriber_type: str | None = None,
        order_by: str = "created_at",
        order_dir: str = "desc",
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
        search: str | None = None,
        include_deleted: bool = False,
    ):
        query = db.query(Subscriber).options(
            selectinload(Subscriber.addresses),
        )
        query = query.filter(Subscriber.user_type != UserType.system_user)
        if not include_deleted:
            query = query.filter(not_(splynx_deleted_import_clause()))
        # Status filter
        if status:
            normalized_status = status.strip().lower()
            if normalized_status == "inactive":
                query = query.filter(Subscriber.is_active.is_(False))
            elif normalized_status in (
                "active",
                "blocked",
                "suspended",
                "disabled",
                "canceled",
                "new",
                "delinquent",
            ):
                query = query.filter(Subscriber.status == normalized_status)
        # Full-text search across subscriber + related tables
        if search:
            term = search.strip()
            if term:
                from app.models.catalog import AccessCredential, NasDevice, Subscription
                from app.models.network import (
                    FdhCabinet,
                    OntAssignment,
                    OntUnit,
                    Splitter,
                )
                from app.models.network_monitoring import PopSite

                like = f"%{term}%"
                # Direct subscriber fields
                direct_conditions = or_(
                    Subscriber.first_name.ilike(like),
                    Subscriber.last_name.ilike(like),
                    Subscriber.display_name.ilike(like),
                    Subscriber.email.ilike(like),
                    Subscriber.phone.ilike(like),
                    Subscriber.subscriber_number.ilike(like),
                    Subscriber.account_number.ilike(like),
                    Subscriber.address_line1.ilike(like),
                    Subscriber.city.ilike(like),
                    Subscriber.notes.ilike(like),
                )
                # Subscription fields (IP, login, MAC)
                sub_match = (
                    db.query(Subscription.subscriber_id)
                    .filter(
                        or_(
                            Subscription.login.ilike(like),
                            Subscription.ipv4_address.ilike(like),
                            Subscription.ipv6_address.ilike(like),
                            Subscription.mac_address.ilike(like),
                        )
                    )
                    .correlate(Subscriber)
                    .exists()
                )
                # PPPoE/RADIUS username
                cred_match = (
                    db.query(AccessCredential.subscriber_id)
                    .filter(
                        AccessCredential.subscriber_id == Subscriber.id,
                        AccessCredential.username.ilike(like),
                    )
                    .correlate(Subscriber)
                    .exists()
                )
                # ONT serial number
                ont_match = (
                    db.query(OntAssignment.id)
                    .join(OntUnit, OntUnit.id == OntAssignment.ont_unit_id)
                    .filter(
                        OntAssignment.subscriber_id == Subscriber.id,
                        OntUnit.serial_number.ilike(like),
                    )
                    .correlate(Subscriber)
                    .exists()
                )
                # Provisioning access point / NAS identity
                nas_match = Subscriber.subscriptions.any(
                    Subscription.provisioning_nas_device.has(
                        or_(
                            NasDevice.name.ilike(like),
                            NasDevice.code.ilike(like),
                        )
                    )
                )
                # POP site serving the subscription via its NAS/access point
                pop_site_match = Subscriber.subscriptions.any(
                    Subscription.provisioning_nas_device.has(
                        NasDevice.pop_site.has(
                            or_(
                                PopSite.name.ilike(like),
                                PopSite.code.ilike(like),
                            )
                        )
                    )
                )
                # Fiber cabinet reached through ONT -> splitter -> FDH cabinet
                cabinet_match = Subscriber.ont_assignments.any(
                    OntAssignment.ont_unit.has(
                        OntUnit.splitter.has(
                            Splitter.fdh.has(
                                or_(
                                    FdhCabinet.name.ilike(like),
                                    FdhCabinet.code.ilike(like),
                                )
                            )
                        ),
                    )
                )
                query = query.filter(
                    or_(
                        direct_conditions,
                        Subscriber.id.in_(
                            db.query(Subscription.subscriber_id).filter(
                                or_(
                                    Subscription.login.ilike(like),
                                    Subscription.ipv4_address.ilike(like),
                                    Subscription.ipv6_address.ilike(like),
                                    Subscription.mac_address.ilike(like),
                                )
                            )
                        ),
                        cred_match,
                        ont_match,
                        nas_match,
                        pop_site_match,
                        cabinet_match,
                    )
                )
        # Backwards-compat: allow filtering by legacy "person_id" keyword.
        if person_id:
            query = query.filter(Subscriber.id == coerce_uuid(person_id))
        if business_account_id:
            query = query.filter(Subscriber.id == coerce_uuid(business_account_id))
            query = query.filter(_is_business_clause())
        if subscriber_type:
            normalized = subscriber_type.strip().lower()
            if normalized == "person":
                query = query.filter(not_(_is_business_clause()))
            elif normalized == "business":
                query = query.filter(_is_business_clause())
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
        previous_status = subscriber.status
        data = payload.model_dump(exclude_unset=True)
        category = data.pop("category", None)
        data.pop("organization_id", None)
        for key, value in data.items():
            setattr(subscriber, key, value)
        if category is not None:
            subscriber.category = (
                category if isinstance(category, SubscriberCategory) else str(category)
            )
        _update_restricted_status_metadata(
            subscriber,
            previous_status=previous_status,
            next_status=subscriber.status,
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
        total, active, persons, organizations = db.execute(
            db.query(
                func.count(Subscriber.id),
                func.coalesce(
                    func.sum(case((Subscriber.is_active.is_(True), 1), else_=0)),
                    0,
                ),
                func.coalesce(
                    func.sum(case((not_(_is_business_clause()), 1), else_=0)),
                    0,
                ),
                func.coalesce(
                    func.sum(case((_is_business_clause(), 1), else_=0)),
                    0,
                ),
            )
            .filter(visible_subscriber_clause())
            .statement
        ).one()
        return {
            "total": int(total or 0),
            "active": int(active or 0),
            "persons": int(persons or 0),
            "organizations": int(organizations or 0),
        }

    @staticmethod
    def count(
        db: Session,
        subscriber_type: str | None = None,
        business_account_id: str | None = None,
        status: str | None = None,
        search: str | None = None,
        include_deleted: bool = False,
    ) -> int:
        """Return total count of subscribers matching filters."""
        query = db.query(func.count(Subscriber.id)).filter(
            Subscriber.user_type != UserType.system_user
        )
        if not include_deleted:
            query = query.filter(not_(splynx_deleted_import_clause()))
        if status:
            normalized_status = status.strip().lower()
            if normalized_status == "inactive":
                query = query.filter(Subscriber.is_active.is_(False))
            elif normalized_status in (
                "active",
                "blocked",
                "suspended",
                "disabled",
                "canceled",
                "new",
                "delinquent",
            ):
                query = query.filter(Subscriber.status == normalized_status)
        if search:
            term = search.strip()
            if term:
                from app.models.catalog import AccessCredential, NasDevice, Subscription
                from app.models.network import (
                    FdhCabinet,
                    OntAssignment,
                    OntUnit,
                    Splitter,
                )
                from app.models.network_monitoring import PopSite

                like = f"%{term}%"
                query = query.filter(
                    or_(
                        Subscriber.first_name.ilike(like),
                        Subscriber.last_name.ilike(like),
                        Subscriber.display_name.ilike(like),
                        Subscriber.email.ilike(like),
                        Subscriber.phone.ilike(like),
                        Subscriber.subscriber_number.ilike(like),
                        Subscriber.account_number.ilike(like),
                        Subscriber.address_line1.ilike(like),
                        Subscriber.city.ilike(like),
                        Subscriber.notes.ilike(like),
                        Subscriber.id.in_(
                            db.query(Subscription.subscriber_id).filter(
                                or_(
                                    Subscription.login.ilike(like),
                                    Subscription.ipv4_address.ilike(like),
                                    Subscription.ipv6_address.ilike(like),
                                    Subscription.mac_address.ilike(like),
                                )
                            )
                        ),
                        Subscriber.id.in_(
                            db.query(AccessCredential.subscriber_id).filter(
                                AccessCredential.username.ilike(like),
                            )
                        ),
                        Subscriber.id.in_(
                            db.query(OntAssignment.subscriber_id)
                            .join(
                                OntUnit,
                                OntUnit.id == OntAssignment.ont_unit_id,
                            )
                            .filter(OntUnit.serial_number.ilike(like))
                        ),
                        Subscriber.subscriptions.any(
                            Subscription.provisioning_nas_device.has(
                                or_(
                                    NasDevice.name.ilike(like),
                                    NasDevice.code.ilike(like),
                                )
                            )
                        ),
                        Subscriber.subscriptions.any(
                            Subscription.provisioning_nas_device.has(
                                NasDevice.pop_site.has(
                                    or_(
                                        PopSite.name.ilike(like),
                                        PopSite.code.ilike(like),
                                    )
                                )
                            )
                        ),
                        Subscriber.ont_assignments.any(
                            OntAssignment.ont_unit.has(
                                OntUnit.splitter.has(
                                    Splitter.fdh.has(
                                        or_(
                                            FdhCabinet.name.ilike(like),
                                            FdhCabinet.code.ilike(like),
                                        )
                                    )
                                ),
                            )
                        ),
                    )
                )
        if business_account_id:
            query = query.filter(Subscriber.id == coerce_uuid(business_account_id))
            query = query.filter(_is_business_clause())
        if subscriber_type:
            normalized = subscriber_type.strip().lower()
            if normalized == "person":
                query = query.filter(not_(_is_business_clause()))
            elif normalized == "business":
                query = query.filter(_is_business_clause())
            else:
                raise HTTPException(status_code=400, detail="Invalid subscriber_type")
        return query.scalar() or 0

    @staticmethod
    def get_dashboard_stats(db: Session) -> dict:
        """Build subscriber dashboard stats for admin overview.

        Returns:
            Dictionary with active_count, new_this_month, suspended_count,
            churn_rate, subscriber_status_chart, signup_trend, and
            recent_subscribers.

        All aggregations use SQL-level queries for performance.
        """
        import calendar
        from datetime import timedelta

        from sqlalchemy import extract

        now = datetime.now(UTC)
        thirty_days_ago = now - timedelta(days=30)

        # Base filter for visible subscribers (excludes system_user and splynx deleted)
        visible_filter = visible_subscriber_clause()

        # SQL expression for effective created_at:
        # COALESCE(metadata->>'splynx_date_add', account_start_date, created_at)
        effective_created_at = func.coalesce(
            func.cast(
                Subscriber.metadata_["splynx_date_add"].as_string(),
                Subscriber.created_at.type,
            ),
            Subscriber.account_start_date,
            Subscriber.created_at,
        )

        # SQL expression for effective updated_at:
        # COALESCE(metadata->>'splynx_last_update', updated_at)
        effective_updated_at = func.coalesce(
            func.cast(
                Subscriber.metadata_["splynx_last_update"].as_string(),
                Subscriber.updated_at.type,
            ),
            Subscriber.updated_at,
        )

        # Total and active counts in single query
        counts = db.query(
            func.count(Subscriber.id).label("total"),
            func.count(Subscriber.id).filter(Subscriber.is_active.is_(True)).label(
                "active"
            ),
            func.count(Subscriber.id)
            .filter(Subscriber.status == SubscriberStatus.suspended)
            .label("suspended"),
            func.count(Subscriber.id)
            .filter(Subscriber.status == SubscriberStatus.canceled)
            .label("canceled"),
            func.count(Subscriber.id)
            .filter(effective_created_at >= thirty_days_ago)
            .label("new_this_month"),
            func.count(Subscriber.id)
            .filter(
                and_(
                    Subscriber.status == SubscriberStatus.canceled,
                    effective_updated_at >= thirty_days_ago,
                )
            )
            .label("churned_recent"),
        ).filter(visible_filter).one()

        total = counts.total or 0
        active_count = counts.active or 0
        suspended_count = counts.suspended or 0
        canceled_count = counts.canceled or 0
        new_this_month = counts.new_this_month or 0
        churned_recent = counts.churned_recent or 0
        inactive_count = total - active_count

        # Churn rate calculation
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

        # Signup trend - last 12 months using SQL GROUP BY
        twelve_months_ago = (now - timedelta(days=365)).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )

        monthly_counts = (
            db.query(
                extract("year", effective_created_at).label("year"),
                extract("month", effective_created_at).label("month"),
                func.count(Subscriber.id).label("count"),
            )
            .filter(visible_filter)
            .filter(effective_created_at >= twelve_months_ago)
            .group_by(
                extract("year", effective_created_at),
                extract("month", effective_created_at),
            )
            .all()
        )

        # Build lookup dict for monthly counts
        # r is (year, month, count) tuple; use index to avoid mypy confusion with row.count method
        monthly_lookup: dict[tuple[int, int], int] = {
            (int(r[0]), int(r[1])): int(r[2]) for r in monthly_counts
        }

        labels: list[str] = []
        values: list[int] = []
        for i in range(11, -1, -1):
            month = now.month - i
            year = now.year
            while month <= 0:
                month += 12
                year -= 1
            labels.append(calendar.month_abbr[month])
            values.append(monthly_lookup.get((year, month), 0))

        signup_trend = {"labels": labels, "values": values}

        # Recent subscribers - query only 10 with ORDER BY
        recent = (
            db.query(Subscriber)
            .filter(visible_filter)
            .order_by(effective_created_at.desc())
            .limit(10)
            .all()
        )

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
            raise HTTPException(
                status_code=404, detail="Subscriber custom field not found"
            )
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
            raise HTTPException(
                status_code=404, detail="Subscriber custom field not found"
            )
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
            raise HTTPException(
                status_code=404, detail="Subscriber custom field not found"
            )
        custom_field.is_active = False
        db.commit()


resellers = Resellers()
subscribers = Subscribers()
accounts = Accounts()
addresses = Addresses()
subscriber_custom_fields = SubscriberCustomFields()
