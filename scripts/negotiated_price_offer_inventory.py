#!/usr/bin/env python3
"""Read-only inventory of duplicate and negotiated-price catalog offers (ledger F1).

Ledger F1 (Starter
``docs/superpowers/plans/2026-08-23-catalog-variant-decomposition.md``) requires a
read-only inventory of dedicated/customer-named offers, their subscriptions,
currencies, billing periods, discounts and every price consumer before any
negotiated-price cutover. Sub's own cutover design
(``docs/designs/NEGOTIATED_PRICE_CONTRACT_LINE_MIGRATION.md``) records that the
checked-in four-name/41-row snapshot is historical evidence, not current fact,
and that a fresh inventory from an explicitly named target remains the gate.

Three properties are structural, not conventions:

**It reports, it does not classify.** The eight adjudication classes in the
migration design (``catalog_price_equal`` ... ``invalid_currency_or_period``) are
decisions owned by the migration, and an operator adjudicates them. This script
emits evidence and *signals*. A "customer-like name" is a signal with its matched
tokens attached, never a verdict, and an offer name is never treated as an
identity or a merge key.

**It cannot write.** Every statement is compiled and asserted to begin with
``SELECT`` before it is executed (:func:`assert_select_only`), and the session is
pinned to one ``REPEATABLE READ, READ ONLY`` snapshot through the repository's
single read-only seam, ``app.db.READ_ONLY_SNAPSHOT_OPTIONS``. Fixing the isolation
level while dropping read-only would leave a report that still looks correct and
can now change the database it measures.

**It has no default target.** ``--database-url`` or ``SUB_INVENTORY_DATABASE_URL``,
and nothing else. It deliberately does NOT fall back to the ambient application
``DATABASE_URL``: a report whose target can be inherited from the environment is a
report that eventually runs against production because someone forgot a flag.
Naming the target is the operator's explicit act.

Output is sorted JSON on stdout, so two runs over one population are
byte-identical and two snapshots diff cleanly.
"""

from __future__ import annotations

import argparse
import enum
import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Select, create_engine, func, select
from sqlalchemy.orm import Session

from app.db import READ_ONLY_SNAPSHOT_OPTIONS
from app.models.billing import Invoice, InvoiceLine
from app.models.billing_contract import BillingContractLine, BillingContractVersion
from app.models.catalog import (
    CatalogOffer,
    OfferPrice,
    OfferVersion,
    OfferVersionPrice,
    Subscription,
)
from app.models.subscriber import Subscriber

#: The environment variable that may name the target. There is no default and no
#: fallback to the application's own ``DATABASE_URL``.
TARGET_ENV_VAR = "SUB_INVENTORY_DATABASE_URL"

#: How far back issued invoices are sampled for the price-consumer evidence.
DEFAULT_INVOICE_WINDOW_DAYS = 400

#: Tokens that describe the product rather than a customer. A name token outside
#: this vocabulary is what makes a name *look* customer-like; it is a signal for
#: an operator, not a classification, which is why the matched tokens travel with
#: the signal instead of being collapsed into a boolean.
TECHNICAL_NAME_TOKENS = frozenset(
    {
        "1g",
        "10g",
        "access",
        "add",
        "addon",
        "annual",
        "basic",
        "block",
        "broadband",
        "bundle",
        "business",
        "cable",
        "capacity",
        "circuit",
        "cir",
        "corporate",
        "data",
        "dedicated",
        "dsl",
        "enterprise",
        "essential",
        "fiber",
        "fibre",
        "fixed",
        "flex",
        "fup",
        "gb",
        "gbps",
        "home",
        "hsd",
        "install",
        "installation",
        "internet",
        "ip",
        "kbps",
        "layer2",
        "lite",
        "mb",
        "mbps",
        "metro",
        "monthly",
        "on",
        "one",
        "onu",
        "ont",
        "package",
        "per",
        "plan",
        "plus",
        "premium",
        "prepaid",
        "postpaid",
        "pro",
        "quarterly",
        "radio",
        "recurring",
        "residential",
        "router",
        "service",
        "shared",
        "sme",
        "speed",
        "standard",
        "starter",
        "static",
        "stm",
        "tariff",
        "time",
        "transit",
        "unlimited",
        "up",
        "upgrade",
        "vat",
        "wireless",
        "with",
        "without",
        "yearly",
        "zone",
    }
)

