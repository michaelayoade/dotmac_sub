from __future__ import annotations

import uuid

from starlette.responses import RedirectResponse

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.services.brand_profiles import upsert_brand_profile
from app.services.brand_theme import (
    CATEGORICAL_COLOR_ROLES,
    COLOR_SCALE_STEPS,
    LEGACY_TAILWIND_PALETTE_ROLES,
)
from app.web.public.branding import branding_asset, theme_css


def test_missing_managed_favicon_asset_redirects_to_default(db_session):
    file_id = uuid.uuid4()
    db_session.add(
        DomainSetting(
            domain=SettingDomain.comms,
            key="favicon_url",
            value_type=SettingValueType.string,
            value_text=f"/branding/assets/{file_id}",
            is_active=True,
        )
    )
    db_session.commit()

    response = branding_asset(str(file_id), db=db_session)

    assert isinstance(response, RedirectResponse)
    assert response.status_code == 307
    assert response.headers["location"] == "/favicon.ico"


def test_theme_css_emits_brand_owned_semantic_scales(db_session):
    upsert_brand_profile(
        db_session,
        scope_type="platform",
        scope_id=None,
        values={
            "metadata_": {
                "semantic_colors": {
                    "positive": "#166534",
                    "negative": "#991b1b",
                }
            }
        },
    )
    db_session.commit()

    response = theme_css(db=db_session)
    css = response.body.decode()

    assert "--color-semantic-positive-600:#166534" in css
    assert "--color-semantic-negative-600:#991b1b" in css
    assert "--color-semantic-warning-600:" in css
    assert "--color-primary-600:" in css
    for palette, role in LEGACY_TAILWIND_PALETTE_ROLES.items():
        for step in COLOR_SCALE_STEPS:
            assert f"--color-{palette}-{step}:var(--color-{role}-{step})" in css
    for index, role in enumerate(CATEGORICAL_COLOR_ROLES, start=1):
        assert f"--color-data-{index}:var(--color-{role}-600)" in css
