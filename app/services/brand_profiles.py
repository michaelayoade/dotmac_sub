"""Branding source of truth and platform/reseller/organization precedence."""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.branding import BrandProfile
from app.models.domain_settings import SettingDomain
from app.models.subscriber import Subscriber
from app.services.brand_theme import (
    DEFAULT_SECONDARY_HEX,
    SEMANTIC_TONES,
    is_accessible_semantic_color,
)
from app.services.branding_config import get_brand as get_deployment_brand

BRAND_SCOPE_PRECEDENCE = ("organization", "reseller", "platform")
_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
_ASSET_URL_FIELDS = {"logo_url", "dark_logo_url", "favicon_url"}
_LEGACY_CACHE_KEY = "brand_profiles.legacy"
_PROFILE_CACHE_KEY = "brand_profiles.active_profiles"
_LEGACY_SEMANTIC_SETTING_KEYS = {
    "positive": "brand_semantic_positive_color",
    "info": "brand_semantic_info_color",
    "warning": "brand_semantic_warning_color",
    "negative": "brand_semantic_negative_color",
    "neutral": "brand_semantic_neutral_color",
}


@dataclass(frozen=True)
class ResolvedBrand:
    name: str
    product_name: str
    legal_name: str
    tagline: str
    primary_color: str
    secondary_color: str
    semantic_colors: dict[str, str]
    logo_url: str
    dark_logo_url: str
    favicon_url: str
    support_email: str
    support_phone: str
    from_email: str
    from_name: str
    app_url: str
    portal_domain: str
    legal_address: dict[str, str]
    source_scope: str
    source_scope_id: str | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


_PROFILE_FIELDS = (
    "brand_name",
    "product_name",
    "legal_name",
    "tagline",
    "primary_color",
    "secondary_color",
    "logo_url",
    "dark_logo_url",
    "favicon_url",
    "support_email",
    "support_phone",
    "from_email",
    "from_name",
    "app_url",
    "portal_domain",
)


def _coerce_uuid(value: str | uuid.UUID | None) -> uuid.UUID | None:
    if value is None or isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


def _legacy_brand(db: Session) -> dict[str, Any]:
    cached = db.info.get(_LEGACY_CACHE_KEY)
    if isinstance(cached, dict):
        return dict(cached)
    from app.services import settings_spec
    from app.services.web_system_company_info import get_company_info

    static = get_deployment_brand()
    company = get_company_info(db)

    def comms(key: str, fallback: str = "") -> str:
        try:
            value = settings_spec.resolve_value(db, SettingDomain.comms, key)
        except Exception:
            value = None
        return str(value).strip() if value else fallback

    legal_name = company.get("company_name") or static["legal_name"]
    resolved = {
        "brand_name": static["name"],
        "product_name": legal_name or static["product_name"],
        "legal_name": legal_name,
        "tagline": static["tagline"],
        "primary_color": comms("brand_primary_color", static["primary_color"]),
        "secondary_color": comms("brand_secondary_color", DEFAULT_SECONDARY_HEX),
        "semantic_colors": {
            tone: comms(
                setting_key,
                static[f"semantic_{tone}_color"],
            )
            for tone, setting_key in _LEGACY_SEMANTIC_SETTING_KEYS.items()
        },
        "logo_url": comms("sidebar_logo_url"),
        "dark_logo_url": comms("sidebar_logo_dark_url"),
        "favicon_url": comms("favicon_url"),
        "support_email": company.get("company_email") or static["support_email"],
        "support_phone": company.get("company_phone") or "",
        "from_email": static["from_email"],
        "from_name": static["from_name"],
        "app_url": static["app_url"],
        "portal_domain": "",
        "legal_address": {
            "street1": company.get("company_address_street1", ""),
            "street2": company.get("company_address_street2", ""),
            "city": company.get("company_address_city", ""),
            "postal_code": company.get("company_address_zip", ""),
            "country": company.get("company_address_country", ""),
        },
        "source_scope": "legacy",
        "source_scope_id": None,
    }
    db.info[_LEGACY_CACHE_KEY] = dict(resolved)
    return resolved


