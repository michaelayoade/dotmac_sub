"""Read-only subscriber identity classification and duplicate evidence.

The subscriber table is a mixed legacy population: service accounts, leads,
contacts, test rows, and unresolved records share one shape.  This resolver
projects that population into review cohorts without changing any source row.

Contact details are grouping evidence, not identity proof.  Every duplicate
group produced here requires review; this module never merges, quarantines, or
backfills a Party.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.models.billing import Invoice, InvoiceStatus, Payment, PaymentStatus
from app.models.catalog import Subscription, SubscriptionStatus
from app.models.provisioning import ServiceOrder
from app.models.sales import Lead, Quote, QuoteStatus, SalesOrder, SalesOrderStatus
from app.models.subscriber import (
    NINVerificationStatus,
    Subscriber,
    SubscriberChannel,
    SubscriberNINVerification,
)
from app.models.support import Ticket


class LifecycleCohort(StrEnum):
    active_subscriber = "active_subscriber"
    inactive_subscriber = "inactive_subscriber"
    customer = "customer"
    lead = "lead"
    verified_contact = "verified_contact"
    unverified_record = "unverified_record"


class RecordClassification(StrEnum):
    production_candidate = "production_candidate"
    declared_nonproduction = "declared_nonproduction"
    suspected_nonproduction = "suspected_nonproduction"
    already_quarantined = "already_quarantined"


class ReviewDisposition(StrEnum):
    ready_for_party_backfill = "ready_for_party_backfill"
    retain_quarantine = "retain_quarantine"
    review_and_quarantine_nonproduction = "review_and_quarantine_nonproduction"
    manual_nonproduction_review = "manual_nonproduction_review"
    manual_duplicate_review = "manual_duplicate_review"
    manual_lifecycle_review = "manual_lifecycle_review"
    quarantine_unverified = "quarantine_unverified"


class DuplicateConfidence(StrEnum):
    weak = "weak"
    medium = "medium"
    high = "high"


_CONFIDENCE_RANK = {
    DuplicateConfidence.weak: 1,
    DuplicateConfidence.medium: 2,
    DuplicateConfidence.high: 3,
}
_NONPRODUCTION_VALUES = {
    "demo",
    "development",
    "nonproduction",
    "qa",
    "sandbox",
    "staging",
    "test",
    "training",
}
_TEST_EMAIL_DOMAINS = {
    "example.com",
    "example.net",
    "example.org",
    "localhost",
    "test.local",
}
_TEST_TOKEN_RE = re.compile(
    r"(^|[^a-z0-9])(demo|dummy|fake|qa|sample|test)([^a-z0-9]|$)"
)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_TERMINAL_ACCOUNT_STATUSES = frozenset({"canceled", "disabled"})


@dataclass(frozen=True)
class SubscriberIdentityFacts:
    subscriber_id: UUID
    first_name: str = ""
    last_name: str = ""
    display_name: str | None = None
    company_name: str | None = None
    legal_name: str | None = None
    email: str = ""
    phone: str | None = None
    nin: str | None = None
    email_verified: bool = False
    verified_nin: bool = False
    verified_contact_methods: frozenset[str] = frozenset()
    account_status: str = ""
    is_active: bool = True
    party_status: str | None = None
    metadata: dict[str, Any] | None = None
    has_lead: bool = False
    has_quote: bool = False
    has_accepted_quote: bool = False
    has_sales_order: bool = False
    has_customer_sales_order: bool = False
    has_service_order: bool = False
    has_any_subscription: bool = False
    has_active_subscription: bool = False
    has_billing_document: bool = False
    has_succeeded_payment: bool = False
    has_support_history: bool = False
    party_id: UUID | None = None


@dataclass(frozen=True)
class DuplicateCandidateGroup:
    group_id: str
    evidence_type: str
    confidence: DuplicateConfidence
    subscriber_ids: tuple[UUID, ...]
    automatic_merge_allowed: bool = False

    @property
    def member_count(self) -> int:
        return len(self.subscriber_ids)


@dataclass(frozen=True)
class SubscriberAuditRow:
    subscriber_id: UUID
    lifecycle_cohort: LifecycleCohort
    record_classification: RecordClassification
    recommended_disposition: ReviewDisposition
    lifecycle_evidence: tuple[str, ...]
    classification_evidence: tuple[str, ...]
    contradictions: tuple[str, ...]
    duplicate_group_ids: tuple[str, ...] = ()
    strongest_duplicate_confidence: DuplicateConfidence | None = None
    existing_party_id: UUID | None = None
    available_display_name_sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class SubscriberIdentityAudit:
    rows: tuple[SubscriberAuditRow, ...]
    duplicate_groups: tuple[DuplicateCandidateGroup, ...]
    generated_at: datetime | None = None
    missing_optional_sources: tuple[str, ...] = ()

    def summary(self) -> dict[str, Any]:
        lifecycle = Counter(row.lifecycle_cohort.value for row in self.rows)
        classifications = Counter(row.record_classification.value for row in self.rows)
        dispositions = Counter(row.recommended_disposition.value for row in self.rows)
        duplicate_confidence = Counter(
            group.confidence.value for group in self.duplicate_groups
        )
        duplicate_evidence = Counter(
            group.evidence_type for group in self.duplicate_groups
        )
        return {
            "audit_digest": subscriber_identity_audit_digest(self),
            "generated_at": (
                self.generated_at.isoformat() if self.generated_at else None
            ),
            "total_subscriber_rows": len(self.rows),
            "lifecycle_cohorts": dict(sorted(lifecycle.items())),
            "record_classifications": dict(sorted(classifications.items())),
            "recommended_dispositions": dict(sorted(dispositions.items())),
            "duplicate_groups": {
                "total": len(self.duplicate_groups),
                "by_confidence": dict(sorted(duplicate_confidence.items())),
                "by_evidence_type": dict(sorted(duplicate_evidence.items())),
                "automatic_merges": 0,
            },
            "artifact_contract": {
                "read_only": True,
                "contains_raw_contact_values": False,
                "automatic_merge_allowed": False,
                "missing_optional_sources": list(self.missing_optional_sources),
            },
        }


def subscriber_audit_row_fingerprint(row: SubscriberAuditRow) -> str:
    """Stable, PII-free fingerprint for one reviewed audit row."""

    payload = {
        "subscriber_id": str(row.subscriber_id),
        "lifecycle_cohort": row.lifecycle_cohort.value,
        "record_classification": row.record_classification.value,
        "recommended_disposition": row.recommended_disposition.value,
        "lifecycle_evidence": list(row.lifecycle_evidence),
        "classification_evidence": list(row.classification_evidence),
        "contradictions": list(row.contradictions),
        "duplicate_group_ids": list(row.duplicate_group_ids),
        "strongest_duplicate_confidence": (
            row.strongest_duplicate_confidence.value
            if row.strongest_duplicate_confidence
            else None
        ),
        "existing_party_id": (
            str(row.existing_party_id) if row.existing_party_id else None
        ),
        "available_display_name_sources": list(row.available_display_name_sources),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def subscriber_identity_audit_digest(audit: SubscriberIdentityAudit) -> str:
    """Fingerprint audit facts while deliberately excluding snapshot time.

    A later read-only run with identical facts has the same digest; any row,
    duplicate-group, installed evidence-source, or existing Party-binding drift
    changes it and invalidates reviewed decisions.
    """

    payload = {
        "contract_version": 1,
        "missing_optional_sources": sorted(audit.missing_optional_sources),
        "row_fingerprints": [
            subscriber_audit_row_fingerprint(row)
            for row in sorted(audit.rows, key=lambda item: str(item.subscriber_id))
        ],
        "duplicate_groups": [
            {
                "group_id": group.group_id,
                "evidence_type": group.evidence_type,
                "confidence": group.confidence.value,
                "subscriber_ids": [str(value) for value in group.subscriber_ids],
                "automatic_merge_allowed": group.automatic_merge_allowed,
            }
            for group in sorted(audit.duplicate_groups, key=lambda item: item.group_id)
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _available_display_name_sources(
    facts: SubscriberIdentityFacts,
) -> tuple[str, ...]:
    sources: list[str] = []
    if f"{facts.first_name} {facts.last_name}".strip():
        sources.append("subscriber_full_name")
    if (facts.display_name or "").strip():
        sources.append("subscriber_display_name")
    if (facts.company_name or "").strip():
        sources.append("company_name")
    if (facts.legal_name or "").strip():
        sources.append("legal_name")
    return tuple(sources)


def _enum_value(value: object | None) -> str:
    if value is None:
        return ""
    raw = getattr(value, "value", value)
    return str(raw).strip().lower()


def normalize_email(value: str | None) -> str | None:
    normalized = (value or "").strip().casefold()
    if not normalized or "@" not in normalized:
        return None
    return normalized


def normalize_phone(value: str | None) -> str | None:
    digits = "".join(character for character in (value or "") if character.isdigit())
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("0"):
        digits = f"234{digits[1:]}"
    elif len(digits) == 10:
        digits = f"234{digits}"
    return digits if len(digits) >= 7 else None


def normalize_nin(value: str | None) -> str | None:
    digits = "".join(character for character in (value or "") if character.isdigit())
    return digits if len(digits) == 11 else None


def normalize_name(*values: str | None) -> str | None:
    text = " ".join((value or "").strip() for value in values if value)
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    normalized = _NON_ALNUM_RE.sub(" ", folded.casefold()).strip()
    return normalized or None


def _metadata_flag(metadata: dict[str, Any], *keys: str) -> bool:
    return any(metadata.get(key) is True for key in keys)


def classify_record(
    facts: SubscriberIdentityFacts,
) -> tuple[RecordClassification, tuple[str, ...]]:
    metadata = facts.metadata if isinstance(facts.metadata, dict) else {}
    declared_value = _enum_value(
        metadata.get("data_classification")
        or metadata.get("environment")
        or metadata.get("record_classification")
    )
    if _metadata_flag(metadata, "identity_quarantined", "party_quarantined") or (
        declared_value == "quarantined"
    ):
        return RecordClassification.already_quarantined, (
            "metadata_declares_quarantine",
        )
    if _metadata_flag(metadata, "is_test", "test_data", "is_demo") or (
        declared_value in _NONPRODUCTION_VALUES
    ):
        return RecordClassification.declared_nonproduction, (
            "metadata_declares_nonproduction",
        )

    email = normalize_email(facts.email)
    email_domain = email.rsplit("@", 1)[1] if email else ""
    name_text = " ".join(
        value
        for value in (
            facts.first_name,
            facts.last_name,
            facts.display_name,
            facts.company_name,
        )
        if value
    ).casefold()
    reasons: list[str] = []
    if email_domain in _TEST_EMAIL_DOMAINS or email_domain.endswith(".test"):
        reasons.append("test_email_domain_pattern")
    if _TEST_TOKEN_RE.search(name_text):
        reasons.append("test_name_pattern")
    if reasons:
        return RecordClassification.suspected_nonproduction, tuple(reasons)
    return RecordClassification.production_candidate, ()


def classify_lifecycle(
    facts: SubscriberIdentityFacts,
) -> tuple[LifecycleCohort, tuple[str, ...], tuple[str, ...]]:
    evidence: list[str] = []
    contradictions: list[str] = []
    party_status = _enum_value(facts.party_status)
    account_status = _enum_value(facts.account_status)

    if facts.has_active_subscription:
        cohort = LifecycleCohort.active_subscriber
        evidence.append("active_subscription")
        # Subscriber.is_active and recoverable account states such as blocked
        # are access/enforcement projections, not lifecycle evidence. Billing
        # may wall an account while its subscription and subscriber
        # relationship remain active. Only a terminal account status conflicts
        # with an active subscription.
        if account_status in _TERMINAL_ACCOUNT_STATUSES:
            contradictions.append("active_subscription_on_inactive_account")
        if party_status in {"lead", "contact", "customer"}:
            contradictions.append("party_status_lags_active_subscription")
    elif facts.has_any_subscription:
        cohort = LifecycleCohort.inactive_subscriber
        evidence.append("historical_or_inactive_subscription")
    elif (
        facts.has_customer_sales_order
        or facts.has_service_order
        or facts.has_accepted_quote
        or facts.has_succeeded_payment
        or facts.has_billing_document
        or party_status in {"customer", "subscriber"}
    ):
        cohort = LifecycleCohort.customer
        evidence.extend(
            signal
            for present, signal in (
                (facts.has_customer_sales_order, "confirmed_sales_order"),
                (facts.has_service_order, "service_order"),
                (facts.has_accepted_quote, "accepted_quote"),
                (facts.has_succeeded_payment, "succeeded_payment"),
                (facts.has_billing_document, "billing_document"),
                (party_status in {"customer", "subscriber"}, "declared_party_status"),
            )
            if present
        )
        if party_status == "subscriber":
            contradictions.append("subscriber_party_status_without_subscription")
    elif (
        facts.has_lead
        or facts.has_quote
        or facts.has_sales_order
        or party_status == "lead"
    ):
        cohort = LifecycleCohort.lead
        evidence.extend(
            signal
            for present, signal in (
                (facts.has_lead, "lead"),
                (facts.has_quote, "quote"),
                (facts.has_sales_order, "unconfirmed_sales_order"),
                (party_status == "lead", "declared_party_status"),
            )
            if present
        )
    elif (
        facts.email_verified
        or facts.verified_nin
        or facts.verified_contact_methods
        or party_status == "contact"
    ):
        cohort = LifecycleCohort.verified_contact
        evidence.extend(sorted(facts.verified_contact_methods))
        if facts.email_verified:
            evidence.append("verified_primary_email")
        if facts.verified_nin:
            evidence.append("verified_nin")
        if party_status == "contact":
            evidence.append("declared_party_status")
    else:
        cohort = LifecycleCohort.unverified_record
        evidence.append("no_verified_lifecycle_evidence")

    if facts.has_support_history:
        evidence.append("support_history")

    if party_status in {"lead", "contact"} and cohort in {
        LifecycleCohort.active_subscriber,
        LifecycleCohort.inactive_subscriber,
        LifecycleCohort.customer,
    }:
        contradictions.append("commercial_evidence_exceeds_party_status")
    return cohort, tuple(dict.fromkeys(evidence)), tuple(dict.fromkeys(contradictions))


def _group_id(evidence_type: str, subscriber_ids: tuple[UUID, ...]) -> str:
    payload = f"{evidence_type}:" + ",".join(str(value) for value in subscriber_ids)
    return hashlib.sha256(payload.encode()).hexdigest()[:20]


def build_duplicate_candidate_groups(
    facts: tuple[SubscriberIdentityFacts, ...],
) -> tuple[DuplicateCandidateGroup, ...]:
    buckets: dict[tuple[str, DuplicateConfidence, str], set[UUID]] = defaultdict(set)
    for item in facts:
        email = normalize_email(item.email)
        phone = normalize_phone(item.phone)
        nin = normalize_nin(item.nin) if item.verified_nin else None
        name = normalize_name(item.first_name, item.last_name)
        if nin:
            buckets[("verified_nin_exact", DuplicateConfidence.high, nin)].add(
                item.subscriber_id
            )
        if email and phone:
            buckets[
                ("email_phone_exact", DuplicateConfidence.medium, f"{email}\0{phone}")
            ].add(item.subscriber_id)
        if name and phone:
            buckets[
                ("name_phone_exact", DuplicateConfidence.medium, f"{name}\0{phone}")
            ].add(item.subscriber_id)
        if email:
            buckets[("email_exact", DuplicateConfidence.weak, email)].add(
                item.subscriber_id
            )
        if phone:
            buckets[("phone_exact", DuplicateConfidence.weak, phone)].add(
                item.subscriber_id
            )

    groups: list[DuplicateCandidateGroup] = []
    for (evidence_type, confidence, _raw_key), members in buckets.items():
        if len(members) < 2:
            continue
        subscriber_ids = tuple(sorted(members, key=str))
        groups.append(
            DuplicateCandidateGroup(
                group_id=_group_id(evidence_type, subscriber_ids),
                evidence_type=evidence_type,
                confidence=confidence,
                subscriber_ids=subscriber_ids,
            )
        )
    return tuple(
        sorted(
            groups,
            key=lambda group: (
                -_CONFIDENCE_RANK[group.confidence],
                group.evidence_type,
                group.group_id,
            ),
        )
    )


def _disposition(
    row: SubscriberAuditRow,
) -> ReviewDisposition:
    if row.record_classification is RecordClassification.already_quarantined:
        return ReviewDisposition.retain_quarantine
    if row.record_classification is RecordClassification.declared_nonproduction:
        return ReviewDisposition.review_and_quarantine_nonproduction
    if row.record_classification is RecordClassification.suspected_nonproduction:
        return ReviewDisposition.manual_nonproduction_review
    if row.strongest_duplicate_confidence in {
        DuplicateConfidence.high,
        DuplicateConfidence.medium,
    }:
        return ReviewDisposition.manual_duplicate_review
    if row.contradictions:
        return ReviewDisposition.manual_lifecycle_review
    if row.lifecycle_cohort is LifecycleCohort.unverified_record:
        return ReviewDisposition.quarantine_unverified
    return ReviewDisposition.ready_for_party_backfill


def resolve_subscriber_identity_audit(
    facts: tuple[SubscriberIdentityFacts, ...],
    *,
    generated_at: datetime | None = None,
    missing_optional_sources: tuple[str, ...] = (),
) -> SubscriberIdentityAudit:
    groups = build_duplicate_candidate_groups(facts)
    groups_by_subscriber: dict[UUID, list[DuplicateCandidateGroup]] = defaultdict(list)
    for group in groups:
        for subscriber_id in group.subscriber_ids:
            groups_by_subscriber[subscriber_id].append(group)

    rows: list[SubscriberAuditRow] = []
    for item in sorted(facts, key=lambda value: str(value.subscriber_id)):
        cohort, lifecycle_evidence, contradictions = classify_lifecycle(item)
        classification, classification_evidence = classify_record(item)
        duplicate_groups = groups_by_subscriber.get(item.subscriber_id, [])
        strongest = max(
            (group.confidence for group in duplicate_groups),
            key=lambda value: _CONFIDENCE_RANK[value],
            default=None,
        )
        row = SubscriberAuditRow(
            subscriber_id=item.subscriber_id,
            lifecycle_cohort=cohort,
            record_classification=classification,
            recommended_disposition=ReviewDisposition.ready_for_party_backfill,
            lifecycle_evidence=lifecycle_evidence,
            classification_evidence=classification_evidence,
            contradictions=contradictions,
            duplicate_group_ids=tuple(group.group_id for group in duplicate_groups),
            strongest_duplicate_confidence=strongest,
            existing_party_id=item.party_id,
            available_display_name_sources=_available_display_name_sources(item),
        )
        rows.append(replace(row, recommended_disposition=_disposition(row)))
    return SubscriberIdentityAudit(
        rows=tuple(rows),
        duplicate_groups=groups,
        generated_at=generated_at,
        missing_optional_sources=missing_optional_sources,
    )


def _id_set(rows) -> set[UUID]:
    return {row[0] for row in rows if row[0] is not None}


def collect_subscriber_identity_facts(
    db: Session,
) -> tuple[SubscriberIdentityFacts, ...]:
    """Collect native Sub facts in bounded bulk queries; never mutate them."""

    subscribers = db.query(Subscriber).order_by(Subscriber.id).all()
    any_subscriptions = _id_set(db.query(Subscription.subscriber_id).distinct().all())
    active_subscriptions = _id_set(
        db.query(Subscription.subscriber_id)
        .filter(Subscription.status == SubscriptionStatus.active)
        .distinct()
        .all()
    )
    leads = _id_set(db.query(Lead.subscriber_id).distinct().all())
    quotes = _id_set(db.query(Quote.subscriber_id).distinct().all())
    accepted_quotes = _id_set(
        db.query(Quote.subscriber_id)
        .filter(Quote.status == QuoteStatus.accepted.value)
        .distinct()
        .all()
    )
    sales_orders = _id_set(db.query(SalesOrder.subscriber_id).distinct().all())
    customer_sales_orders = _id_set(
        db.query(SalesOrder.subscriber_id)
        .filter(
            SalesOrder.status.in_(
                (
                    SalesOrderStatus.confirmed.value,
                    SalesOrderStatus.paid.value,
                    SalesOrderStatus.fulfilled.value,
                )
            )
        )
        .distinct()
        .all()
    )
    service_orders = _id_set(db.query(ServiceOrder.subscriber_id).distinct().all())
    billing_documents = _id_set(
        db.query(Invoice.account_id)
        .filter(
            Invoice.is_active.is_(True),
            Invoice.status.in_(
                (
                    InvoiceStatus.issued,
                    InvoiceStatus.partially_paid,
                    InvoiceStatus.paid,
                    InvoiceStatus.overdue,
                    InvoiceStatus.written_off,
                )
            ),
        )
        .distinct()
        .all()
    )
    succeeded_payments = _id_set(
        db.query(Payment.account_id)
        .filter(
            Payment.status.in_(
                (
                    PaymentStatus.succeeded,
                    PaymentStatus.refunded,
                    PaymentStatus.partially_refunded,
                )
            )
        )
        .distinct()
        .all()
    )
    support_history = _id_set(
        db.query(Ticket.subscriber_id).filter(Ticket.subscriber_id.is_not(None)).all()
    )
    support_history.update(
        _id_set(
            db.query(Ticket.customer_account_id)
            .filter(Ticket.customer_account_id.is_not(None))
            .all()
        )
    )
    support_history.update(
        _id_set(
            db.query(Ticket.customer_person_id)
            .filter(Ticket.customer_person_id.is_not(None))
            .all()
        )
    )

    verified_nin_by_subscriber: dict[UUID, set[str]] = defaultdict(set)
    for subscriber_id, nin in (
        db.query(
            SubscriberNINVerification.subscriber_id,
            SubscriberNINVerification.nin,
        )
        .filter(SubscriberNINVerification.status == NINVerificationStatus.success)
        .all()
    ):
        normalized = normalize_nin(nin)
        if normalized:
            verified_nin_by_subscriber[subscriber_id].add(normalized)

    verified_methods: dict[UUID, set[str]] = defaultdict(set)
    for subscriber_id, channel_type in (
        db.query(SubscriberChannel.subscriber_id, SubscriberChannel.channel_type)
        .filter(SubscriberChannel.is_verified.is_(True))
        .all()
    ):
        verified_methods[subscriber_id].add(_enum_value(channel_type))

    latest_field_verifications: dict[tuple[UUID, str], str | None] = {}
    available_tables = set(inspect(db.get_bind()).get_table_names())
    if "subscriber_field_verifications" in available_tables:
        verification_rows = db.execute(
            text(
                "SELECT subscriber_id, field_key, value "
                "FROM subscriber_field_verifications "
                "ORDER BY verified_at DESC"
            )
        ).all()
        for subscriber_id, field_key, value in verification_rows:
            latest_field_verifications.setdefault((subscriber_id, field_key), value)

    facts: list[SubscriberIdentityFacts] = []
    for subscriber in subscribers:
        subscriber_id = subscriber.id
        current_nin = normalize_nin(subscriber.nin)
        nin_verified = bool(
            current_nin
            and current_nin in verified_nin_by_subscriber.get(subscriber_id, set())
        )
        current_email = normalize_email(subscriber.email)
        verified_email = normalize_email(
            latest_field_verifications.get((subscriber_id, "email"))
        )
        current_phone = normalize_phone(subscriber.phone)
        verified_phone = normalize_phone(
            latest_field_verifications.get((subscriber_id, "phone"))
        )
        methods = set(verified_methods.get(subscriber_id, set()))
        if current_email and current_email == verified_email:
            methods.add("verified_field_email")
        if current_phone and current_phone == verified_phone:
            methods.add("verified_field_phone")
        facts.append(
            SubscriberIdentityFacts(
                subscriber_id=subscriber_id,
                first_name=subscriber.first_name,
                last_name=subscriber.last_name,
                display_name=subscriber.display_name,
                company_name=subscriber.company_name,
                legal_name=subscriber.legal_name,
                email=subscriber.email,
                phone=subscriber.phone,
                nin=subscriber.nin,
                email_verified=bool(subscriber.email_verified),
                verified_nin=nin_verified,
                verified_contact_methods=frozenset(methods),
                account_status=_enum_value(subscriber.status),
                is_active=bool(subscriber.is_active),
                party_status=subscriber.party_status,
                metadata=(
                    dict(subscriber.metadata_)
                    if isinstance(subscriber.metadata_, dict)
                    else None
                ),
                has_lead=subscriber_id in leads,
                has_quote=subscriber_id in quotes,
                has_accepted_quote=subscriber_id in accepted_quotes,
                has_sales_order=subscriber_id in sales_orders,
                has_customer_sales_order=subscriber_id in customer_sales_orders,
                has_service_order=subscriber_id in service_orders,
                has_any_subscription=subscriber_id in any_subscriptions,
                has_active_subscription=subscriber_id in active_subscriptions,
                has_billing_document=subscriber_id in billing_documents,
                has_succeeded_payment=subscriber_id in succeeded_payments,
                has_support_history=subscriber_id in support_history,
                party_id=subscriber.party_id,
            )
        )
    return tuple(facts)


def build_subscriber_identity_audit(db: Session) -> SubscriberIdentityAudit:
    available_tables = set(inspect(db.get_bind()).get_table_names())
    generated_at = datetime.now(UTC)
    if db.get_bind().dialect.name == "postgresql":
        timestamp_result = db.execute(text("SELECT transaction_timestamp()"))
        generated_at = timestamp_result.scalar_one()
    missing_optional_sources = tuple(
        table_name
        for table_name in ("subscriber_field_verifications",)
        if table_name not in available_tables
    )
    return resolve_subscriber_identity_audit(
        collect_subscriber_identity_facts(db),
        generated_at=generated_at,
        missing_optional_sources=missing_optional_sources,
    )
