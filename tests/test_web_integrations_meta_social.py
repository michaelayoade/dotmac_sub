from __future__ import annotations

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.services import web_integrations_meta_social


def _setting(db_session, key: str, value: str, *, is_secret: bool = False) -> None:
    row = DomainSetting(
        domain=SettingDomain.comms,
        key=key,
        value_type=SettingValueType.string,
        value_text=value,
        is_secret=is_secret,
        is_active=True,
    )
    db_session.add(row)
    db_session.flush()


def test_save_meta_social_config_preserves_blank_secret_fields(db_session):
    _setting(db_session, "meta_app_secret", "existing-secret", is_secret=True)
    _setting(
        db_session,
        "meta_facebook_access_token_override",
        "existing-facebook",
        is_secret=True,
    )

    web_integrations_meta_social.save_config(
        db_session,
        auth_mode="override",
        meta_app_id="app-1",
        meta_app_secret="",
        meta_webhook_verify_token="verify-1",
        meta_graph_api_version="v21.0",
        meta_facebook_access_token_override="",
        meta_instagram_access_token_override="instagram-token",
    )

    rows = {
        row.key: row
        for row in db_session.query(DomainSetting).filter(
            DomainSetting.domain == SettingDomain.comms,
            DomainSetting.key.in_(
                {
                    "meta_social_auth_mode",
                    "meta_app_id",
                    "meta_app_secret",
                    "meta_webhook_verify_token",
                    "meta_graph_api_version",
                    "meta_facebook_access_token_override",
                    "meta_instagram_access_token_override",
                }
            ),
        )
    }
    assert rows["meta_social_auth_mode"].value_text == "override"
    assert rows["meta_app_id"].value_text == "app-1"
    assert rows["meta_app_secret"].value_text == "existing-secret"
    assert rows["meta_facebook_access_token_override"].value_text == "existing-facebook"
    assert rows["meta_instagram_access_token_override"].value_text == "instagram-token"
    assert rows["meta_webhook_verify_token"].is_secret is True
