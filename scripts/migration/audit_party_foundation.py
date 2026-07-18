#!/usr/bin/env python3
"""Read-only installation and population audit for the Party foundation."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, inspect
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.party import (
    PartnerRoleKey,
    Party,
    PartyContactPoint,
    PartyExternalReference,
    PartyIdentityBackfillReceipt,
    PartyMembership,
    PartyRelationship,
    PartyRole,
    PartyRoleType,
)
from app.models.subscriber import Subscriber
from app.services import party

_FOUNDATION_TABLES = {
    "parties",
    "party_roles",
    "party_relationships",
    "party_memberships",
    "party_contact_points",
    "party_external_references",
}
_SUBSCRIBER_BINDING_COLUMNS = {
    "party_id",
    "party_bound_at",
    "party_binding_source",
    "party_binding_reason",
}
_BACKFILL_RECEIPT_TABLE = "party_identity_backfill_receipts"


def _group_counts(db: Session, column) -> dict[str, int]:
    rows = db.query(column, func.count()).group_by(column).order_by(column).all()
    return {str(value): int(count) for value, count in rows}


def _role_contracts() -> dict[str, Any]:
    reseller = party.role_contract(PartyRoleType.reseller)
    partners = {
        key.value: list(
            party.role_contract(PartyRoleType.partner, key).capability_domains
        )
        for key in PartnerRoleKey
    }
    return {
        "reseller": {
            "capability_domains": list(reseller.capability_domains),
            "implicit_permissions": list(reseller.implicit_permissions),
        },
        "partners": partners,
        "partner_implicit_permissions": [],
    }


def build_party_foundation_audit(db: Session) -> dict[str, Any]:
    """Return read-only schema, population, and role-contract evidence."""

    installed_tables = set(inspect(db.get_bind()).get_table_names())
    missing_tables = sorted(_FOUNDATION_TABLES - installed_tables)
    if missing_tables:
        return {
            "status": "not_installed",
            "missing_tables": missing_tables,
            "role_contracts": _role_contracts(),
        }

    subscriber_columns = {
        column["name"] for column in inspect(db.get_bind()).get_columns("subscribers")
    }
    missing_binding_columns = sorted(_SUBSCRIBER_BINDING_COLUMNS - subscriber_columns)
    subscriber_binding: dict[str, Any]
    if missing_binding_columns:
        subscriber_binding = {
            "status": "not_installed",
            "missing_columns": missing_binding_columns,
        }
    else:
        total_accounts = db.query(func.count(Subscriber.id)).scalar() or 0
        bound_accounts = (
            db.query(func.count(Subscriber.id))
            .filter(Subscriber.party_id.is_not(None))
            .scalar()
            or 0
        )
        parties_with_accounts = (
            db.query(func.count(func.distinct(Subscriber.party_id)))
            .filter(Subscriber.party_id.is_not(None))
            .scalar()
            or 0
        )
        subscriber_binding = {
            "status": "installed",
            "total_accounts": int(total_accounts),
            "bound_accounts": int(bound_accounts),
            "unbound_accounts": int(total_accounts - bound_accounts),
            "parties_with_accounts": int(parties_with_accounts),
        }

    backfill_receipts: dict[str, Any]
    if _BACKFILL_RECEIPT_TABLE not in installed_tables:
        backfill_receipts = {"status": "not_installed"}
    else:
        backfill_receipts = {
            "status": "installed",
            "total": db.query(PartyIdentityBackfillReceipt).count(),
            "planned_parties": int(
                db.query(
                    func.coalesce(
                        func.sum(PartyIdentityBackfillReceipt.planned_party_count),
                        0,
                    )
                ).scalar()
                or 0
            ),
            "bindings": int(
                db.query(
                    func.coalesce(
                        func.sum(PartyIdentityBackfillReceipt.binding_count),
                        0,
                    )
                ).scalar()
                or 0
            ),
        }

    return {
        "status": "installed",
        "parties": {
            "total": db.query(Party).count(),
            "by_type": _group_counts(db, Party.party_type),
            "by_status": _group_counts(db, Party.status),
            "by_data_classification": _group_counts(db, Party.data_classification),
        },
        "roles": {
            "total": db.query(PartyRole).count(),
            "by_type": _group_counts(db, PartyRole.role_type),
            "by_status": _group_counts(db, PartyRole.status),
        },
        "relationships": db.query(PartyRelationship).count(),
        "memberships": db.query(PartyMembership).count(),
        "contact_points": db.query(PartyContactPoint).count(),
        "external_references": db.query(PartyExternalReference).count(),
        "subscriber_binding": subscriber_binding,
        "backfill_receipts": backfill_receipts,
        "role_contracts": _role_contracts(),
    }


def main() -> int:
    with SessionLocal() as db:
        result = build_party_foundation_audit(db)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "installed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
