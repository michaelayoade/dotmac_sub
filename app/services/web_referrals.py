"""Context builders for native admin referral pages.

Ported from the CRM's ``app/web/admin/crm_referrals.py`` onto sub identity:
referrer/referred are subscribers (not CRM people), rows link to the existing
``/admin/customers/{person|business}/{id}`` detail pages, and the program
lives in the five ``referral_*`` settings keys (``SettingDomain.subscriber``)
surfaced on the system settings page — there is no program table.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.models.party import PartyContactPoint, PartyContactPointType
from app.models.referral_native import (
    Referral,
    ReferralRewardStatus,
    ReferralStatus,
)
from app.models.subscriber import Subscriber
from app.schemas.status_presentation import StatusTone
from app.services.common import coerce_uuid
from app.services.list_query import (
    ListDefinition,
    ListFieldDefinition,
    ListQuery,
    PageMeta,
)
from app.services.referrals import referrals as referrals_service
from app.services.ui_contracts import Kpi, StateValue

# The five referral_* program keys render on the generic system settings page
# under the subscriber domain (settings_spec gives them labels).
PROGRAM_SETTINGS_URL = "/admin/system/settings?domain=subscriber"

STATUSES = [s.value for s in ReferralStatus]
REWARD_STATUSES = [s.value for s in ReferralRewardStatus]

# The referrals list's declared query capabilities (Carbon/WCAG list standard):
# filterable by status/reward, sortable by created/status, deterministic order.
REFERRAL_LIST_DEFINITION = ListDefinition(
    key="referrals",
    fields=(
        ListFieldDefinition("created_at", "Created", sortable=True),
        ListFieldDefinition("status", "Status", filterable=True, sortable=True),
        ListFieldDefinition("reward_status", "Reward", filterable=True),
    ),
    default_sort="created_at",
    default_sort_dir="desc",
)
_REFERRAL_SORT_COLUMNS = {
    "created_at": Referral.created_at,
    "status": Referral.status,
}


def _subscriber_name(subscriber: Subscriber | None) -> str:
    if subscriber is None:
        return "—"
    name = (
        subscriber.display_name
        or f"{subscriber.first_name or ''} {subscriber.last_name or ''}".strip()
        or subscriber.email
        or "—"
    )
    return str(name).strip() or "—"


def _subscriber_link(subscriber: Subscriber | None) -> str | None:
    if subscriber is None:
        return None
    kind = "business" if subscriber.is_business else "person"
    return f"/admin/customers/{kind}/{subscriber.id}"


def _capture_meta(db: Session, referral: Referral) -> dict:
    if referral.referred_party_id is not None:
        points = {
            point.channel_type: point.display_value or point.normalized_value
            for point in db.query(PartyContactPoint)
            .filter(PartyContactPoint.party_id == referral.referred_party_id)
            .filter(PartyContactPoint.is_active.is_(True))
            .filter(
                PartyContactPoint.channel_type.in_(
                    [
                        PartyContactPointType.email.value,
                        PartyContactPointType.phone.value,
                    ]
                )
            )
            .all()
        }
        return {
            "name": referral.referred_party.display_name
            if referral.referred_party is not None
            else None,
            "email": points.get(PartyContactPointType.email.value),
            "phone": points.get(PartyContactPointType.phone.value),
        }
    meta = referral.metadata_ if isinstance(referral.metadata_, dict) else {}
    capture = meta.get("capture")
    return capture if isinstance(capture, dict) else {}


def _reward_display(referral: Referral) -> str:
    amount = referral.reward_amount
    if amount is None:
        return "—"
    currency = (referral.reward_currency or "NGN").strip() or "NGN"
    return f"{currency} {amount:,.2f}"


def _referred_name(referral: Referral) -> str:
    if referral.referred_party is not None:
        return referral.referred_party.display_name
    if referral.referred_subscriber is not None:
        return _subscriber_name(referral.referred_subscriber)
    meta = referral.metadata_ if isinstance(referral.metadata_, dict) else {}
    capture_value = meta.get("capture")
    capture = capture_value if isinstance(capture_value, dict) else {}
    name = capture.get("name")
    return str(name).strip() if name else "—"


def _row(referral: Referral) -> dict:
    return {
        "id": str(referral.id),
        "referrer": _subscriber_name(referral.referrer),
        "referrer_href": _subscriber_link(referral.referrer),
        "referred": _referred_name(referral),
        "referred_href": _subscriber_link(referral.referred_subscriber),
        "status": referral.status,
        "reward_status": referral.reward_status,
        "reward": _reward_display(referral),
        "source": referral.source or "—",
        "created_at": referral.created_at,
        "qualified_at": referral.qualified_at,
        # Action gates (mirror the service guards so buttons never 409 on a
        # fresh page): qualify rescues pending/expired, issue pays a
        # qualified referral, reject voids anything not yet paid out.
        "can_qualify": referral.status
        in (ReferralStatus.pending.value, ReferralStatus.expired.value)
        and (
            referral.referred_party_id is None
            or referral.referred_subscriber_id is not None
        ),
        "can_issue": referral.status == ReferralStatus.qualified.value
        and referral.reward_status
        in (ReferralRewardStatus.pending.value, ReferralRewardStatus.approved.value),
        "can_reject": referral.status
        in (ReferralStatus.pending.value, ReferralStatus.qualified.value),
        "can_attach_account": referral.referred_party_id is not None
        and referral.referred_lead_id is not None
        and referral.referred_subscriber_id is None
        and referral.status
        in (
            ReferralStatus.pending.value,
            ReferralStatus.expired.value,
            ReferralStatus.qualified.value,
        ),
    }


def _cohort_url(*, status: ReferralStatus | None = None) -> str:
    query = REFERRAL_LIST_DEFINITION.build_query(
        search=None,
        filters={
            "status": status.value if status is not None else None,
            "reward_status": None,
        },
    )
    return query.url("/admin/referrals")


def _stats(db: Session) -> dict[str, Kpi | Decimal]:
    counts: dict[str, int] = {
        status: count
        for status, count in db.query(Referral.status, func.count(Referral.id))
        .filter(Referral.is_active.is_(True))
        .group_by(Referral.status)
        .all()
    }
    rewarded_total = (
        db.query(func.coalesce(func.sum(Referral.reward_amount), 0))
        .filter(Referral.is_active.is_(True))
        .filter(Referral.status == ReferralStatus.rewarded.value)
        .scalar()
    ) or Decimal("0")
    return {
        "total": Kpi(
            label="Total",
            value=StateValue.present(sum(counts.values())),
            cohort_url=_cohort_url(),
        ),
        "pending": Kpi(
            label="Pending",
            value=StateValue.present(counts.get(ReferralStatus.pending.value, 0)),
            cohort_url=_cohort_url(status=ReferralStatus.pending),
            tone=StatusTone.warning,
        ),
        "qualified": Kpi(
            label="Qualified (reward due)",
            value=StateValue.present(counts.get(ReferralStatus.qualified.value, 0)),
            cohort_url=_cohort_url(status=ReferralStatus.qualified),
            tone=StatusTone.info,
        ),
        "rewarded": Kpi(
            label="Rewarded",
            value=StateValue.present(counts.get(ReferralStatus.rewarded.value, 0)),
            cohort_url=_cohort_url(status=ReferralStatus.rewarded),
            tone=StatusTone.positive,
        ),
        "rewarded_total": rewarded_total,
    }


def _request_needs_canonicalization(
    *,
    list_query: ListQuery,
    status: str | None,
    reward_status: str | None,
    sort_by: str | None,
    sort_dir: str | None,
    page: int,
    per_page: int,
) -> bool:
    return (
        page != list_query.page
        or per_page != list_query.per_page
        or (status is not None and status != list_query.filter_value("status"))
        or (
            reward_status is not None
            and reward_status != list_query.filter_value("reward_status")
        )
        or (sort_by is not None and sort_by != list_query.sort_by)
        or (sort_dir is not None and sort_dir != list_query.sort_dir)
    )


def list_data(
    db: Session,
    *,
    status: str | None = None,
    reward_status: str | None = None,
    sort_by: str | None = None,
    sort_dir: str | None = None,
    page: int = 1,
    per_page: int = 25,
) -> dict:
    # Unknown filter/sort/page-size values are normalized to a valid state
    # rather than raising, matching the queue idiom — a stale bookmark degrades
    # to the default view, never a 400/500.
    requested_status = status
    requested_reward_status = reward_status
    status = status if status in STATUSES else None
    reward_status = reward_status if reward_status in REWARD_STATUSES else None
    safe_sort = (
        sort_by
        if sort_by in REFERRAL_LIST_DEFINITION.sortable_keys
        else REFERRAL_LIST_DEFINITION.default_sort
    )
    safe_dir = sort_dir if sort_dir in ("asc", "desc") else None
    safe_per_page = (
        per_page
        if per_page in REFERRAL_LIST_DEFINITION.per_page_options
        else REFERRAL_LIST_DEFINITION.default_per_page
    )
    requested_query = REFERRAL_LIST_DEFINITION.build_query(
        search=None,
        filters={"status": status, "reward_status": reward_status},
        sort_by=safe_sort,
        sort_dir=safe_dir,
        page=max(1, page),
        per_page=safe_per_page,
    )

    query = (
        db.query(Referral)
        .options(
            joinedload(Referral.referrer),
            joinedload(Referral.referred_subscriber),
            joinedload(Referral.referred_party),
        )
        .filter(Referral.is_active.is_(True))
    )
    if value := requested_query.filter_value("status"):
        query = query.filter(Referral.status == value)
    if value := requested_query.filter_value("reward_status"):
        query = query.filter(Referral.reward_status == value)

    total = query.count()
    page_meta = PageMeta.from_query(requested_query, total)
    list_query = requested_query.with_page(page_meta.page)
    sort_column = _REFERRAL_SORT_COLUMNS[list_query.sort_by]
    ordered = sort_column.desc() if list_query.sort_dir == "desc" else sort_column.asc()
    items = (
        # Unique tie-breaker keeps ordering deterministic across pages.
        query.order_by(ordered, Referral.id.asc())
        .offset((page_meta.page - 1) * list_query.per_page)
        .limit(list_query.per_page)
        .all()
    )

    return {
        "referrals": [_row(r) for r in items],
        "stats": _stats(db),
        "program": referrals_service.program(db),
        "statuses": STATUSES,
        "reward_statuses": REWARD_STATUSES,
        "status_filter": list_query.filter_value("status"),
        "reward_status_filter": list_query.filter_value("reward_status"),
        "list_query": list_query,
        "canonicalization_needed": _request_needs_canonicalization(
            list_query=list_query,
            status=requested_status,
            reward_status=requested_reward_status,
            sort_by=sort_by,
            sort_dir=sort_dir,
            page=page,
            per_page=per_page,
        ),
        "page_meta": page_meta,
        "page": page_meta.page,
        "per_page": page_meta.per_page,
        "total": page_meta.total_items,
        "total_pages": page_meta.total_pages,
        "program_settings_url": PROGRAM_SETTINGS_URL,
    }


def detail_data(db: Session, *, referral_id: str) -> dict | None:
    try:
        rid = coerce_uuid(str(referral_id))
    except Exception:  # noqa: BLE001 - malformed id → 404, not a 500
        return None
    referral = (
        db.query(Referral)
        .options(
            joinedload(Referral.referrer),
            joinedload(Referral.referred_subscriber),
            joinedload(Referral.referred_party),
            joinedload(Referral.code),
            joinedload(Referral.lead),
        )
        .filter(Referral.id == rid)
        .first()
    )
    if referral is None:
        return None
    meta = referral.metadata_ if isinstance(referral.metadata_, dict) else {}
    return {
        "referral": referral,
        "row": _row(referral),
        "capture": _capture_meta(db, referral),
        "code": referral.code.code if referral.code is not None else None,
        "lead_id": str(referral.referred_lead_id)
        if referral.referred_lead_id
        else None,
        "conversion_context": {
            "referred_party_id": str(referral.referred_party_id),
            "referred_lead_id": str(referral.referred_lead_id),
        }
        if referral.referred_party_id is not None
        and referral.referred_lead_id is not None
        else None,
        "reward_credit_id": meta.get("reward_credit_id"),
        "program": referrals_service.program(db),
        "program_settings_url": PROGRAM_SETTINGS_URL,
    }
