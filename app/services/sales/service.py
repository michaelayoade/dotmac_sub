"""Leads / pipeline / quotes services — CRM port (Phase 3 §2.1).

Faithful port of ``dotmac_crm/app/services/crm/sales/service.py`` onto sub's
native models (``app/models/sales.py``), with the Phase 3 deltas applied:

* Customer party: CRM ``person_id`` (people) becomes ``subscriber_id``
  (sub ``subscribers``); party-status upgrades write the Phase 3
  ``subscribers.party_status`` lifecycle column (§1.3/§1.8).
* Staff references (``quotes.owner_person_id``) are plain UUIDs — no FK and
  no existence check; display resolves via the Phase 1 staff map.
* Phase 4 stubs (risk #8): owner-agent auto-assignment from the CRM inbox
  (ConversationAssignment / last agent-authored message) and lead-source
  inference from messages / person channels degrade to None. Attribution
  metadata inference is kept — it never touched inbox models.
* ``lead_source`` vocabulary gains ``Portal`` (+ ``portal`` alias): the fix
  for the live self-serve quote-request 400 (§2.1 / risk #7 — CRM's
  ``PortalQuotes.request`` passes ``lead_source="portal"`` which the old
  vocabulary rejected).
* Statuses are stored as plain strings (sub convention: String column +
  app-level enum); helpers normalise enum members to their values.
* ``quote_line_items.inventory_item_id`` is carried verbatim without an
  existence check — inventory is Phase 5 (§1.4).
* Install-project creation from accepted quotes is deferred to the projects
  service port (next in the Phase 3 series) — see
  ``_ensure_project_from_quote``.
* Native services emit sub events from day one (risk #13):
  ``lead.created`` / ``quote.accepted``.
"""

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import String, cast, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.models.domain_settings import SettingDomain
from app.models.sales import (
    Lead,
    LeadStatus,
    Pipeline,
    PipelineStage,
    Quote,
    QuoteLineItem,
    QuoteStatus,
)
from app.models.subscriber import PartyStatus, Subscriber
from app.services import settings_spec
from app.services.common import (
    apply_ordering,
    apply_pagination,
    coerce_uuid,
    round_money,
    validate_enum,
)
from app.services.events import EventType, emit_event
from app.services.response import ListResponseMixin

_logger = logging.getLogger(__name__)

# Normalized lead-source vocabulary. ``Portal`` is the Phase 3 addition — the
# self-serve (map-pin) quote request tags its leads with it (§1.3, risk #7).
LEAD_SOURCE_OPTIONS = (
    "Facebook",
    "Instagram",
    "Whatsapp",
    "Email",
    "Referrer",
    "Instagram Ads",
    "Facebook Ads",
    "Google",
    "Website",
    "Portal",
)

_LEAD_SOURCE_NORMALIZED_MAP = {
    "facebook": "Facebook",
    "facebook messenger": "Facebook",
    "facebook_messenger": "Facebook",
    "instagram": "Instagram",
    "instagram dm": "Instagram",
    "instagram_dm": "Instagram",
    "whatsapp": "Whatsapp",
    "wa": "Whatsapp",
    "email": "Email",
    "referrer": "Referrer",
    "referral": "Referrer",
    "instagram ads": "Instagram Ads",
    "instagram ad": "Instagram Ads",
    "ig ads": "Instagram Ads",
    "ig ad": "Instagram Ads",
    "facebook ads": "Facebook Ads",
    "facebook ad": "Facebook Ads",
    "fb ads": "Facebook Ads",
    "fb ad": "Facebook Ads",
    "meta ads": "Facebook Ads",
    "meta ad": "Facebook Ads",
    "google": "Google",
    "google ads": "Google",
    "google ad": "Google",
    "adwords": "Google",
    "website": "Website",
    "web": "Website",
    "chat widget": "Website",
    "chat_widget": "Website",
    "portal": "Portal",
    "portal_self_serve": "Portal",
    "self serve": "Portal",
    "self_serve": "Portal",
}

# Lead statuses that count as an "open" deal (not yet won/lost).
_OPEN_LEAD_STATUSES = (
    LeadStatus.new.value,
    LeadStatus.contacted.value,
    LeadStatus.qualified.value,
    LeadStatus.proposal.value,
    LeadStatus.negotiation.value,
)

_CLOSED_LEAD_STATUSES = (LeadStatus.won.value, LeadStatus.lost.value)


def _enum_str(value, enum_cls, label: str) -> str | None:
    """Validate ``value`` against ``enum_cls`` and return its string value.

    Sub stores CRM's PG-enum columns as plain strings (Phase 3 §1.7), so
    every write path normalises enum members / raw strings to ``.value``.
    """
    member = validate_enum(value, enum_cls, label)
    return member.value if member is not None else None