def _profile(
    db: Session,
    scope_type: str,
    scope_id: uuid.UUID | None,
    *,
    active_only: bool = True,
) -> BrandProfile | None:
    cache_key = (scope_type, scope_id)
    if active_only:
        cache = db.info.setdefault(_PROFILE_CACHE_KEY, {})
        if cache_key in cache:
            return cache[cache_key]
    stmt = select(BrandProfile).where(
        BrandProfile.scope_type == scope_type,
        BrandProfile.scope_id == scope_id,
    )
    if active_only:
        stmt = stmt.where(BrandProfile.is_active.is_(True))
    profile = db.scalars(stmt).first()
    if active_only:
        cache[cache_key] = profile
    return profile


def _apply_profile(values: dict[str, Any], profile: BrandProfile) -> None:
    for field in _PROFILE_FIELDS:
        value = getattr(profile, field)
        if value not in (None, ""):
            values[field] = value
    if profile.legal_address:
        values["legal_address"] = dict(profile.legal_address)
    profile_semantic_colors = (profile.metadata_ or {}).get("semantic_colors")
    if isinstance(profile_semantic_colors, dict):
        semantic_colors = dict(values.get("semantic_colors") or {})
        for tone, color in profile_semantic_colors.items():
            if tone in SEMANTIC_TONES and (
                isinstance(color, str) and _HEX_COLOR.fullmatch(color)
            ):
                semantic_colors[tone] = color
        values["semantic_colors"] = semantic_colors
    values["source_scope"] = profile.scope_type
    values["source_scope_id"] = str(profile.scope_id) if profile.scope_id else None


def resolve_brand(
    db: Session,
    *,
    subscriber_id: str | uuid.UUID | None = None,
    reseller_id: str | uuid.UUID | None = None,
    organization_id: str | uuid.UUID | None = None,
) -> ResolvedBrand:
    """Resolve organization -> reseller -> platform -> legacy branding."""
    subscriber_uuid = _coerce_uuid(subscriber_id)
    reseller_uuid = _coerce_uuid(reseller_id)
    organization_uuid = _coerce_uuid(organization_id)
    if subscriber_uuid:
        subscriber = db.get(Subscriber, subscriber_uuid)
        if subscriber:
            reseller_uuid = reseller_uuid or subscriber.reseller_id
            organization_uuid = organization_uuid or subscriber.organization_id

    values = _legacy_brand(db)
    platform = _profile(db, "platform", None)
    if platform:
        _apply_profile(values, platform)
    if reseller_uuid:
        reseller = _profile(db, "reseller", reseller_uuid)
        if reseller:
            _apply_profile(values, reseller)
    if organization_uuid:
        organization = _profile(db, "organization", organization_uuid)
        if organization:
            _apply_profile(values, organization)
    values["name"] = str(
        values.pop("brand_name") or values["product_name"] or values["legal_name"]
    )
    return ResolvedBrand(**values)


def upsert_brand_profile(
    db: Session,
    *,
    scope_type: str,
    scope_id: str | uuid.UUID | None,
    values: dict[str, object],
) -> BrandProfile:
    scope_uuid = _coerce_uuid(scope_id)
    if scope_type not in BRAND_SCOPE_PRECEDENCE:
        raise ValueError("Unsupported brand scope")
    if (scope_type == "platform") != (scope_uuid is None):
        raise ValueError("Platform branding cannot have a scope id")
    if scope_type == "reseller":
        from app.models.subscriber import Reseller

        if db.get(Reseller, scope_uuid) is None:
            raise ValueError("Reseller brand scope does not exist")
    if scope_type == "organization":
        from app.models.organization import Organization

        if db.get(Organization, scope_uuid) is None:
            raise ValueError("Organization brand scope does not exist")
    profile = _profile(db, scope_type, scope_uuid, active_only=False)
    if profile is None:
        profile = BrandProfile(scope_type=scope_type, scope_id=scope_uuid)
        db.add(profile)
    for field in _PROFILE_FIELDS + ("legal_address", "metadata_"):
        if field not in values:
            continue
        value = values[field]
        if field in {"primary_color", "secondary_color"} and value:
            if not _HEX_COLOR.fullmatch(str(value)):
                raise ValueError(f"{field} must be a 6-digit hex colour")
        if field == "metadata_" and value:
            if not isinstance(value, dict):
                raise ValueError("metadata must be an object")
            semantic_colors = value.get("semantic_colors")
            if semantic_colors is not None:
                if not isinstance(semantic_colors, dict):
                    raise ValueError("metadata semantic_colors must be an object")
                for tone, color in semantic_colors.items():
                    if tone not in SEMANTIC_TONES or not _HEX_COLOR.fullmatch(
                        str(color)
                    ):
                        raise ValueError(
                            "semantic colors must use known tones and 6-digit hex values"
                        )
                    if not is_accessible_semantic_color(str(color)):
                        raise ValueError(
                            "semantic colors must meet WCAG AA contrast in light and dark themes"
                        )
        if field in _ASSET_URL_FIELDS and value:
            candidate = str(value).strip()
            if not candidate.startswith(
                ("https://", "http://", "/static/", "/branding/assets/")
            ):
                raise ValueError(f"{field} must be an approved branding URL")
        if field == "app_url" and value:
            if not str(value).strip().startswith(("https://", "http://")):
                raise ValueError("app_url must be an absolute HTTP(S) URL")
        setattr(profile, field, value)
    profile.is_active = True
    db.flush()
    db.info.setdefault(_PROFILE_CACHE_KEY, {})[(scope_type, scope_uuid)] = profile
    return profile