#: Minimum token length considered when matching an offer name against a real
#: subscriber name. Shorter fragments produce noise, not evidence.
MIN_NAME_TOKEN_LENGTH = 4

_TOKEN_SPLIT = re.compile(r"[^0-9a-z]+")


class InventoryTargetError(RuntimeError):
    """The operator did not name exactly one explicit target."""


@dataclass(frozen=True, slots=True)
class InventoryOptions:
    """Everything the report needs, resolved before a connection is opened."""

    database_url: str
    invoice_window_days: int


def resolve_target(
    *,
    argument: str | None,
    environ: Mapping[str, str],
) -> str:
    """Return the one explicitly named target, or refuse.

    An absent target is a refusal, never a default. An argument and an
    environment variable that disagree is also a refusal: silently preferring one
    is how a report ends up measuring a database nobody named.
    """

    from_argument = (argument or "").strip()
    from_environment = (environ.get(TARGET_ENV_VAR) or "").strip()

    if from_argument and from_environment and from_argument != from_environment:
        raise InventoryTargetError(
            "--database-url and "
            f"{TARGET_ENV_VAR} name different targets; supply exactly one"
        )
    target = from_argument or from_environment
    if not target:
        raise InventoryTargetError(
            "no target database was named; pass --database-url or set "
            f"{TARGET_ENV_VAR}. This report has no default target on purpose."
        )
    return target


def assert_select_only(statement: Select[Any]) -> Select[Any]:
    """Return the statement after proving it compiles to a bare ``SELECT``.

    The read-only session already refuses writes at the connection level. This is
    the second, independent rail: it fails in a unit test, without a database,
    the moment a statement stops being a read.
    """

    compiled = str(statement.compile(compile_kwargs={"literal_binds": False}))
    if not compiled.lstrip().upper().startswith("SELECT"):
        raise AssertionError(f"inventory statement is not a SELECT: {compiled[:120]}")
    return statement


def offer_query() -> Select[Any]:
    """Every offer with its sellable, technical and legacy-VAT shape."""

    return assert_select_only(
        select(
            CatalogOffer.id,
            CatalogOffer.name,
            CatalogOffer.code,
            CatalogOffer.service_type,
            CatalogOffer.access_type,
            CatalogOffer.plan_category,
            CatalogOffer.plan_family,
            CatalogOffer.price_basis,
            CatalogOffer.billing_cycle,
            CatalogOffer.billing_mode,
            CatalogOffer.contract_term,
            CatalogOffer.prepaid_period,
            CatalogOffer.speed_download_mbps,
            CatalogOffer.speed_upload_mbps,
            CatalogOffer.aggregation,
            CatalogOffer.with_vat,
            CatalogOffer.vat_percent,
            CatalogOffer.status,
            CatalogOffer.is_active,
            CatalogOffer.available_for_services,
            CatalogOffer.region_zone_id,
        ).order_by(CatalogOffer.name, CatalogOffer.id)
    )


def offer_price_query() -> Select[Any]:
    """Catalog prices held directly on the offer."""

    return assert_select_only(
        select(
            OfferPrice.offer_id,
            OfferPrice.price_type,
            OfferPrice.amount,
            OfferPrice.currency,
            OfferPrice.billing_cycle,
            OfferPrice.unit,
            OfferPrice.is_active,
        ).order_by(OfferPrice.offer_id, OfferPrice.price_type, OfferPrice.amount)
    )


def offer_version_price_query() -> Select[Any]:
    """Catalog prices held on a published offer version."""

    return assert_select_only(
        select(
            OfferVersion.offer_id,
            OfferVersion.version_number,
            OfferVersionPrice.price_type,
            OfferVersionPrice.amount,
            OfferVersionPrice.currency,
            OfferVersionPrice.billing_cycle,
            OfferVersionPrice.is_active,
        )
        .join(OfferVersion, OfferVersion.id == OfferVersionPrice.offer_version_id)
        .order_by(
            OfferVersion.offer_id,
            OfferVersion.version_number,
            OfferVersionPrice.price_type,
        )
    )


