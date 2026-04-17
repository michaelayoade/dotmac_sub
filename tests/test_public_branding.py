from __future__ import annotations

import uuid

from starlette.responses import RedirectResponse

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.web.public.branding import branding_asset


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
