from __future__ import annotations

from app.models.branding import BrandProfile
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.organization import Organization
from app.models.subscriber import Reseller
from app.models.subscription_engine import SettingValueType
from app.services.brand_profiles import (
    deactivate_brand_profile_committed,
    resolve_brand,
    sync_platform_brand_from_legacy_settings,
    upsert_brand_profile,
    upsert_brand_profile_committed,
)


def test_brand_resolution_uses_organization_then_reseller_then_platform(
    db_session, subscriber
):
    reseller = Reseller(name="Channel Partner")
    organization = Organization(name="Enterprise Customer")
    db_session.add_all([reseller, organization])
    db_session.flush()
    subscriber.reseller_id = reseller.id
    subscriber.organization_id = organization.id
    upsert_brand_profile(
        db_session,
        scope_type="platform",
        scope_id=None,
        values={
            "brand_name": "Platform Short",
            "product_name": "Platform",
            "primary_color": "#112233",
        },
    )
    upsert_brand_profile(
        db_session,
        scope_type="reseller",
        scope_id=reseller.id,
        values={"product_name": "Partner", "secondary_color": "#445566"},
    )
    upsert_brand_profile(
        db_session,
        scope_type="organization",
        scope_id=organization.id,
        values={"product_name": "Enterprise"},
    )
    db_session.commit()

    resolved = resolve_brand(db_session, subscriber_id=subscriber.id)

    assert resolved.product_name == "Enterprise"
    assert resolved.name == "Platform Short"
    assert resolved.primary_color == "#112233"
    assert resolved.secondary_color == "#445566"
    assert resolved.source_scope == "organization"
    assert resolved.source_scope_id == str(organization.id)


def test_brand_profile_rejects_invalid_scope_and_colour(db_session):
    try:
        upsert_brand_profile(
            db_session,
            scope_type="platform",
            scope_id=None,
            values={"primary_color": "green"},
        )
    except ValueError as exc:
        assert "6-digit hex" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("invalid colour accepted")


def test_legacy_branding_backfill_is_idempotent(db_session):
    db_session.add_all(
        [
            DomainSetting(
                domain=SettingDomain.billing,
                key="company_name",
                value_type=SettingValueType.string,
                value_text="Backfilled ISP",
                is_active=True,
            ),
            DomainSetting(
                domain=SettingDomain.comms,
                key="brand_primary_color",
                value_type=SettingValueType.string,
                value_text="#123456",
                is_active=True,
            ),
        ]
    )
    db_session.commit()

    first = sync_platform_brand_from_legacy_settings(db_session)
    db_session.commit()
    second = sync_platform_brand_from_legacy_settings(db_session)
    db_session.commit()

    assert first.id == second.id
    assert second.product_name == "Backfilled ISP"
    assert second.primary_color == "#123456"
    assert (
        db_session.query(BrandProfile)
        .filter(BrandProfile.scope_type == "platform")
        .count()
        == 1
    )


def test_field_scoped_legacy_sync_does_not_clobber_canonical_name(db_session):
    upsert_brand_profile(
        db_session,
        scope_type="platform",
        scope_id=None,
        values={"product_name": "Canonical Name"},
    )
    db_session.add(
        DomainSetting(
            domain=SettingDomain.comms,
            key="sidebar_logo_url",
            value_type=SettingValueType.string,
            value_text="/branding/assets/logo-id",
            is_active=True,
        )
    )
    db_session.commit()

    profile = sync_platform_brand_from_legacy_settings(
        db_session, overwrite_fields={"logo_url"}
    )

    assert profile.product_name == "Canonical Name"
    assert profile.logo_url == "/branding/assets/logo-id"


def test_inactive_brand_profile_is_reactivated_instead_of_duplicated(db_session):
    profile = upsert_brand_profile_committed(
        db_session,
        scope_type="platform",
        scope_id=None,
        values={"product_name": "First"},
    )
    deactivate_brand_profile_committed(
        db_session, scope_type="platform", scope_id=None
    )

    reactivated = upsert_brand_profile_committed(
        db_session,
        scope_type="platform",
        scope_id=None,
        values={"product_name": "Second"},
    )

    assert reactivated.id == profile.id
    assert reactivated.is_active is True
    assert reactivated.product_name == "Second"