def subscription_count_query() -> Select[Any]:
    """Subscription counts per offer and status."""

    return assert_select_only(
        select(
            Subscription.offer_id,
            Subscription.status,
            func.count().label("subscription_count"),
        )
        .group_by(Subscription.offer_id, Subscription.status)
        .order_by(Subscription.offer_id, Subscription.status)
    )


def subscription_price_query() -> Select[Any]:
    """Distinct subscription-level amounts, currencies and cadences per offer.

    ``Subscription`` carries no currency column, so the cadence comes from the
    subscription and the currency can only come from a price consumer that has
    one. That asymmetry is reported rather than papered over.
    """

    return assert_select_only(
        select(
            Subscription.offer_id,
            Subscription.unit_price,
            Subscription.billing_cycle,
            Subscription.billing_mode,
            func.count().label("subscription_count"),
        )
        .group_by(
            Subscription.offer_id,
            Subscription.unit_price,
            Subscription.billing_cycle,
            Subscription.billing_mode,
        )
        .order_by(Subscription.offer_id, Subscription.unit_price)
    )


def subscription_discount_query() -> Select[Any]:
    """Active and historical subscription-level discounts per offer."""

    return assert_select_only(
        select(
            Subscription.offer_id,
            Subscription.discount,
            Subscription.discount_type,
            Subscription.discount_value,
            func.count().label("subscription_count"),
            func.min(Subscription.discount_start_at).label("earliest_start_at"),
            func.max(Subscription.discount_end_at).label("latest_end_at"),
        )
        .where(
            Subscription.discount.is_(True)
            | Subscription.discount_value.isnot(None)
            | Subscription.discount_type.isnot(None)
        )
        .group_by(
            Subscription.offer_id,
            Subscription.discount,
            Subscription.discount_type,
            Subscription.discount_value,
        )
        .order_by(Subscription.offer_id, Subscription.discount_type)
    )


def invoice_line_price_query(
    *,
    window_days: int,
    now: datetime | None = None,
) -> Select[Any]:
    """Issued invoice-line price snapshots per offer, over a bounded window.

    Issued document evidence is immutable and is the only consumer here that
    proves what a customer was actually charged. The cutoff is a bound parameter
    computed here rather than a dialect-specific interval expression, so the
    statement compiles and can be asserted without a database.
    """

    cutoff = (now or datetime.now(UTC)) - timedelta(days=window_days)
    return assert_select_only(
        select(
            Subscription.offer_id,
            InvoiceLine.unit_price,
            Invoice.currency,
            func.count().label("line_count"),
            func.min(Invoice.issued_at).label("earliest_issued_at"),
            func.max(Invoice.issued_at).label("latest_issued_at"),
        )
        .join(Subscription, Subscription.id == InvoiceLine.subscription_id)
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .where(Invoice.issued_at.isnot(None))
        .where(Invoice.issued_at >= cutoff)
        .where(InvoiceLine.is_active.is_(True))
        .group_by(Subscription.offer_id, InvoiceLine.unit_price, Invoice.currency)
        .order_by(Subscription.offer_id, InvoiceLine.unit_price)
    )


def contract_line_price_query() -> Select[Any]:
    """Sub-local billing-contract line prices per offer."""

    return assert_select_only(
        select(
            Subscription.offer_id,
            BillingContractLine.charge_component,
            BillingContractLine.unit_price,
            BillingContractLine.currency,
            BillingContractLine.tax_treatment_code,
            func.count().label("line_count"),
        )
        .join(
            BillingContractVersion,
            BillingContractVersion.id == BillingContractLine.contract_version_id,
        )
        .join(Subscription, Subscription.id == BillingContractVersion.subscription_id)
        .group_by(
            Subscription.offer_id,
            BillingContractLine.charge_component,
            BillingContractLine.unit_price,
            BillingContractLine.currency,
            BillingContractLine.tax_treatment_code,
        )
        .order_by(Subscription.offer_id, BillingContractLine.unit_price)
    )


