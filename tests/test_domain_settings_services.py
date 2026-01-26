import pytest
from fastapi import HTTPException

from app.models.domain_settings import SettingDomain
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingCreate, DomainSettingUpdate
from app.services import domain_settings as domain_settings_service
from app.services import settings_api as settings_api_service


def test_domain_setting_domain_mismatch(db_session):
    settings = domain_settings_service.DomainSettings(domain=SettingDomain.gis)
    created = settings.create(
        db_session,
        DomainSettingCreate(
            domain=SettingDomain.gis,
            key="sync_enabled",
            value_type=SettingValueType.boolean,
            value_text="true",
        ),
    )
    with pytest.raises(HTTPException) as exc:
        settings.update(
            db_session,
            str(created.id),
            DomainSettingUpdate(domain=SettingDomain.auth),
        )
    assert exc.value.status_code == 400


def test_settings_api_gis_upsert_and_validation(db_session):
    updated = settings_api_service.upsert_gis_setting(
        db_session,
        "sync_enabled",
        DomainSettingUpdate(value_text="false"),
    )
    assert updated.value_type == SettingValueType.boolean
    assert updated.value_json is False
    fetched = settings_api_service.get_gis_setting(db_session, "sync_enabled")
    assert fetched.id == updated.id


def test_settings_api_invalid_key(db_session):
    with pytest.raises(HTTPException) as exc:
        settings_api_service.get_gis_setting(db_session, "bad_key")
    assert exc.value.status_code == 400
