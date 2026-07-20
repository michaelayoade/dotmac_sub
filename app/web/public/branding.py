"""Public branding asset routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from fastapi.responses import (
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.stored_file import StoredFile
from app.services import settings_spec
from app.services.brand_theme import (
    CATEGORICAL_COLOR_ROLES,
    COLOR_SCALE_STEPS,
    DEFAULT_HEX,
    DEFAULT_SECONDARY_HEX,
    DEFAULT_SEMANTIC_COLORS,
    LEGACY_TAILWIND_PALETTE_ROLES,
    generate_scale,
    is_accessible_semantic_color,
)
from app.services.file_storage import file_uploads
from app.services.object_storage import ObjectNotFoundError
from app.services.public_branding import is_configured_favicon_url
from app.services.settings_spec import SettingDomain

router = APIRouter(prefix="/branding", tags=["public-branding"])

_LOGIN_HERO_PORTALS = {"customer", "reseller", "admin"}


def _theme_css(
    scale: dict[int, str],
    secondary_scale: dict[int, str],
    semantic_scales: dict[str, dict[int, str]],
) -> str:
    primary_lines = "".join(
        f"  --color-primary-{step}:{scale[step]};\n" for step in COLOR_SCALE_STEPS
    )
    brand_lines = "".join(
        f"  --color-brand-{step}:{scale[step]};\n"
        for step in (50, 100, 200, 300, 400, 500, 600, 700, 800, 900)
    )
    accent_lines = "".join(
        f"  --color-accent-{step}:{secondary_scale[step]};\n"
        for step in COLOR_SCALE_STEPS
    )
    semantic_lines = "".join(
        f"  --color-semantic-{tone}-{step}:{tone_scale[step]};\n"
        for tone, tone_scale in semantic_scales.items()
        for step in COLOR_SCALE_STEPS
    )
    compatibility_lines = "".join(
        f"  --color-{palette}-{step}:var(--color-{role}-{step});\n"
        for palette, role in LEGACY_TAILWIND_PALETTE_ROLES.items()
        for step in COLOR_SCALE_STEPS
    )
    categorical_lines = "".join(
        f"  --color-data-{index}:var(--color-{role}-600);\n"
        for index, role in enumerate(CATEGORICAL_COLOR_ROLES, start=1)
    )
    return (
        ":root{\n"
        + primary_lines
        + brand_lines
        + accent_lines
        + semantic_lines
        + compatibility_lines
        + categorical_lines
        + "}\n"
    )


@router.get("/theme.css", include_in_schema=False)
def theme_css(db: Session = Depends(get_db)):
    """Runtime brand colour theme as a CSS variable stylesheet.

    Reads ``brand_primary_color`` and ``brand_secondary_color`` and emits an
    11-stop scale for the identity and semantic custom properties, then maps
    legacy Tailwind palettes and categorical data tokens onto those owners.
    Never raises: any failure falls back to the checked-in brand defaults.
    """
    brand = None
    try:
        from app.services.brand_profiles import resolve_brand

        brand = resolve_brand(db)
        hex_color = brand.primary_color
        scale = generate_scale(hex_color or DEFAULT_HEX)
    except Exception:
        scale = generate_scale(DEFAULT_HEX)
    try:
        secondary_hex = brand.secondary_color if brand else DEFAULT_SECONDARY_HEX
        secondary_scale = generate_scale(secondary_hex or DEFAULT_SECONDARY_HEX)
    except Exception:
        secondary_scale = generate_scale(DEFAULT_SECONDARY_HEX)
    try:
        configured_semantic_colors = (
            brand.semantic_colors if brand else DEFAULT_SEMANTIC_COLORS
        )
        semantic_scales = {
            tone: generate_scale(
                configured_semantic_colors.get(tone, default)
                if is_accessible_semantic_color(
                    configured_semantic_colors.get(tone, default)
                )
                else default
            )
            for tone, default in DEFAULT_SEMANTIC_COLORS.items()
        }
    except Exception:
        semantic_scales = {
            tone: generate_scale(color)
            for tone, color in DEFAULT_SEMANTIC_COLORS.items()
        }
    return Response(
        content=_theme_css(scale, secondary_scale, semantic_scales),
        media_type="text/css",
        headers={"Cache-Control": "public, max-age=300"},
    )


@router.get("/login-hero/{portal}", include_in_schema=False)
def login_hero(portal: str, db: Session = Depends(get_db)):
    """Redirect to the configured login hero image for a portal.

    Falls back to the bundled static illustration when no override is set.
    """
    if portal not in _LOGIN_HERO_PORTALS:
        return Response(status_code=404)
    fallback = f"/static/illustrations/login-hero-{portal}.webp"
    try:
        raw = settings_spec.resolve_value(
            db, SettingDomain.comms, f"login_hero_{portal}_url"
        )
        configured = str(raw).strip() if raw else ""
    except Exception:
        configured = ""
    target = configured or fallback
    return RedirectResponse(url=target, status_code=307)


@router.get("/manifest.webmanifest", include_in_schema=False)
def web_manifest(db: Session = Depends(get_db)):
    """Brand-driven PWA manifest from the canonical platform profile."""
    from app.services.brand_profiles import resolve_brand

    resolved = resolve_brand(db)
    manifest = {
        "name": f"{resolved.name} Selfcare",
        "short_name": resolved.name,
        "description": "Manage your internet subscription, billing, and support",
        "start_url": "/portal/",
        "display": "standalone",
        "background_color": "#ffffff",
        "theme_color": resolved.primary_color,
        "icons": [
            {
                "src": "/static/branding/favicon/icon-192.png",
                "sizes": "192x192",
                "type": "image/png",
            },
            {
                "src": "/static/branding/favicon/icon-512.png",
                "sizes": "512x512",
                "type": "image/png",
            },
            {
                "src": "/static/branding/favicon/apple-touch-icon.png",
                "sizes": "180x180",
                "type": "image/png",
            },
        ],
    }
    return JSONResponse(
        manifest,
        media_type="application/manifest+json",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/assets/{file_id}")
def branding_asset(file_id: str, db: Session = Depends(get_db)):
    try:
        file_uuid = uuid.UUID(file_id)
    except ValueError:
        return Response(status_code=404)

    record = db.get(StoredFile, file_uuid)
    if not record or record.is_deleted or record.entity_type != "branding_asset":
        if is_configured_favicon_url(db, file_uuid):
            return RedirectResponse(url="/favicon.ico", status_code=307)
        return Response(status_code=404)

    try:
        stream = file_uploads.stream_file(record)
    except ObjectNotFoundError:
        if is_configured_favicon_url(db, file_uuid):
            return RedirectResponse(url="/favicon.ico", status_code=307)
        return Response(status_code=404)

    headers: dict[str, str] = {"Cache-Control": "public, max-age=3600"}
    if stream.content_length is not None:
        headers["Content-Length"] = str(stream.content_length)
    return StreamingResponse(
        stream.chunks,
        media_type=stream.content_type or "application/octet-stream",
        headers=headers,
    )