def sync_platform_brand_from_legacy_settings(
    db: Session, *, overwrite_fields: set[str] | None = None
) -> BrandProfile:
    db.info.pop(_LEGACY_CACHE_KEY, None)
    legacy = _legacy_brand(db)
    existing = _profile(db, "platform", None)
    if existing is None:
        values = {field: legacy[field] for field in _PROFILE_FIELDS}
        values["legal_address"] = legacy["legal_address"]
        values["metadata_"] = {"semantic_colors": legacy["semantic_colors"]}
    else:
        overwrite = overwrite_fields or set()
        values = {
            field: legacy[field]
            for field in _PROFILE_FIELDS
            if field in overwrite or getattr(existing, field) in (None, "")
        }
        if "legal_address" in overwrite or not existing.legal_address:
            values["legal_address"] = legacy["legal_address"]
        if "semantic_colors" in overwrite or not (existing.metadata_ or {}).get(
            "semantic_colors"
        ):
            metadata = dict(existing.metadata_ or {})
            metadata["semantic_colors"] = legacy["semantic_colors"]
            values["metadata_"] = metadata
    return upsert_brand_profile(
        db,
        scope_type="platform",
        scope_id=None,
        values=values,
    )


def upsert_brand_profile_committed(
    db: Session,
    *,
    scope_type: str,
    scope_id: str | uuid.UUID | None,
    values: dict[str, object],
) -> BrandProfile:
    profile = upsert_brand_profile(
        db,
        scope_type=scope_type,
        scope_id=scope_id,
        values=values,
    )
    db.commit()
    db.refresh(profile)
    return profile


def sync_platform_brand_from_legacy_settings_committed(
    db: Session,
    *,
    overwrite_fields: set[str] | None = None,
) -> BrandProfile:
    profile = sync_platform_brand_from_legacy_settings(
        db, overwrite_fields=overwrite_fields
    )
    db.commit()
    db.refresh(profile)
    return profile


def list_brand_profiles(
    db: Session, *, scope_type: str | None = None, active_only: bool = True
) -> list[BrandProfile]:
    stmt = select(BrandProfile)
    if scope_type:
        stmt = stmt.where(BrandProfile.scope_type == scope_type)
    if active_only:
        stmt = stmt.where(BrandProfile.is_active.is_(True))
    return list(
        db.scalars(
            stmt.order_by(BrandProfile.scope_type, BrandProfile.created_at)
        ).all()
    )


def deactivate_brand_profile_committed(
    db: Session, *, scope_type: str, scope_id: str | uuid.UUID | None
) -> None:
    profile = _profile(db, scope_type, _coerce_uuid(scope_id))
    if profile is None:
        raise ValueError("Brand profile not found")
    profile.is_active = False
    db.info.setdefault(_PROFILE_CACHE_KEY, {}).pop(
        (scope_type, _coerce_uuid(scope_id)), None
    )
    db.commit()