def party_name_query() -> Select[Any]:
    """Real customer names, for evidence-based name matching.

    ``Subscriber.category`` is a JSONB-backed property rather than a column, so it
    cannot be selected here; only the stored name columns are usable evidence.
    """

    return assert_select_only(
        select(
            func.lower(Subscriber.company_name).label("company_name"),
            func.lower(Subscriber.legal_name).label("legal_name"),
            func.lower(Subscriber.last_name).label("last_name"),
        )
        .where(
            Subscriber.company_name.isnot(None)
            | Subscriber.legal_name.isnot(None)
            | Subscriber.last_name.isnot(None)
        )
        .distinct()
    )


ALL_QUERY_BUILDERS = (
    offer_query,
    offer_price_query,
    offer_version_price_query,
    subscription_count_query,
    subscription_price_query,
    subscription_discount_query,
    contract_line_price_query,
    party_name_query,
)


def tokenize(value: str | None) -> tuple[str, ...]:
    """Split a name into lowercase alphanumeric tokens."""

    if not value:
        return ()
    return tuple(token for token in _TOKEN_SPLIT.split(value.lower()) if token)


def non_technical_tokens(name: str | None) -> tuple[str, ...]:
    """Name tokens that describe neither speed nor product vocabulary."""

    return tuple(
        token
        for token in tokenize(name)
        if token not in TECHNICAL_NAME_TOKENS
        and not token.isdigit()
        and not re.fullmatch(r"\d+[a-z]*", token)
    )


def party_name_tokens(rows: Iterable[Mapping[str, Any]]) -> frozenset[str]:
    """Long-enough tokens drawn from real stored customer names."""

    tokens: set[str] = set()
    for row in rows:
        for column in ("company_name", "legal_name", "last_name"):
            for token in tokenize(row.get(column)):
                if len(token) >= MIN_NAME_TOKEN_LENGTH:
                    tokens.add(token)
    return frozenset(tokens)


def customer_like_signal(
    *,
    offer_name: str,
    known_party_tokens: frozenset[str],
) -> dict[str, Any]:
    """Two independent signals that an offer name encodes a customer.

    ``unmatched_tokens`` is a heuristic over vocabulary. ``party_name_matches`` is
    evidence: the token also appears in a stored customer name. Both are reported
    with their tokens so an operator can see exactly why the row surfaced.
    """

    unmatched = non_technical_tokens(offer_name)
    matches = tuple(
        sorted(
            token
            for token in unmatched
            if len(token) >= MIN_NAME_TOKEN_LENGTH and token in known_party_tokens
        )
    )
    return {
        "unmatched_tokens": list(unmatched),
        "party_name_matches": list(matches),
        "has_signal": bool(unmatched) or bool(matches),
    }


def group_key(row: Mapping[str, Any]) -> tuple[str, int | None, int | None, str | None]:
    """The (name, speed, plan_family) grouping ledger F1 asks for."""

    return (
        (row.get("name") or "").strip().lower(),
        row.get("speed_download_mbps"),
        row.get("speed_upload_mbps"),
        (row.get("plan_family") or None),
    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, enum.Enum):
        return value.value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _rows(db: Session, statement: Select[Any]) -> tuple[dict[str, Any], ...]:
    return tuple(
        {key: _json_safe(value) for key, value in row._mapping.items()}
        for row in db.execute(assert_select_only(statement)).all()
    )


