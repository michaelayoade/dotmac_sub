from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

from app.models.billing import Invoice, InvoiceLine, InvoiceStatus, TaxApplication
from app.services.common import apply_ordering, apply_pagination, coerce_uuid, validate_enum
from app.models.catalog import CatalogOffer, OfferVersion, Subscription, UsageAllowance
from app.models.domain_settings import SettingDomain
from app.models.subscriber import Subscriber
from app.models.usage import (
    QuotaBucket,
    RadiusAccountingSession,
    UsageCharge,
    UsageChargeStatus,
    UsageRatingRun,
    UsageRatingRunStatus,
    UsageRecord,
)
from app.services import settings_spec
from app.services.response import ListResponseMixin
from app.schemas.usage import (
    QuotaBucketCreate,
    QuotaBucketUpdate,
    RadiusAccountingSessionCreate,
    RadiusAccountingSessionUpdate,
    UsageChargePostRequest,
    UsageChargePostBatchRequest,
    UsageRecordCreate,
    UsageRecordUpdate,
    UsageRatingRunRequest,
    UsageRatingRunResponse,
)
from app.services.events import emit_event
from app.services.events.types import EventType


def _round_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _round_gb(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def _round_bucket_gb(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _period_bounds(payload: UsageRatingRunRequest) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    if payload.period_start and payload.period_end:
        return payload.period_start, payload.period_end
    start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    if now.month == 12:
        end = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)
    return payload.period_start or start, payload.period_end or end


def _resolve_allowance(subscription: Subscription) -> UsageAllowance | None:
    if subscription.offer_version and subscription.offer_version.usage_allowance_id:
        return subscription.offer_version.usage_allowance
    if subscription.offer and subscription.offer.usage_allowance_id:
        return subscription.offer.usage_allowance
    return None


def _period_bounds_for_record(recorded_at: datetime) -> tuple[datetime, datetime]:
    start = datetime(recorded_at.year, recorded_at.month, 1, tzinfo=timezone.utc)
    if recorded_at.month == 12:
        end = datetime(recorded_at.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(recorded_at.year, recorded_at.month + 1, 1, tzinfo=timezone.utc)
    return start, end


def _parse_warning_thresholds(value: str | None) -> list[Decimal]:
    if not value:
        return []
    thresholds: list[Decimal] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            threshold = Decimal(part)
        except Exception:
            continue
        if Decimal("0") < threshold < Decimal("1.5"):
            thresholds.append(threshold)
    return sorted(set(thresholds))


def _resolve_or_create_quota_bucket(
    db: Session, subscription: Subscription, recorded_at: datetime
) -> QuotaBucket:
    period_start, period_end = _period_bounds_for_record(recorded_at)
    bucket = (
        db.query(QuotaBucket)
        .filter(QuotaBucket.subscription_id == subscription.id)
        .filter(QuotaBucket.period_start == period_start)
        .filter(QuotaBucket.period_end == period_end)
        .first()
    )
    if bucket:
        return bucket
    allowance = _resolve_allowance(subscription)
    included_gb, _ = _prorate_allowance(allowance, subscription, period_start, period_end)
    bucket = QuotaBucket(
        subscription_id=subscription.id,
        period_start=period_start,
        period_end=period_end,
        included_gb=_round_bucket_gb(included_gb),
        used_gb=Decimal("0.00"),
        rollover_gb=Decimal("0.00"),
        overage_gb=Decimal("0.00"),
    )
    db.add(bucket)
    db.flush()
    return bucket


def _emit_usage_events(
    db: Session,
    subscription: Subscription,
    bucket: QuotaBucket,
    previous_used: Decimal,
    new_used: Decimal,
) -> None:
    warning_enabled = settings_spec.resolve_value(
        db, SettingDomain.usage, "usage_warning_enabled"
    )
    if warning_enabled is not None and str(warning_enabled).lower() in {"0", "false", "no", "off"}:
        return
    included = Decimal(str(bucket.included_gb or 0))
    if included <= 0:
        return
    thresholds = _parse_warning_thresholds(
        settings_spec.resolve_value(db, SettingDomain.usage, "usage_warning_thresholds")
    )
    if thresholds:
        previous_ratio = previous_used / included if included else Decimal("0")
        new_ratio = new_used / included if included else Decimal("0")
        for threshold in thresholds:
            if previous_ratio < threshold <= new_ratio:
                emit_event(
                    db,
                    EventType.usage_warning,
                    {
                        "subscription_id": str(subscription.id),
                        "account_id": str(subscription.subscriber_id),
                        "used_gb": str(_round_gb(new_used)),
                        "included_gb": str(_round_gb(included)),
                        "threshold": str(threshold),
                    },
                    subscription_id=subscription.id,
                    account_id=subscription.subscriber_id,
                )
    if previous_used < included <= new_used:
        emit_event(
            db,
            EventType.usage_exhausted,
            {
                "subscription_id": str(subscription.id),
                "account_id": str(subscription.subscriber_id),
                "used_gb": str(_round_gb(new_used)),
                "included_gb": str(_round_gb(included)),
            },
            subscription_id=subscription.id,
            account_id=subscription.subscriber_id,
        )


def _prorate_allowance(
    allowance: UsageAllowance | None,
    subscription: Subscription,
    period_start: datetime,
    period_end: datetime,
) -> tuple[Decimal, Decimal | None]:
    if not allowance:
        return Decimal("0.0000"), None
    included = Decimal(str(allowance.included_gb or 0))
    cap = Decimal(str(allowance.overage_cap_gb)) if allowance.overage_cap_gb else None
    if not subscription.start_at and not subscription.end_at:
        return included, cap
    active_start = max(subscription.start_at or period_start, period_start)
    active_end = min(subscription.end_at or period_end, period_end)
    period_seconds = max((period_end - period_start).total_seconds(), 1)
    active_seconds = max((active_end - active_start).total_seconds(), 0)
    ratio = Decimal(str(active_seconds / period_seconds))
    if ratio <= 0:
        return Decimal("0.0000"), Decimal("0.0000") if cap else None
    prorated_included = _round_gb(included * ratio)
    prorated_cap = _round_gb(cap * ratio) if cap is not None else None
    return prorated_included, prorated_cap


def _resolve_or_create_invoice(
    db: Session,
    account_id: str,
    period_start: datetime,
    period_end: datetime,
    currency: str,
) -> Invoice:
    invoice = (
        db.query(Invoice)
        .filter(Invoice.account_id == account_id)
        .filter(Invoice.billing_period_start == period_start)
        .filter(Invoice.billing_period_end == period_end)
        .filter(Invoice.is_active.is_(True))
        .first()
    )
    if invoice:
        if invoice.currency != currency:
            raise HTTPException(status_code=400, detail="Invoice currency mismatch")
        return invoice
    default_status = settings_spec.resolve_value(
        db, SettingDomain.billing, "default_invoice_status"
    )
    status_value = validate_enum(default_status, InvoiceStatus, "status") if default_status else InvoiceStatus.draft
    invoice = Invoice(
        account_id=account_id,
        status=status_value,
        currency=currency,
        subtotal=Decimal("0.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("0.00"),
        balance_due=Decimal("0.00"),
        billing_period_start=period_start,
        billing_period_end=period_end,
    )
    db.add(invoice)
    db.flush()
    return invoice

class QuotaBuckets(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: QuotaBucketCreate):
        bucket = QuotaBucket(**payload.model_dump())
        db.add(bucket)
        db.commit()
        db.refresh(bucket)
        return bucket

    @staticmethod
    def get(db: Session, bucket_id: str):
        bucket = db.get(QuotaBucket, bucket_id)
        if not bucket:
            raise HTTPException(status_code=404, detail="Quota bucket not found")
        return bucket

    @staticmethod
    def list(
        db: Session,
        subscription_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(QuotaBucket)
        if subscription_id:
            query = query.filter(QuotaBucket.subscription_id == subscription_id)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": QuotaBucket.created_at, "period_start": QuotaBucket.period_start},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, bucket_id: str, payload: QuotaBucketUpdate):
        bucket = db.get(QuotaBucket, bucket_id)
        if not bucket:
            raise HTTPException(status_code=404, detail="Quota bucket not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(bucket, key, value)
        db.commit()
        db.refresh(bucket)
        return bucket

    @staticmethod
    def delete(db: Session, bucket_id: str):
        bucket = db.get(QuotaBucket, bucket_id)
        if not bucket:
            raise HTTPException(status_code=404, detail="Quota bucket not found")
        db.delete(bucket)
        db.commit()


class RadiusAccountingSessions(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: RadiusAccountingSessionCreate):
        session = RadiusAccountingSession(**payload.model_dump())
        db.add(session)
        db.commit()
        db.refresh(session)
        return session

    @staticmethod
    def get(db: Session, session_id: str):
        session = db.get(RadiusAccountingSession, session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Accounting session not found")
        return session

    @staticmethod
    def list(
        db: Session,
        subscription_id: str | None,
        access_credential_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(RadiusAccountingSession)
        if subscription_id:
            query = query.filter(
                RadiusAccountingSession.subscription_id == subscription_id
            )
        if access_credential_id:
            query = query.filter(
                RadiusAccountingSession.access_credential_id == access_credential_id
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": RadiusAccountingSession.created_at,
                "session_start": RadiusAccountingSession.session_start,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, session_id: str, payload: RadiusAccountingSessionUpdate):
        session = db.get(RadiusAccountingSession, session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Accounting session not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(session, key, value)
        db.commit()
        db.refresh(session)
        return session

    @staticmethod
    def delete(db: Session, session_id: str):
        session = db.get(RadiusAccountingSession, session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Accounting session not found")
        db.delete(session)
        db.commit()


class UsageRecords(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: UsageRecordCreate):
        record = UsageRecord(**payload.model_dump())
        db.add(record)
        subscription = db.get(Subscription, record.subscription_id)
        bucket = None
        previous_used = Decimal("0.00")
        new_used = Decimal("0.00")
        if subscription:
            if record.quota_bucket_id:
                bucket = db.get(QuotaBucket, record.quota_bucket_id)
            if not bucket:
                bucket = _resolve_or_create_quota_bucket(
                    db, subscription, record.recorded_at
                )
                record.quota_bucket_id = bucket.id
            previous_used = Decimal(str(bucket.used_gb or 0))
            increment = Decimal(str(record.total_gb or 0))
            new_used = previous_used + increment
            bucket.used_gb = _round_bucket_gb(new_used)
            bucket.overage_gb = _round_bucket_gb(
                max(new_used - Decimal(str(bucket.included_gb or 0)), Decimal("0"))
            )
        db.commit()
        db.refresh(record)
        if subscription and bucket:
            _emit_usage_events(db, subscription, bucket, previous_used, new_used)
        return record

    @staticmethod
    def get(db: Session, record_id: str):
        record = db.get(UsageRecord, record_id)
        if not record:
            raise HTTPException(status_code=404, detail="Usage record not found")
        return record

    @staticmethod
    def list(
        db: Session,
        subscription_id: str | None,
        quota_bucket_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(UsageRecord)
        if subscription_id:
            query = query.filter(UsageRecord.subscription_id == subscription_id)
        if quota_bucket_id:
            query = query.filter(UsageRecord.quota_bucket_id == quota_bucket_id)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": UsageRecord.created_at, "recorded_at": UsageRecord.recorded_at},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, record_id: str, payload: UsageRecordUpdate):
        record = db.get(UsageRecord, record_id)
        if not record:
            raise HTTPException(status_code=404, detail="Usage record not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(record, key, value)
        db.commit()
        db.refresh(record)
        return record

    @staticmethod
    def delete(db: Session, record_id: str):
        record = db.get(UsageRecord, record_id)
        if not record:
            raise HTTPException(status_code=404, detail="Usage record not found")
        db.delete(record)
        db.commit()


class UsageCharges(ListResponseMixin):
    @staticmethod
    def get(db: Session, charge_id: str):
        charge = db.get(UsageCharge, charge_id)
        if not charge:
            raise HTTPException(status_code=404, detail="Usage charge not found")
        return charge

    @staticmethod
    def list(
        db: Session,
        subscription_id: str | None,
        subscriber_id: str | None,
        status: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(UsageCharge)
        if subscription_id:
            query = query.filter(UsageCharge.subscription_id == subscription_id)
        if subscriber_id:
            query = query.filter(UsageCharge.subscriber_id == subscriber_id)
        if status:
            query = query.filter(
                UsageCharge.status
                == validate_enum(status, UsageChargeStatus, "status")
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": UsageCharge.created_at, "period_start": UsageCharge.period_start},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def post(
        db: Session,
        charge_id: str,
        payload: UsageChargePostRequest,
        commit: bool = True,
    ):
        charge = db.get(UsageCharge, charge_id)
        if not charge:
            raise HTTPException(status_code=404, detail="Usage charge not found")
        if charge.status == UsageChargeStatus.posted:
            return charge
        if charge.status == UsageChargeStatus.needs_review:
            raise HTTPException(status_code=400, detail="Charge requires review")
        subscriber = db.get(Subscriber, charge.subscriber_id)
        if not subscriber:
            raise HTTPException(status_code=404, detail="Subscriber not found")
        if payload.invoice_id:
            invoice = db.get(Invoice, payload.invoice_id)
            if not invoice:
                raise HTTPException(status_code=404, detail="Invoice not found")
            if str(invoice.account_id) != str(charge.subscriber_id):
                raise HTTPException(status_code=400, detail="Invoice not for account")
        else:
            invoice = _resolve_or_create_invoice(
                db,
                str(charge.subscriber_id),
                charge.period_start,
                charge.period_end,
                charge.currency,
            )
        line = InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=charge.subscription_id,
            description="Usage overage",
            quantity=Decimal("1.000"),
            unit_price=_round_money(charge.amount),
            amount=_round_money(charge.amount),
            tax_application=TaxApplication.exclusive,
        )
        db.add(line)
        db.flush()
        charge.invoice_line_id = line.id
        charge.status = UsageChargeStatus.posted
        db.flush()
        from app.services import billing as billing_service

        billing_service._recalculate_invoice_totals(db, invoice)
        if commit:
            db.commit()
            db.refresh(charge)
        return charge

    @staticmethod
    def post_batch(db: Session, payload: UsageChargePostBatchRequest) -> int:
        query = (
            db.query(UsageCharge)
            .filter(UsageCharge.period_start == payload.period_start)
            .filter(UsageCharge.period_end == payload.period_end)
            .filter(UsageCharge.status == UsageChargeStatus.staged)
        )
        if payload.account_id:
            query = query.filter(UsageCharge.subscriber_id == payload.account_id)
        charges = query.all()
        posted = 0
        for charge in charges:
            UsageCharges.post(
                db,
                str(charge.id),
                UsageChargePostRequest(),
                commit=False,
            )
            posted += 1
        if posted:
            db.commit()
        return posted


class UsageRatingRuns(ListResponseMixin):
    @staticmethod
    def get(db: Session, run_id: str):
        run = db.get(UsageRatingRun, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Usage rating run not found")
        return run

    @staticmethod
    def list(
        db: Session,
        status: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(UsageRatingRun)
        if status:
            query = query.filter(
                UsageRatingRun.status
                == validate_enum(status, UsageRatingRunStatus, "status")
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": UsageRatingRun.created_at, "run_at": UsageRatingRun.run_at},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def run(db: Session, payload: UsageRatingRunRequest) -> UsageRatingRunResponse:
        period_start, period_end = _period_bounds(payload)
        run_at = datetime.now(timezone.utc)
        default_run_status = settings_spec.resolve_value(
            db, SettingDomain.usage, "default_rating_run_status"
        )
        run_status = (
            validate_enum(default_run_status, UsageRatingRunStatus, "status")
            if default_run_status
            else UsageRatingRunStatus.running
        )
        run = UsageRatingRun(
            run_at=run_at,
            period_start=period_start,
            period_end=period_end,
            status=run_status,
        )
        if not payload.dry_run:
            db.add(run)
            db.flush()
        try:
            query = db.query(Subscription).options(
                selectinload(Subscription.offer).selectinload(
                    CatalogOffer.usage_allowance
                ),
                selectinload(Subscription.offer_version).selectinload(
                    OfferVersion.usage_allowance
                ),
            )
            if payload.subscription_id:
                query = query.filter(Subscription.id == payload.subscription_id)
            subscriptions = query.all()
            charges_created = 0
            skipped = 0
            for subscription in subscriptions:
                existing = (
                    db.query(UsageCharge)
                    .filter(UsageCharge.subscription_id == subscription.id)
                    .filter(UsageCharge.period_start == period_start)
                    .filter(UsageCharge.period_end == period_end)
                    .first()
                )
                if existing:
                    skipped += 1
                    continue
                total_gb = (
                    db.query(func.coalesce(func.sum(UsageRecord.total_gb), 0))
                    .filter(UsageRecord.subscription_id == subscription.id)
                    .filter(UsageRecord.recorded_at >= period_start)
                    .filter(UsageRecord.recorded_at < period_end)
                    .scalar()
                )
                total_gb = _round_gb(Decimal(str(total_gb)))
                allowance = _resolve_allowance(subscription)
                included_gb, cap_gb = _prorate_allowance(
                    allowance, subscription, period_start, period_end
                )
                included_gb = _round_gb(included_gb)
                billable_gb = total_gb - included_gb
                if billable_gb < 0:
                    billable_gb = Decimal("0.0000")
                if cap_gb is not None and billable_gb > cap_gb:
                    billable_gb = cap_gb
                rate = Decimal("0.0000")
                default_currency = settings_spec.resolve_value(
                    db, SettingDomain.billing, "default_currency"
                )
                currency = default_currency or "NGN"
                default_status = settings_spec.resolve_value(
                    db, SettingDomain.usage, "default_charge_status"
                )
                status = (
                    validate_enum(default_status, UsageChargeStatus, "status")
                    if default_status
                    else UsageChargeStatus.staged
                )
                notes = None
                if allowance and allowance.overage_rate is not None:
                    rate = Decimal(str(allowance.overage_rate))
                else:
                    status = UsageChargeStatus.needs_review
                    notes = "Missing usage allowance or overage rate"
                amount = _round_money(billable_gb * rate)
                if billable_gb == 0:
                    amount = Decimal("0.00")
                    status = UsageChargeStatus.skipped
                if not subscription.subscriber_id:
                    status = UsageChargeStatus.needs_review
                    notes = "Subscription missing account"
                charge = UsageCharge(
                    subscription_id=subscription.id,
                    subscriber_id=subscription.subscriber_id,
                    period_start=period_start,
                    period_end=period_end,
                    total_gb=total_gb,
                    included_gb=included_gb,
                    billable_gb=_round_gb(billable_gb),
                    unit_price=rate,
                    amount=amount,
                    currency=currency,
                    status=status,
                    notes=notes,
                    rated_at=run_at,
                )
                if not payload.dry_run:
                    db.add(charge)
                charges_created += 1
            if not payload.dry_run:
                run.subscriptions_scanned = len(subscriptions)
                run.charges_created = charges_created
                run.skipped = skipped
                run.status = UsageRatingRunStatus.success
                db.commit()
            return UsageRatingRunResponse(
                run_id=run.id if not payload.dry_run else None,
                run_at=run_at,
                period_start=period_start,
                period_end=period_end,
                subscriptions_scanned=len(subscriptions),
                charges_created=charges_created,
                skipped=skipped,
            )
        except Exception as exc:
            if not payload.dry_run:
                run.status = UsageRatingRunStatus.failed
                run.error = str(exc)
                db.commit()
            raise


quota_buckets = QuotaBuckets()
radius_accounting_sessions = RadiusAccountingSessions()
usage_records = UsageRecords()
usage_charges = UsageCharges()
usage_rating_runs = UsageRatingRuns()