def _resolve_owner_agent_id(db: Session, subscriber_id) -> uuid.UUID | None:
    """Phase 4 stub (risk #8).

    The CRM resolved a lead's owner agent from the inbox: the active
    ConversationAssignment for the person, falling back to the author of the
    last agent-authored message. Those models (``crm_agents``,
    conversations) arrive with the Phase 4 inbox port — until then leads
    land unowned (visible as "unassigned" in the kanban).
    """
    return None


def _lead_title_from_subscriber(subscriber: Subscriber | None) -> str | None:
    if not subscriber:
        return None
    if subscriber.display_name:
        return subscriber.display_name.strip() or None
    name = " ".join(
        part for part in [subscriber.first_name, subscriber.last_name] if part
    ).strip()
    if name:
        return name
    if subscriber.email:
        return subscriber.email.strip() or None
    if subscriber.phone:
        return subscriber.phone.strip() or None
    return None


def _is_placeholder_lead_title(value: str | None) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip().lower()
    return normalized in {"website chat", "website chat lead"}


def _normalize_lead_source(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    mapped = _LEAD_SOURCE_NORMALIZED_MAP.get(candidate.lower())
    if mapped:
        return mapped
    if candidate in LEAD_SOURCE_OPTIONS:
        return candidate
    return None


def _normalize_lead_source_or_400(value: str | None) -> str | None:
    normalized = _normalize_lead_source(value)
    if value and value.strip() and not normalized:
        raise HTTPException(status_code=400, detail="Invalid lead_source")
    return normalized


def _derive_lead_source_from_attribution(attribution: dict | None) -> str | None:
    if not isinstance(attribution, dict):
        return None

    keys = (
        "source",
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "referer_uri",
        "ref",
        "campaign_id",
        "ad_id",
        "adgroup_id",
        "adset_id",
    )
    values: list[str] = []
    for key in keys:
        raw = attribution.get(key)
        if raw is None:
            continue
        candidate = (raw if isinstance(raw, str) else str(raw)).strip().lower()
        if candidate:
            values.append(candidate)

    combined = " ".join(values)
    if not combined:
        return None
    if "google" in combined or "adwords" in combined or "gclid" in combined:
        return "Google"
    if "portal" in combined:
        return "Portal"
    if "instagram" in combined or "ig_" in combined or " ig " in f" {combined} ":
        return "Instagram Ads"
    if "facebook" in combined or "fb" in combined or "meta" in combined:
        return "Facebook Ads"
    if (
        "referrer" in combined
        or "referral" in combined
        or "referer" in combined
        or "ref=" in combined
    ):
        return "Referrer"
    if "website" in combined or "web" in combined:
        return "Website"
    return None


def _infer_lead_source(
    db: Session, subscriber: Subscriber | None, metadata: dict | None
) -> str | None:
    """Best-effort lead-source inference.

    Kept: attribution blobs on the lead metadata / subscriber metadata (pure
    dict inspection). Dropped until Phase 4 (risk #8): inference from recent
    inbound inbox messages and person channels — those models live with the
    CRM inbox and have not been ported.
    """
    metadata_attr = metadata.get("attribution") if isinstance(metadata, dict) else None
    inferred = _derive_lead_source_from_attribution(
        metadata_attr if isinstance(metadata_attr, dict) else None
    )
    if inferred:
        return inferred
    subscriber_meta = (
        subscriber.metadata_
        if subscriber is not None and isinstance(subscriber.metadata_, dict)
        else {}
    )
    subscriber_attr = (
        subscriber_meta.get("attribution")
        if isinstance(subscriber_meta, dict)
        else None
    )
    return _derive_lead_source_from_attribution(
        subscriber_attr if isinstance(subscriber_attr, dict) else None
    )


def _lead_dedup_enabled(db: Session) -> bool:
    value = settings_spec.resolve_value(
        db, SettingDomain.subscriber, "lead_dedup_enabled"
    )
    if value is None:
        return True  # default on: one open lead per subscriber
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _default_currency(db: Session) -> str | None:
    value = settings_spec.resolve_value(db, SettingDomain.billing, "default_currency")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _find_open_duplicate_lead(db: Session, subscriber_id, *, pipeline_id=None):
    """The subscriber's most recent open lead within the same pipeline bucket.

    Scope is per-(subscriber, pipeline). A null pipeline is its own bucket (it
    only collides with other null-pipeline open leads), matching the partial
    unique index ``uq_leads_one_open_per_subscriber_pipeline`` that COALESCEs
    a null pipeline to a sentinel UUID.
    """
    query = (
        db.query(Lead)
        .filter(Lead.subscriber_id == subscriber_id)
        .filter(Lead.is_active.is_(True))
        .filter(Lead.status.in_(_OPEN_LEAD_STATUSES))
    )
    if pipeline_id is None:
        query = query.filter(Lead.pipeline_id.is_(None))
    else:
        query = query.filter(Lead.pipeline_id == pipeline_id)
    return query.order_by(Lead.created_at.desc()).first()


def _apply_lead_closed_at(
    lead: Lead,
    status: str | None,
    *,
    previous_status: str | None = None,
) -> None:
    if status in _CLOSED_LEAD_STATUSES:
        # Stamp close time on open -> closed, or backfill if missing.
        if previous_status not in _CLOSED_LEAD_STATUSES or lead.closed_at is None:
            lead.closed_at = datetime.now(UTC)
        return

    # Clear close timestamp if a previously closed lead is reopened.
    if previous_status in _CLOSED_LEAD_STATUSES:
        lead.closed_at = None


def _apply_lead_status_from_quote(db: Session, quote: Quote, status: str | None):
    if not quote or not status or not quote.lead_id:
        return
    lead = db.get(Lead, quote.lead_id)
    if not lead:
        return
    previous_status = lead.status
    if status == QuoteStatus.accepted.value:
        lead.status = LeadStatus.won.value
    elif status == QuoteStatus.rejected.value:
        lead.status = LeadStatus.lost.value
    else:
        return
    if lead.owner_agent_id is None:
        lead.owner_agent_id = _resolve_owner_agent_id(db, lead.subscriber_id)
    _apply_lead_closed_at(lead, lead.status, previous_status=previous_status)
    db.commit()


def _uuid_from_metadata(metadata: dict | None, key: str):
    if not isinstance(metadata, dict):
        return None
    value = metadata.get(key)
    if not value:
        return None
    try:
        return coerce_uuid(str(value))
    except Exception:
        return None


def _datetime_from_metadata(metadata: dict | None, key: str) -> datetime | None:
    if not isinstance(metadata, dict):
        return None
    value = metadata.get(key)
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _quote_owner_from_lead(db: Session, lead_id) -> uuid.UUID | None:
    """Phase 4 stub (risk #8).

    The CRM derived a quote's owner from the lead's owning agent
    (``CrmAgent.person_id``). Agents arrive with the Phase 4 inbox port;
    until then only an explicit ``owner_person_id`` (payload or metadata)
    sets quote ownership.
    """
    return None


def _prepare_quote_ownership(
    db: Session, data: dict, *, existing: Quote | None = None
) -> None:
    metadata = data.get("metadata_")
    if not isinstance(metadata, dict):
        metadata = (
            existing.metadata_
            if existing is not None and isinstance(existing.metadata_, dict)
            else None
        )

    # ``owner_person_id`` is a staff UUID carried verbatim (§1.8) — no
    # existence check against a people table; the staff map resolves display.
    if not data.get("owner_person_id") and (
        existing is None or not existing.owner_person_id
    ):
        owner_from_meta = _uuid_from_metadata(metadata, "owner_person_id")
        owner_from_lead = _quote_owner_from_lead(
            db, data.get("lead_id") or (existing.lead_id if existing else None)
        )
        owner_person_id = owner_from_meta or owner_from_lead
        if owner_person_id:
            data["owner_person_id"] = owner_person_id

    if data.get("sent_at") is None:
        sent_from_meta = _datetime_from_metadata(metadata, "sent_at")
        if sent_from_meta is not None:
            data["sent_at"] = sent_from_meta

    status = data.get("status")
    if (
        status == QuoteStatus.sent.value
        and data.get("sent_at") is None
        and (existing is None or existing.sent_at is None)
    ):
        data["sent_at"] = datetime.now(UTC)


def _line_amount(quantity, unit_price, discount_percent) -> Decimal:
    """Net line amount: quantity * unit_price, less the line discount percent."""
    qty = Decimal(quantity or 0)
    price = Decimal(unit_price or 0)
    discount = Decimal(discount_percent or 0)
    if discount < 0:
        discount = Decimal("0")
    if discount > 100:
        discount = Decimal("100")
    gross = qty * price
    net = gross * (Decimal("100") - discount) / Decimal("100")
    if net < 0:
        net = Decimal("0")
    return net.quantize(Decimal("0.01"))


def _recalculate_quote_totals(db: Session, quote: Quote) -> None:
    items = db.query(QuoteLineItem).filter(QuoteLineItem.quote_id == quote.id).all()
    # Subtotal is the sum of net (discounted) line amounts.
    subtotal = round_money(
        sum((Decimal(item.amount or 0) for item in items), Decimal("0.00"))
    )
    quote.subtotal = subtotal
    # Auto-derive tax from the applied rate when one is set; otherwise keep the
    # manually entered tax_total. Tax always follows the (discounted) subtotal.
    if quote.tax_rate is not None:
        rate = Decimal(quote.tax_rate or 0)
        quote.tax_total = round_money(subtotal * rate / Decimal("100"))
    quote.total = subtotal + Decimal(quote.tax_total or 0)
    db.commit()


def _upgrade_party_status_to_customer(subscriber: Subscriber | None) -> None:
    """Won lead / accepted quote converts a prospect into a customer (§1.3)."""
    if subscriber is None:
        return
    if subscriber.party_status in (PartyStatus.lead.value, PartyStatus.contact.value):
        subscriber.party_status = PartyStatus.customer.value


def _ensure_project_from_quote(db: Session, quote: Quote, sales_order_id: str | None):
    """Deferred to the projects service port (Phase 3 PR 6).

    The CRM pipeline creates an install project when a quote is accepted
    (template by ``metadata.project_type``, status active, idempotent on
    ``Project.metadata_["quote_id"]``). Sub's projects *service* (template
    instantiation, fiber-stage engine) has not been ported yet — the projects
    PR rewires this hook onto it. Until then accepted quotes create only the
    sales order.
    """
    _logger.info(
        "sales_project_from_quote_deferred quote_id=%s sales_order_id=%s "
        "(projects service port pending)",
        quote.id,
        sales_order_id,
    )
    return None


def _emit_lead_created(db: Session, lead: Lead) -> None:
    try:
        emit_event(
            db,
            EventType.lead_created,
            {
                "lead_id": str(lead.id),
                "status": lead.status,
                "lead_source": lead.lead_source,
                "pipeline_id": str(lead.pipeline_id) if lead.pipeline_id else None,
            },
            subscriber_id=lead.subscriber_id,
        )
    except Exception:
        _logger.warning("lead_created_event_failed lead_id=%s", lead.id, exc_info=True)


def _emit_quote_accepted(db: Session, quote: Quote, sales_order_id) -> None:
    try:
        emit_event(
            db,
            EventType.quote_accepted,
            {
                "quote_id": str(quote.id),
                "total": str(quote.total or 0),
                "currency": quote.currency,
                "sales_order_id": str(sales_order_id) if sales_order_id else None,
            },
            subscriber_id=quote.subscriber_id,
        )
    except Exception:
        _logger.warning(
            "quote_accepted_event_failed quote_id=%s", quote.id, exc_info=True
        )


def _handle_quote_accepted(db: Session, quote: Quote) -> None:
    """The unchanged sales-service pipeline (§2.2 step 4): accepted quote →
    sales order (idempotent on quote_id) → install project (PR 6 stub)."""
    from app.services import sales_orders as sales_order_service

    _upgrade_party_status_to_customer(quote.subscriber)
    db.commit()
    db.refresh(quote)

    sales_order = sales_order_service.sales_orders.create_from_quote(db, str(quote.id))
    _ensure_project_from_quote(db, quote, str(sales_order.id) if sales_order else None)
    _emit_quote_accepted(db, quote, sales_order.id if sales_order else None)


class Pipelines(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload):
        pipeline = Pipeline(**payload.model_dump())
        db.add(pipeline)
        db.commit()
        db.refresh(pipeline)
        return pipeline

    @staticmethod
    def get(db: Session, pipeline_id: str):
        pipeline = db.get(Pipeline, coerce_uuid(pipeline_id))
        if not pipeline:
            raise HTTPException(status_code=404, detail="Pipeline not found")
        return pipeline

    @staticmethod
    def list(
        db: Session,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(Pipeline)
        if is_active is None:
            query = query.filter(Pipeline.is_active.is_(True))
        else:
            query = query.filter(Pipeline.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": Pipeline.created_at, "name": Pipeline.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, pipeline_id: str, payload):
        pipeline = db.get(Pipeline, coerce_uuid(pipeline_id))
        if not pipeline:
            raise HTTPException(status_code=404, detail="Pipeline not found")
        data = payload.model_dump(exclude_unset=True)
        for key, value in data.items():
            setattr(pipeline, key, value)
        db.commit()
        db.refresh(pipeline)
        return pipeline

    @staticmethod
    def delete(db: Session, pipeline_id: str):
        pipeline = db.get(Pipeline, coerce_uuid(pipeline_id))
        if not pipeline:
            raise HTTPException(status_code=404, detail="Pipeline not found")
        pipeline.is_active = False
        db.commit()


class PipelineStages(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload):
        pipeline = db.get(Pipeline, payload.pipeline_id)
        if not pipeline:
            raise HTTPException(status_code=404, detail="Pipeline not found")
        stage = PipelineStage(**payload.model_dump())
        db.add(stage)
        db.commit()
        db.refresh(stage)
        return stage

    @staticmethod
    def list(
        db: Session,
        pipeline_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(PipelineStage)
        if pipeline_id:
            query = query.filter(PipelineStage.pipeline_id == coerce_uuid(pipeline_id))
        if is_active is None:
            query = query.filter(PipelineStage.is_active.is_(True))
        else:
            query = query.filter(PipelineStage.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "order_index": PipelineStage.order_index,
                "created_at": PipelineStage.created_at,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, stage_id: str, payload):
        stage = db.get(PipelineStage, coerce_uuid(stage_id))
        if not stage:
            raise HTTPException(status_code=404, detail="Pipeline stage not found")
        data = payload.model_dump(exclude_unset=True)
        for key, value in data.items():
            setattr(stage, key, value)
        db.commit()
        db.refresh(stage)
        return stage


class Leads(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload):
        data = payload.model_dump()
        if data.get("status"):
            data["status"] = _enum_str(data["status"], LeadStatus, "status")
        if "lead_source" in data:
            data["lead_source"] = _normalize_lead_source_or_400(data.get("lead_source"))

        subscriber_id = data.get("subscriber_id")
        if not subscriber_id:
            raise HTTPException(status_code=400, detail="subscriber_id is required")

        subscriber = db.get(Subscriber, subscriber_id)
        if not subscriber:
            raise HTTPException(status_code=404, detail="Subscriber not found")

        # Dedup: a subscriber shouldn't have two open leads. If one exists,
        # return it (idempotent) instead of creating a duplicate pipeline
        # entry. Scoped to the requested pipeline when one is given.
        dedup_enabled = _lead_dedup_enabled(db)
        if dedup_enabled:
            duplicate = _find_open_duplicate_lead(
                db, subscriber_id, pipeline_id=data.get("pipeline_id")
            )
            if duplicate is not None:
                metadata = dict(duplicate.metadata_ or {})
                metadata["dedup_hits"] = int(metadata.get("dedup_hits") or 0) + 1
                duplicate.metadata_ = metadata
                db.commit()
                db.refresh(duplicate)
                _logger.info(
                    "lead_dedup_returned_existing subscriber_id=%s lead_id=%s",
                    subscriber_id,
                    duplicate.id,
                )
                # Transient signal for callers (e.g. web route) to distinguish
                # a deduped return from a freshly created lead. Not persisted.
                duplicate.dedup_returned_existing = True
                return duplicate

        # Auto-upgrade the party to at least 'contact' status if they're a lead.
        if subscriber.party_status == PartyStatus.lead.value:
            subscriber.party_status = PartyStatus.contact.value

        title_value = data.get("title")
        if (
            not title_value
            or (isinstance(title_value, str) and not title_value.strip())
            or _is_placeholder_lead_title(title_value)
        ):
            data["title"] = _lead_title_from_subscriber(subscriber)

        if not data.get("owner_agent_id"):
            data["owner_agent_id"] = _resolve_owner_agent_id(db, subscriber_id)
        if not data.get("currency"):
            default_currency = _default_currency(db)
            if default_currency:
                data["currency"] = default_currency
        if not data.get("lead_source"):
            data["lead_source"] = _infer_lead_source(
                db, subscriber, data.get("metadata_")
            )
        lead = Lead(**data)
        _apply_lead_closed_at(lead, lead.status)
        db.add(lead)
        try:
            db.commit()
        except IntegrityError:
            # A concurrent create won the partial unique index
            # (uq_leads_one_open_per_subscriber_pipeline). Resolve the race by
            # returning the existing open lead instead of surfacing a 500.
            db.rollback()
            if dedup_enabled:
                existing = _find_open_duplicate_lead(
                    db, subscriber_id, pipeline_id=data.get("pipeline_id")
                )
                if existing is not None:
                    _logger.info(
                        "lead_dedup_race_resolved subscriber_id=%s lead_id=%s",
                        subscriber_id,
                        existing.id,
                    )
                    existing.dedup_returned_existing = True
                    return existing
            raise
        db.refresh(lead)
        _emit_lead_created(db, lead)
        return lead

    @staticmethod
    def get(db: Session, lead_id: str):
        lead = db.get(Lead, coerce_uuid(lead_id))
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        return lead

    @staticmethod
    def list(
        db: Session,
        pipeline_id: str | None,
        stage_id: str | None,
        owner_agent_id: str | None,
        status: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
        lead_source: str | None = None,
        search: str | None = None,
    ):
        query = db.query(Lead)
        if pipeline_id:
            query = query.filter(Lead.pipeline_id == coerce_uuid(pipeline_id))
        if stage_id:
            query = query.filter(Lead.stage_id == coerce_uuid(stage_id))
        if owner_agent_id:
            query = query.filter(Lead.owner_agent_id == coerce_uuid(owner_agent_id))
        if status:
            query = query.filter(Lead.status == _enum_str(status, LeadStatus, "status"))
        if lead_source:
            query = query.filter(
                func.lower(Lead.lead_source) == lead_source.strip().lower()
            )
        if search:
            pattern = f"%{search.strip()}%"
            if pattern != "%%":
                full_name = func.trim(
                    func.coalesce(Subscriber.first_name, "")
                    + " "
                    + func.coalesce(Subscriber.last_name, "")
                )
                query = query.outerjoin(
                    Subscriber, Subscriber.id == Lead.subscriber_id
                ).filter(
                    or_(
                        Lead.title.ilike(pattern),
                        Subscriber.display_name.ilike(pattern),
                        full_name.ilike(pattern),
                        Subscriber.first_name.ilike(pattern),
                        Subscriber.last_name.ilike(pattern),
                        Subscriber.email.ilike(pattern),
                        Subscriber.phone.ilike(pattern),
                    )
                )
        if is_active is None:
            query = query.filter(Lead.is_active.is_(True))
        else:
            query = query.filter(Lead.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": Lead.created_at, "updated_at": Lead.updated_at},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, lead_id: str, payload):
        lead = db.get(Lead, coerce_uuid(lead_id))
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        previous_status = lead.status
        data = payload.model_dump(exclude_unset=True)
        if "status" in data:
            data["status"] = _enum_str(data["status"], LeadStatus, "status")
        if "lead_source" in data:
            data["lead_source"] = _normalize_lead_source_or_400(data.get("lead_source"))

        # If subscriber_id is being changed, validate it exists.
        if data.get("subscriber_id"):
            subscriber = db.get(Subscriber, data["subscriber_id"])
            if not subscriber:
                raise HTTPException(status_code=404, detail="Subscriber not found")
        else:
            subscriber = lead.subscriber

        if "title" in data:
            title_value = data.get("title")
            if (
                not title_value
                or (isinstance(title_value, str) and not title_value.strip())
                or _is_placeholder_lead_title(title_value)
            ):
                data["title"] = (
                    _lead_title_from_subscriber(subscriber) if subscriber else None
                )

        for key, value in data.items():
            setattr(lead, key, value)

        # When the lead is won, upgrade the party to customer.
        if data.get("status") == LeadStatus.won.value:
            _upgrade_party_status_to_customer(lead.subscriber)
        if "status" in data:
            if lead.owner_agent_id is None and lead.status in _CLOSED_LEAD_STATUSES:
                lead.owner_agent_id = _resolve_owner_agent_id(db, lead.subscriber_id)
            _apply_lead_closed_at(lead, lead.status, previous_status=previous_status)

        db.commit()
        db.refresh(lead)
        return lead

    @staticmethod
    def delete(db: Session, lead_id: str):
        lead = db.get(Lead, coerce_uuid(lead_id))
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        lead.is_active = False
        db.commit()

    @staticmethod
    def kanban_view(db: Session, pipeline_id: str | None = None) -> dict:
        """Return kanban board data with columns and records.

        Returns:
            dict with 'columns' (list of stage info) and 'records' (leads).
        """
        if pipeline_id:
            stages = (
                db.query(PipelineStage)
                .filter(PipelineStage.pipeline_id == coerce_uuid(pipeline_id))
                .filter(PipelineStage.is_active.is_(True))
                .order_by(PipelineStage.order_index.asc())
                .all()
            )
            leads_rows = (
                db.query(Lead)
                .filter(Lead.pipeline_id == coerce_uuid(pipeline_id))
                .filter(Lead.is_active.is_(True))
                .all()
            )
        else:
            stages = (
                db.query(PipelineStage)
                .filter(PipelineStage.is_active.is_(True))
                .order_by(PipelineStage.order_index.asc())
                .all()
            )
            leads_rows = db.query(Lead).filter(Lead.is_active.is_(True)).all()

        columns = [
            {
                "id": str(stage.id),
                "title": stage.name,
                "order_index": stage.order_index,
                "default_probability": stage.default_probability,
            }
            for stage in stages
        ]

        # Batch load subscribers to avoid N+1 queries.
        subscriber_ids = [
            lead.subscriber_id for lead in leads_rows if lead.subscriber_id
        ]
        subscribers = (
            db.query(Subscriber).filter(Subscriber.id.in_(subscriber_ids)).all()
            if subscriber_ids
            else []
        )
        subscriber_map = {s.id: s for s in subscribers}

        records = []
        for lead in leads_rows:
            subscriber = (
                subscriber_map.get(lead.subscriber_id) if lead.subscriber_id else None
            )
            contact_name = ""
            if subscriber:
                contact_name = (
                    subscriber.display_name
                    or f"{subscriber.first_name or ''} {subscriber.last_name or ''}".strip()
                )

            records.append(
                {
                    "id": str(lead.id),
                    "stage": str(lead.stage_id) if lead.stage_id else None,
                    "title": lead.title or f"Lead #{str(lead.id)[:8]}",
                    "contact_name": contact_name,
                    "estimated_value": float(lead.estimated_value)
                    if lead.estimated_value
                    else None,
                    "probability": lead.probability,
                    "weighted_value": float(lead.weighted_value)
                    if lead.weighted_value
                    else None,
                    "status": lead.status or LeadStatus.new.value,
                    "currency": lead.currency or "",
                    "url": f"/admin/crm/leads/{lead.id}",
                }
            )

        return {"columns": columns, "records": records}

    @staticmethod
    def update_stage(db: Session, lead_id: str, new_stage_id: str) -> dict:
        """Move a lead to a new stage, defaulting probability from the stage.

        Returns:
            dict with updated lead info.
        """
        lead = db.get(Lead, coerce_uuid(lead_id))
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")

        stage = db.get(PipelineStage, coerce_uuid(new_stage_id))
        if not stage:
            raise HTTPException(status_code=404, detail="Stage not found")

        lead.stage_id = stage.id
        lead.pipeline_id = stage.pipeline_id

        # Auto-update probability from stage default if not already set.
        if lead.probability is None:
            lead.probability = stage.default_probability

        db.commit()
        db.refresh(lead)

        return {
            "id": str(lead.id),
            "stage_id": str(lead.stage_id),
            "pipeline_id": str(lead.pipeline_id) if lead.pipeline_id else None,
            "probability": lead.probability,
        }

    @staticmethod
    def bulk_assign_pipeline(
        db: Session,
        pipeline_id: str,
        stage_id: str | None = None,
        *,
        scope: str = "unassigned",
    ) -> int:
        pipeline = db.get(Pipeline, coerce_uuid(pipeline_id))
        if not pipeline:
            raise HTTPException(status_code=404, detail="Pipeline not found")

        resolved_stage_id = None
        if stage_id:
            stage = db.get(PipelineStage, coerce_uuid(stage_id))
            if not stage or stage.pipeline_id != pipeline.id:
                raise HTTPException(
                    status_code=400,
                    detail="Selected stage does not belong to this pipeline",
                )
            resolved_stage_id = stage.id

        query = db.query(Lead).filter(Lead.is_active.is_(True))
        if scope == "unassigned":
            query = query.filter(Lead.pipeline_id.is_(None))
        elif scope != "all_active":
            raise HTTPException(status_code=400, detail="Unsupported bulk assign scope")

        count = query.update(
            {
                Lead.pipeline_id: pipeline.id,
                Lead.stage_id: resolved_stage_id,
            },
            synchronize_session=False,
        )
        db.commit()
        return int(count)


class Quotes(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload):
        data = payload.model_dump()
        if data.get("status"):
            data["status"] = _enum_str(data["status"], QuoteStatus, "status")

        subscriber_id = data.get("subscriber_id")
        if not subscriber_id:
            raise HTTPException(status_code=400, detail="subscriber_id is required")

        subscriber = db.get(Subscriber, subscriber_id)
        if not subscriber:
            raise HTTPException(status_code=404, detail="Subscriber not found")

        # Set quote_name from the subscriber's display name.
        if not data.get("metadata_"):
            data["metadata_"] = {}
        if isinstance(data["metadata_"], dict):
            display_name = (
                subscriber.display_name
                or f"{subscriber.first_name} {subscriber.last_name}"
            )
            data["metadata_"]["quote_name"] = display_name

        _prepare_quote_ownership(db, data)

        if not data.get("currency"):
            default_currency = _default_currency(db)
            if default_currency:
                data["currency"] = default_currency
        quote = Quote(**data)
        db.add(quote)
        db.commit()
        db.refresh(quote)
        _apply_lead_status_from_quote(db, quote, quote.status)
        if quote.status == QuoteStatus.accepted.value:
            _handle_quote_accepted(db, quote)
        return quote

    @staticmethod
    def get(db: Session, quote_id: str):
        quote = db.get(
            Quote,
            coerce_uuid(quote_id),
            options=[selectinload(Quote.line_items)],
        )
        if not quote:
            raise HTTPException(status_code=404, detail="Quote not found")
        return quote

    @staticmethod
    def list(
        db: Session,
        lead_id: str | None,
        status: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
        search: str | None = None,
    ):
        query = db.query(Quote)
        if lead_id:
            query = query.filter(Quote.lead_id == coerce_uuid(lead_id))
        if status:
            query = query.filter(
                Quote.status == _enum_str(status, QuoteStatus, "status")
            )
        if search:
            like = f"%{search.strip()}%"
            query = query.outerjoin(
                Subscriber, Quote.subscriber_id == Subscriber.id
            ).filter(
                or_(
                    Subscriber.display_name.ilike(like),
                    Subscriber.first_name.ilike(like),
                    Subscriber.last_name.ilike(like),
                    Subscriber.email.ilike(like),
                    cast(Quote.id, String).ilike(like),
                )
            )
        if is_active is None:
            query = query.filter(Quote.is_active.is_(True))
        else:
            query = query.filter(Quote.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": Quote.created_at, "updated_at": Quote.updated_at},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def count_by_status(db: Session) -> dict:
        """Return counts by quote status."""
        results = (
            db.query(Quote.status, func.count(Quote.id))
            .filter(Quote.is_active.is_(True))
            .group_by(Quote.status)
            .all()
        )
        counts = {s.value: 0 for s in QuoteStatus}
        for status_val, count in results:
            if status_val:
                counts[str(status_val)] = count
        counts["total"] = sum(v for key, v in counts.items() if key != "total")
        return counts

    @staticmethod
    def update(db: Session, quote_id: str, payload):
        quote = db.get(Quote, coerce_uuid(quote_id))
        if not quote:
            raise HTTPException(status_code=404, detail="Quote not found")
        previous_status = quote.status
        data = payload.model_dump(exclude_unset=True)
        if "status" in data:
            data["status"] = _enum_str(data["status"], QuoteStatus, "status")

        # If subscriber_id is being changed, validate it exists.
        if data.get("subscriber_id"):
            subscriber = db.get(Subscriber, data["subscriber_id"])
            if not subscriber:
                raise HTTPException(status_code=404, detail="Subscriber not found")

        _prepare_quote_ownership(db, data, existing=quote)

        for key, value in data.items():
            setattr(quote, key, value)

        # When the quote is accepted, upgrade the party to customer.
        if data.get("status") == QuoteStatus.accepted.value:
            _upgrade_party_status_to_customer(quote.subscriber)

        db.commit()
        db.refresh(quote)
        # Re-derive totals when the tax rate changed, so tax follows.
        if "tax_rate" in data:
            _recalculate_quote_totals(db, quote)
            db.refresh(quote)
        if "status" in data:
            _apply_lead_status_from_quote(db, quote, quote.status)
        transitioned_to_accepted = (
            previous_status != QuoteStatus.accepted.value
            and quote.status == QuoteStatus.accepted.value
        )
        if transitioned_to_accepted:
            _handle_quote_accepted(db, quote)
        return quote

    @staticmethod
    def delete(db: Session, quote_id: str):
        quote = db.get(Quote, coerce_uuid(quote_id))
        if not quote:
            raise HTTPException(status_code=404, detail="Quote not found")
        quote.is_active = False
        db.commit()


class QuoteLineItems(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload):
        quote = db.get(Quote, payload.quote_id)
        if not quote:
            raise HTTPException(status_code=404, detail="Quote not found")
        data = payload.model_dump()
        # ``inventory_item_id`` is a CRM inventory UUID carried verbatim —
        # inventory is Phase 5, so there is nothing to validate against (§1.4).
        # Always derive amount server-side (net of any line discount).
        data["amount"] = _line_amount(
            data.get("quantity"), data.get("unit_price"), data.get("discount_percent")
        )
        item = QuoteLineItem(**data)
        db.add(item)
        db.commit()
        _recalculate_quote_totals(db, quote)
        db.refresh(item)
        return item

    @staticmethod
    def update(db: Session, item_id: str, payload):
        item = db.get(QuoteLineItem, coerce_uuid(item_id))
        if not item:
            raise HTTPException(status_code=404, detail="Quote line item not found")
        data = payload.model_dump(exclude_unset=True)
        for key, value in data.items():
            setattr(item, key, value)
        if {"quantity", "unit_price", "discount_percent"} & set(data):
            item.amount = _line_amount(
                item.quantity, item.unit_price, item.discount_percent
            )
        db.commit()
        db.refresh(item)
        quote = db.get(Quote, item.quote_id)
        if quote:
            _recalculate_quote_totals(db, quote)
        return item

    @staticmethod
    def list(
        db: Session,
        quote_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(QuoteLineItem)
        if quote_id:
            query = query.filter(QuoteLineItem.quote_id == coerce_uuid(quote_id))
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": QuoteLineItem.created_at},
        )
        return apply_pagination(query, limit, offset).all()


# Singleton instances
pipelines = Pipelines()
pipeline_stages = PipelineStages()
leads = Leads()
quotes = Quotes()
quote_line_items = QuoteLineItems()