def build_report(db: Session, *, options: InventoryOptions) -> dict[str, Any]:
    """Assemble the whole inventory from one read-only snapshot."""

    offers = _rows(db, offer_query())
    offer_prices = _rows(db, offer_price_query())
    version_prices = _rows(db, offer_version_price_query())
    counts = _rows(db, subscription_count_query())
    subscription_prices = _rows(db, subscription_price_query())
    discounts = _rows(db, subscription_discount_query())
    invoice_prices = _rows(
        db, invoice_line_price_query(window_days=options.invoice_window_days)
    )
    contract_prices = _rows(db, contract_line_price_query())
    known_tokens = party_name_tokens(_rows(db, party_name_query()))

    by_offer: dict[str, dict[str, Any]] = {}
    for offer in offers:
        offer_id = offer["id"]
        by_offer[offer_id] = {
            "offer": offer,
            "customer_like_signal": customer_like_signal(
                offer_name=offer.get("name") or "",
                known_party_tokens=known_tokens,
            ),
            "catalog_prices": [],
            "offer_version_prices": [],
            "subscription_counts": [],
            "subscription_prices": [],
            "subscription_discounts": [],
            "invoice_line_prices": [],
            "contract_line_prices": [],
        }

    def _attach(bucket: str, rows: Sequence[Mapping[str, Any]], key: str) -> None:
        for row in rows:
            entry = by_offer.get(row.get(key))
            if entry is not None:
                entry[bucket].append(dict(row))

    _attach("catalog_prices", offer_prices, "offer_id")
    _attach("offer_version_prices", version_prices, "offer_id")
    _attach("subscription_counts", counts, "offer_id")
    _attach("subscription_prices", subscription_prices, "offer_id")
    _attach("subscription_discounts", discounts, "offer_id")
    _attach("invoice_line_prices", invoice_prices, "offer_id")
    _attach("contract_line_prices", contract_prices, "offer_id")

    for entry in by_offer.values():
        entry["subscription_total"] = sum(
            int(row["subscription_count"]) for row in entry["subscription_counts"]
        )
        entry["price_consumers"] = sorted(
            bucket
            for bucket in (
                "catalog_prices",
                "offer_version_prices",
                "subscription_prices",
                "invoice_line_prices",
                "contract_line_prices",
            )
            if entry[bucket]
        )
        entry["zero_or_absent_catalog_price"] = not [
            row
            for row in entry["catalog_prices"]
            if row.get("is_active") and Decimal(str(row.get("amount") or "0")) > 0
        ]

    duplicate_groups: dict[str, list[str]] = {}
    for offer in offers:
        key = "|".join("" if part is None else str(part) for part in group_key(offer))
        duplicate_groups.setdefault(key, []).append(offer["id"])

    sellable_names: dict[str, list[str]] = {}
    for offer in offers:
        if not (offer.get("is_active") and offer.get("available_for_services")):
            continue
        name = (offer.get("name") or "").strip().lower()
        sellable_names.setdefault(name, []).append(offer["id"])

    return {
        "report": "negotiated_price_offer_inventory",
        "report_version": 1,
        "invoice_window_days": options.invoice_window_days,
        "offer_count": len(offers),
        "offers": [by_offer[key] for key in sorted(by_offer)],
        "duplicate_name_speed_family_groups": {
            key: sorted(ids)
            for key, ids in sorted(duplicate_groups.items())
            if len(ids) > 1
        },
        "duplicate_sellable_names": {
            name: sorted(ids)
            for name, ids in sorted(sellable_names.items())
            if len(ids) > 1
        },
        "customer_like_offer_ids": sorted(
            offer_id
            for offer_id, entry in by_offer.items()
            if entry["customer_like_signal"]["has_signal"]
        ),
        "zero_or_absent_catalog_price_offer_ids": sorted(
            offer_id
            for offer_id, entry in by_offer.items()
            if entry["zero_or_absent_catalog_price"]
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=None,
        help=(
            "explicit target database URL; may also be supplied through "
            f"{TARGET_ENV_VAR}. There is no default."
        ),
    )
    parser.add_argument(
        "--invoice-window-days",
        type=int,
        default=DEFAULT_INVOICE_WINDOW_DAYS,
        help="how far back issued invoice lines are sampled",
    )
    return parser


def build_options(
    argv: Sequence[str] | None,
    *,
    environ: Mapping[str, str] | None = None,
) -> InventoryOptions:
    args = build_parser().parse_args(argv)
    if args.invoice_window_days <= 0:
        raise InventoryTargetError("--invoice-window-days must be positive")
    return InventoryOptions(
        database_url=resolve_target(
            argument=args.database_url,
            environ=os.environ if environ is None else environ,
        ),
        invoice_window_days=args.invoice_window_days,
    )


def main(argv: Sequence[str] | None = None) -> int:
    try:
        options = build_options(argv)
    except InventoryTargetError as error:
        print(json.dumps({"error": str(error)}, sort_keys=True))
        return 2

    engine = create_engine(
        options.database_url,
        execution_options=dict(READ_ONLY_SNAPSHOT_OPTIONS),
        pool_pre_ping=True,
    )
    try:
        with Session(engine) as db:
            report = build_report(db, options=options)
            db.rollback()
    finally:
        engine.dispose()

    print(json.dumps(report, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
