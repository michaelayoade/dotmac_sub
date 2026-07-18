"""Read-only convergence audit for Person principals and auth contexts.

Only schema state and aggregate counts leave this module. Names, emails,
phones, UUIDs, legacy person identifiers, binding reasons, credentials, roles,
permissions, and tokens are never emitted or changed.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, inspect
from sqlalchemy.orm import Session

from app.models.field_vendor import FieldVendor, FieldVendorUser
from app.models.organization import (
    Organization,
    OrganizationMembership,
    OrganizationMembershipRole,
)
from app.models.party import Party, PartyMembership, PartyMembershipType, PartyType
from app.models.subscriber import Reseller, ResellerUser
from app.models.system_user import SystemUser
from app.models.vendor_routes import Vendor

_EVIDENCE_COLUMNS = {
    "party_bound_at",
    "party_binding_source",
    "party_binding_reason",
}
_REQUIRED_COLUMNS = {
    "system_users": _EVIDENCE_COLUMNS | {"person_party_id"},
    "reseller_users": _EVIDENCE_COLUMNS | {"person_party_id", "party_membership_id"},
    "organization_memberships": _EVIDENCE_COLUMNS | {"party_membership_id"},
    "field_vendor_users": _EVIDENCE_COLUMNS | {"party_membership_id"},
}


def _evidence_complete(row: Any) -> bool:
    return bool(
        row.party_bound_at is not None
        and (row.party_binding_source or "").strip()
        and (row.party_binding_reason or "").strip()
    )


def _has_any_evidence(row: Any) -> bool:
    return any(
        value is not None
        for value in (
            row.party_bound_at,
            row.party_binding_source,
            row.party_binding_reason,
        )
    )


def _system_user_counts(db: Session, parties: dict[UUID, str]) -> dict[str, int]:
    counts = {
        "total": 0,
        "bound": 0,
        "unbound": 0,
        "incomplete_evidence": 0,
        "missing_or_non_person_party": 0,
    }
    for row in db.query(SystemUser).all():
        counts["total"] += 1
        if row.person_party_id is None:
            counts["unbound"] += 1
            if _has_any_evidence(row):
                counts["incomplete_evidence"] += 1
            continue
        counts["bound"] += 1
        if not _evidence_complete(row):
            counts["incomplete_evidence"] += 1
        if parties.get(row.person_party_id) != PartyType.person.value:
            counts["missing_or_non_person_party"] += 1
    return counts


def _reseller_user_counts(
    db: Session,
    *,
    parties: dict[UUID, str],
    memberships: dict[UUID, tuple[UUID, UUID, str]],
) -> dict[str, int]:
    reseller_parties = {
        row.id: row.party_id for row in db.query(Reseller.id, Reseller.party_id).all()
    }
    counts = {
        "total": 0,
        "unbound": 0,
        "partial_binding": 0,
        "missing_or_non_person_party": 0,
        "missing_reseller_organization_party": 0,
        "missing_or_wrong_membership": 0,
        "person_context_mismatch": 0,
        "organization_context_mismatch": 0,
        "aligned": 0,
    }
    for row in db.query(ResellerUser).all():
        counts["total"] += 1
        binding_values = (row.person_party_id, row.party_membership_id)
        if binding_values == (None, None):
            if _has_any_evidence(row):
                counts["partial_binding"] += 1
            else:
                counts["unbound"] += 1
            continue
        if None in binding_values or not _evidence_complete(row):
            counts["partial_binding"] += 1
            continue
        assert row.person_party_id is not None
        assert row.party_membership_id is not None
        if parties.get(row.person_party_id) != PartyType.person.value:
            counts["missing_or_non_person_party"] += 1
            continue
        organization_party_id = reseller_parties.get(row.reseller_id)
        if (
            organization_party_id is None
            or parties.get(organization_party_id) != PartyType.organization.value
        ):
            counts["missing_reseller_organization_party"] += 1
            continue
        membership = memberships.get(row.party_membership_id)
        if (
            membership is None
            or membership[2] != PartyMembershipType.reseller_admin.value
        ):
            counts["missing_or_wrong_membership"] += 1
            continue
        if membership[0] != row.person_party_id:
            counts["person_context_mismatch"] += 1
            continue
        if membership[1] != organization_party_id:
            counts["organization_context_mismatch"] += 1
            continue
        counts["aligned"] += 1
    return counts


def _organization_membership_counts(
    db: Session,
    *,
    parties: dict[UUID, str],
    memberships: dict[UUID, tuple[UUID, UUID, str]],
) -> dict[str, int]:
    organization_parties = {
        row.id: row.party_id
        for row in db.query(Organization.id, Organization.party_id).all()
    }
    role_types = {
        OrganizationMembershipRole.owner.value: PartyMembershipType.owner.value,
        OrganizationMembershipRole.admin.value: PartyMembershipType.admin.value,
        OrganizationMembershipRole.member.value: PartyMembershipType.member.value,
    }
    counts = {
        "total": 0,
        "unbound": 0,
        "incomplete_binding": 0,
        "missing_organization_party": 0,
        "missing_or_wrong_membership": 0,
        "organization_context_mismatch": 0,
        "aligned": 0,
    }
    for row in db.query(OrganizationMembership).all():
        counts["total"] += 1
        if row.party_membership_id is None:
            if _has_any_evidence(row):
                counts["incomplete_binding"] += 1
            else:
                counts["unbound"] += 1
            continue
        if not _evidence_complete(row):
            counts["incomplete_binding"] += 1
            continue
        organization_party_id = organization_parties.get(row.organization_id)
        if (
            organization_party_id is None
            or parties.get(organization_party_id) != PartyType.organization.value
        ):
            counts["missing_organization_party"] += 1
            continue
        membership = memberships.get(row.party_membership_id)
        expected_type = role_types.get(row.role)
        if (
            membership is None
            or membership[2] != expected_type
            or parties.get(membership[0]) != PartyType.person.value
            or parties.get(membership[1]) != PartyType.organization.value
        ):
            counts["missing_or_wrong_membership"] += 1
            continue
        if membership[1] != organization_party_id:
            counts["organization_context_mismatch"] += 1
            continue
        counts["aligned"] += 1
    return counts


def _field_vendor_user_counts(
    db: Session,
    *,
    parties: dict[UUID, str],
    system_user_parties: dict[UUID, UUID | None],
    memberships: dict[UUID, tuple[UUID, UUID, str]],
) -> dict[str, int]:
    vendors = {
        row.id: row.party_id for row in db.query(Vendor.id, Vendor.party_id).all()
    }
    field_vendors = {
        row.id: (row.crm_vendor_id, row.party_id)
        for row in db.query(
            FieldVendor.id, FieldVendor.crm_vendor_id, FieldVendor.party_id
        ).all()
    }
    field_rows = db.query(FieldVendorUser).all()
    counts = {
        "total": len(field_rows),
        "unbound": 0,
        "incomplete_binding": 0,
        "invalid_vendor_profile_bridge": 0,
        "orphan_vendor_profile_bridge": 0,
        "missing_profile_party_context": 0,
        "missing_system_user_person_party": 0,
        "missing_or_wrong_membership": 0,
        "person_context_mismatch": 0,
        "organization_context_mismatch": 0,
        "aligned": 0,
    }
    for field in field_rows:
        field_vendor = field_vendors.get(field.vendor_id)
        vendor_party_id = None
        bridge_valid = False
        if field_vendor is None or field_vendor[0] is None:
            counts["invalid_vendor_profile_bridge"] += 1
        else:
            try:
                native_vendor_id = UUID(field_vendor[0])
            except ValueError:
                counts["invalid_vendor_profile_bridge"] += 1
            else:
                if native_vendor_id not in vendors:
                    counts["orphan_vendor_profile_bridge"] += 1
                else:
                    vendor_party_id = vendors[native_vendor_id]
                    bridge_valid = True
        if field.party_membership_id is None:
            if _has_any_evidence(field):
                counts["incomplete_binding"] += 1
            else:
                counts["unbound"] += 1
            continue
        if not _evidence_complete(field):
            counts["incomplete_binding"] += 1
            continue
        if not bridge_valid or field_vendor is None:
            continue
        if (
            vendor_party_id is None
            or parties.get(vendor_party_id) != PartyType.organization.value
            or field_vendor[1] != vendor_party_id
        ):
            counts["missing_profile_party_context"] += 1
            continue
        person_party_id = system_user_parties.get(field.system_user_id)
        if (
            person_party_id is None
            or parties.get(person_party_id) != PartyType.person.value
        ):
            counts["missing_system_user_person_party"] += 1
            continue
        membership = memberships.get(field.party_membership_id)
        if (
            membership is None
            or membership[2] != PartyMembershipType.vendor_user.value
            or parties.get(membership[0]) != PartyType.person.value
            or parties.get(membership[1]) != PartyType.organization.value
        ):
            counts["missing_or_wrong_membership"] += 1
            continue
        if membership[0] != person_party_id:
            counts["person_context_mismatch"] += 1
            continue
        if membership[1] != vendor_party_id:
            counts["organization_context_mismatch"] += 1
            continue
        counts["aligned"] += 1
    return counts


def build_party_principal_context_audit(db: Session) -> dict[str, Any]:
    """Return PII-free principal/context schema and convergence counts."""

    inspector = inspect(db.get_bind())
    installed_tables = set(inspector.get_table_names())
    missing_tables = sorted(set(_REQUIRED_COLUMNS) - installed_tables)
    if missing_tables:
        return _not_installed(missing_tables=missing_tables)
    missing_columns = {
        table_name: sorted(
            required - {column["name"] for column in inspector.get_columns(table_name)}
        )
        for table_name, required in _REQUIRED_COLUMNS.items()
    }
    missing_columns = {
        table_name: columns
        for table_name, columns in missing_columns.items()
        if columns
    }
    if missing_columns:
        return _not_installed(missing_columns=missing_columns)
    parties = {
        row.id: row.party_type for row in db.query(Party.id, Party.party_type).all()
    }
    memberships = {
        row.id: (
            row.person_party_id,
            row.organization_party_id,
            row.membership_type,
        )
        for row in db.query(
            PartyMembership.id,
            PartyMembership.person_party_id,
            PartyMembership.organization_party_id,
            PartyMembership.membership_type,
        ).all()
    }
    system_user_parties = {
        row.id: row.person_party_id
        for row in db.query(SystemUser.id, SystemUser.person_party_id).all()
    }
    return {
        "status": "installed",
        "system_user_principals": _system_user_counts(db, parties),
        "reseller_user_principals": _reseller_user_counts(
            db,
            parties=parties,
            memberships=memberships,
        ),
        "organization_membership_contexts": _organization_membership_counts(
            db,
            parties=parties,
            memberships=memberships,
        ),
        "field_vendor_user_contexts": _field_vendor_user_counts(
            db,
            parties=parties,
            system_user_parties=system_user_parties,
            memberships=memberships,
        ),
        "party_memberships": {
            "total": int(db.query(func.count(PartyMembership.id)).scalar() or 0),
            "missing_or_non_person_endpoint": sum(
                1
                for person_party_id, _organization_party_id, _type in memberships.values()
                if parties.get(person_party_id) != PartyType.person.value
            ),
            "missing_or_non_organization_endpoint": sum(
                1
                for _person_party_id, organization_party_id, _type in memberships.values()
                if parties.get(organization_party_id) != PartyType.organization.value
            ),
        },
        "artifact_contract": _artifact_contract(),
    }


def _artifact_contract() -> dict[str, bool]:
    return {
        "read_only": True,
        "contains_identity_values": False,
        "automatic_party_binding": False,
        "automatic_membership_creation": False,
        "changes_authentication_or_authorization": False,
    }


def _not_installed(**details: Any) -> dict[str, Any]:
    return {
        "status": "not_installed",
        **details,
        "artifact_contract": _artifact_contract(),
    }
