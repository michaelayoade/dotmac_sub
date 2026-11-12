"""Catalog speed validation follows the existing Sub rate precedence."""

from types import SimpleNamespace
from uuid import uuid4

from app.services import access_rate_profile, web_catalog_offers


def _profile(**overrides):
    values = {
        "id": uuid4(),
        "mikrotik_rate_limit": None,
        "download_speed": None,
        "upload_speed": None,
        "code": "external",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _ProfileDb:
    def __init__(self, profile):
        self.profile = profile

    def get(self, model, profile_id):
        return self.profile if str(self.profile.id) == str(profile_id) else None


def _query(
    profile,
    download_mbps,
    upload_mbps,
    *,
    existing_offer=None,
    previous_profile_id=None,
):
    return access_rate_profile.CatalogSpeedQuery(
        profile_id=profile.id,
        profile_code=profile.code,
        configured=access_rate_profile.profile_configured_speed_mbps(profile),
        download_mbps=download_mbps,
        upload_mbps=upload_mbps,
        existing_offer_id=existing_offer.id if existing_offer else None,
        existing_download_mbps=(
            existing_offer.speed_download_mbps if existing_offer else None
        ),
        existing_upload_mbps=(
            existing_offer.speed_upload_mbps if existing_offer else None
        ),
        previous_profile_id=previous_profile_id,
    )


def test_profile_literal_speed_uses_upload_first_and_unit_conversion():
    speed = access_rate_profile.profile_configured_speed_mbps(
        _profile(mikrotik_rate_limit="10M/20M 12M/24M")
    )
    assert speed.download_mbps == 20
    assert speed.upload_mbps == 10
    assert speed.source is access_rate_profile.RateSpeedSource.literal
    assert "takes precedence" in speed.provisioning_note


def test_profile_numeric_speed_is_kbps_converted_to_mbps():
    speed = access_rate_profile.profile_configured_speed_mbps(
        _profile(download_speed=20_000, upload_speed=10_000)
    )
    assert speed.download_mbps == 20
    assert speed.upload_mbps == 10
    assert "fallback" in speed.provisioning_note


def test_rate_owner_classifies_mismatch_and_unchanged_legacy_pair():
    profile = _profile(mikrotik_rate_limit="10M/20M")
    mismatch = access_rate_profile.assess_catalog_profile_speed(
        _query(profile, download_mbps=100, upload_mbps=10)
    )
    assert mismatch.status is access_rate_profile.CatalogSpeedStatus.mismatch
    assert mismatch.blocks_save
    unchanged = access_rate_profile.assess_catalog_profile_speed(
        _query(
            profile,
            download_mbps=100,
            upload_mbps=10,
            existing_offer=SimpleNamespace(
                id=uuid4(), speed_download_mbps=100, speed_upload_mbps=10
            ),
            previous_profile_id=profile.id,
        )
    )
    assert unchanged.status is access_rate_profile.CatalogSpeedStatus.unchanged_legacy
    assert not unchanged.blocks_save


def test_numeric_profile_difference_is_advisory_when_tariff_rate_wins():
    profile = _profile(download_speed=20_000, upload_speed=5_000)
    assessment = access_rate_profile.assess_catalog_profile_speed(
        _query(profile, download_mbps=10, upload_mbps=5)
    )
    assert (
        assessment.status
        is access_rate_profile.CatalogSpeedStatus.profile_fields_differ
    )
    assert not assessment.blocks_save
    assert (
        web_catalog_offers.selected_profile_speed_issue(
            _ProfileDb(profile),
            profile_id=str(profile.id),
            download_mbps=10,
            upload_mbps=5,
        )
        is None
    )


def test_unspecified_tariff_speed_does_not_conflict_with_profile_rate():
    profile = _profile(mikrotik_rate_limit="10M/20M")
    assessment = access_rate_profile.assess_catalog_profile_speed(
        _query(profile, download_mbps=None, upload_mbps=None)
    )
    assert assessment.status is access_rate_profile.CatalogSpeedStatus.match
    assert not assessment.blocks_save


def test_generated_profile_can_refresh_when_tariff_speed_changes():
    offer_id = uuid4()
    profile = _profile(
        code=access_rate_profile.generated_profile_code_for_offer(offer_id),
        mikrotik_rate_limit="5M/10M",
    )
    existing_offer = SimpleNamespace(
        id=offer_id, speed_download_mbps=10, speed_upload_mbps=5
    )
    assessment = access_rate_profile.assess_catalog_profile_speed(
        _query(
            profile,
            download_mbps=20,
            upload_mbps=5,
            existing_offer=existing_offer,
            previous_profile_id=profile.id,
        )
    )
    assert (
        assessment.status
        is access_rate_profile.CatalogSpeedStatus.generated_profile_refresh
    )
    assert not assessment.blocks_save


def test_new_mismatch_is_rejected_but_unchanged_legacy_pair_can_be_edited():
    profile = _profile(mikrotik_rate_limit="10M/20M")
    db = _ProfileDb(profile)
    kwargs = {
        "profile_id": str(profile.id),
        "download_mbps": 100,
        "upload_mbps": 10,
    }
    assert "does not match" in web_catalog_offers.selected_profile_speed_issue(
        db, **kwargs
    )
    assert (
        web_catalog_offers.selected_profile_speed_issue(
            db,
            **kwargs,
            existing_offer=SimpleNamespace(
                id=uuid4(), speed_download_mbps=100, speed_upload_mbps=10
            ),
            previous_profile_id=str(profile.id),
        )
        is None
    )
    assert (
        web_catalog_offers.selected_profile_speed_issue(
            db, profile_id=str(profile.id), download_mbps=20, upload_mbps=10
        )
        is None
    )


def test_fractional_profile_rate_explains_integer_form_limit():
    profile = _profile(mikrotik_rate_limit="5M/12.5M")
    issue = web_catalog_offers.selected_profile_speed_issue(
        _ProfileDb(profile),
        profile_id=str(profile.id),
        download_mbps=12,
        upload_mbps=5,
    )
    assert "fractional Mbps" in issue


def test_update_can_clear_optional_speed_values():
    offer = web_catalog_offers.default_offer_form()
    offer["name"] = "Optional speed test"
    payload = web_catalog_offers.update_offer_payload(offer).model_dump(
        exclude_unset=True
    )
    assert payload["speed_download_mbps"] is None
    assert payload["speed_upload_mbps"] is None


def test_form_rejects_fractional_or_nonpositive_speed():
    offer = web_catalog_offers.default_offer_form()
    offer["price_amount"] = "1000"
    offer["speed_download_mbps"] = "10.5"
    assert "whole number" in web_catalog_offers.validate_offer_form(offer)
    offer["speed_download_mbps"] = "0"
    assert "greater than zero" in web_catalog_offers.validate_offer_form(offer)
