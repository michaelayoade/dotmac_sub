"""Read-only convergence audit for organization business-role profiles.

The audit exposes only schema state and aggregate counts. It never copies
identity values, assigns roles, repairs the Vendor/FieldVendor bridge, binds a
profile, calls CRM, or changes a legacy read path.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, inspect
from sqlalchemy.orm import Session

from app.models.field_vendor import FieldVendor
from app.models.organization import Organization, OrganizationAccountType
from app.models.party import PartyRole, PartyRoleType
from app.models.subscriber import Reseller
from app.models.vendor_routes import Vendor

_BINDING_COLUMNS = {
    "party_id",
    "party_bound_at",
    "party_binding_source",
    "party_binding_reason",
}
_PROFILE_MODELS = {
    "organizations": Organization,
    "resellers": Reseller,
    "vendors": Vendor,
    "field_vendors": FieldVendor,
}


def _profile_counts(db: Session, model) -> dict[str, int]:
    total = int(db.query(func.count(model.id)).scalar() or 0)
    bound = int(
        db.query(func.count(model.id)).filter(model.party_id.is_not(None)).scalar() or 0
    )
    return {
        "total": total,
        "bound": bound,
        "unbound": total - bound,
    }


def _missing_role_count(
    db: Session,
    *,
    profile_model,
    role_type: PartyRoleType,
) -> int:
    bound_party_ids = {
        row[0]
        for row in db.query(profile_model.party_id)
        .filter(profile_model.party_id.is_not(None))
        .all()
    }
    if not bound_party_ids:
        return 0
    role_party_ids = {
        row[0]
        for row in db.query(PartyRole.party_id)
        .filter(
            PartyRole.party_id.in_(bound_party_ids),
            PartyRole.role_type == role_type.value,
        )
        .all()
    }
    return len(bound_party_ids - role_party_ids)


def _vendor_twin_counts(db: Session) -> dict[str, int]:
    vendors = db.query(Vendor.id, Vendor.party_id).all()
    field_vendors = db.query(
        FieldVendor.crm_vendor_id,
        FieldVendor.party_id,
    ).all()
    vendor_ids = {vendor_id for vendor_id, _party_id in vendors}
    exact_twins = {
        crm_vendor_id: party_id
        for crm_vendor_id, party_id in field_vendors
        if crm_vendor_id
    }
    counts = {
        "native_vendors": len(vendors),
        "field_vendors": len(field_vendors),
        "missing_field_vendor_twin": 0,
        "unbridged_field_vendor": 0,
        "invalid_uuid_bridge": 0,
        "orphan_uuid_bridge": 0,
        "unbound_pairs": 0,
        "aligned_bound_pairs": 0,
        "partial_party_binding": 0,
        "conflicting_party_binding": 0,
    }
    for crm_vendor_id, _party_id in field_vendors:
        if not crm_vendor_id:
            counts["unbridged_field_vendor"] += 1
            continue
        try:
            bridged_vendor_id = UUID(crm_vendor_id)
        except ValueError:
            counts["invalid_uuid_bridge"] += 1
            continue
        if bridged_vendor_id not in vendor_ids:
            counts["orphan_uuid_bridge"] += 1
    for vendor_id, vendor_party_id in vendors:
        bridge_key = str(vendor_id)
        if bridge_key not in exact_twins:
            counts["missing_field_vendor_twin"] += 1
            continue
        field_party_id = exact_twins[bridge_key]
        if vendor_party_id is None and field_party_id is None:
            counts["unbound_pairs"] += 1
        elif (vendor_party_id is None) != (field_party_id is None):
            counts["partial_party_binding"] += 1
        elif vendor_party_id == field_party_id:
            counts["aligned_bound_pairs"] += 1
        else:
            counts["conflicting_party_binding"] += 1
    return counts


def _concurrent_role_parties(db: Session) -> int:
    rows = (
        db.query(PartyRole.party_id)
        .filter(
            PartyRole.role_type.in_(
                (
                    PartyRoleType.reseller.value,
                    PartyRoleType.vendor.value,
                    PartyRoleType.partner.value,
                )
            )
        )
        .group_by(PartyRole.party_id)
        .having(func.count(func.distinct(PartyRole.role_type)) >= 2)
        .all()
    )
    return len(rows)


def build_party_organization_profile_audit(db: Session) -> dict[str, Any]:
    """Return PII-free schema, binding, bridge, and role coverage counts."""

    inspector = inspect(db.get_bind())
    installed_tables = set(inspector.get_table_names())
    missing_tables = sorted(set(_PROFILE_MODELS) - installed_tables)
    if missing_tables:
        return {
            "status": "not_installed",
            "missing_tables": missing_tables,
            "artifact_contract": {
                "read_only": True,
                "contains_identity_values": False,
                "automatic_role_assignment": False,
                "automatic_profile_binding": False,
            },
        }
    missing_columns = {
        table_name: sorted(
            _BINDING_COLUMNS
            - {column["name"] for column in inspector.get_columns(table_name)}
        )
        for table_name in _PROFILE_MODELS
    }
    missing_columns = {
        table_name: columns
        for table_name, columns in missing_columns.items()
        if columns
    }
    if missing_columns:
        return {
            "status": "not_installed",
            "missing_columns": missing_columns,
            "artifact_contract": {
                "read_only": True,
                "contains_identity_values": False,
                "automatic_role_assignment": False,
                "automatic_profile_binding": False,
            },
        }
    compatibility_types = {
        value: int(
            db.query(func.count(Organization.id))
            .filter(Organization.account_type == value)
            .scalar()
            or 0
        )
        for value in (
            OrganizationAccountType.partner.value,
            OrganizationAccountType.reseller.value,
            OrganizationAccountType.vendor.value,
        )
    }
    return {
        "status": "installed",
        "profiles": {
            table_name: _profile_counts(db, model)
            for table_name, model in _PROFILE_MODELS.items()
        },
        "vendor_twin_bridge": _vendor_twin_counts(db),
        "role_coverage": {
            "bound_resellers_missing_reseller_role": _missing_role_count(
                db,
                profile_model=Reseller,
                role_type=PartyRoleType.reseller,
            ),
            "bound_vendors_missing_vendor_role": _missing_role_count(
                db,
                profile_model=Vendor,
                role_type=PartyRoleType.vendor,
            ),
            "parties_with_multiple_channel_roles": _concurrent_role_parties(db),
        },
        "legacy_organization_account_type": compatibility_types,
        "artifact_contract": {
            "read_only": True,
            "contains_identity_values": False,
            "automatic_role_assignment": False,
            "automatic_profile_binding": False,
        },
    }
