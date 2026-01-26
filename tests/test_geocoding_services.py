"""Tests for geocoding services."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.services import geocoding


# =============================================================================
# Helper Function Tests
# =============================================================================


class TestSettingValue:
    """Tests for _setting_value helper."""

    def test_returns_none_when_not_found(self, db_session):
        """Test returns None when setting not found."""
        result = geocoding._setting_value(db_session, "nonexistent_key")
        assert result is None

    def test_returns_value_text(self, db_session):
        """Test returns value_text when set."""
        setting = DomainSetting(
            domain=SettingDomain.geocoding,
            key="base_url",
            value_text="https://custom.nominatim.com",
            is_active=True,
        )
        db_session.add(setting)
        db_session.commit()

        result = geocoding._setting_value(db_session, "base_url")
        assert result == "https://custom.nominatim.com"

    def test_returns_value_json_as_string(self, db_session):
        """Test returns value_json as string when value_text is None."""
        setting = DomainSetting(
            domain=SettingDomain.geocoding,
            key="json_setting",
            value_type=SettingValueType.json,
            value_text=None,
            value_json={"key": "value"},
            is_active=True,
        )
        db_session.add(setting)
        db_session.commit()

        result = geocoding._setting_value(db_session, "json_setting")
        assert result == "{'key': 'value'}"

    def test_ignores_inactive_settings(self, db_session):
        """Test ignores inactive settings."""
        setting = DomainSetting(
            domain=SettingDomain.geocoding,
            key="inactive_key",
            value_text="inactive_value",
            is_active=False,
        )
        db_session.add(setting)
        db_session.commit()

        result = geocoding._setting_value(db_session, "inactive_key")
        assert result is None


class TestSettingBool:
    """Tests for _setting_bool helper."""

    def test_returns_default_when_not_found(self, db_session):
        """Test returns default when setting not found."""
        result = geocoding._setting_bool(db_session, "nonexistent", True)
        assert result is True

        result = geocoding._setting_bool(db_session, "nonexistent", False)
        assert result is False

    def test_parses_true_values(self, db_session):
        """Test parses various true values."""
        for value in ["1", "true", "True", "TRUE", "yes", "Yes", "on", "ON"]:
            setting = DomainSetting(
                domain=SettingDomain.geocoding,
                key=f"bool_{value}",
                value_text=value,
                is_active=True,
            )
            db_session.add(setting)
            db_session.commit()

            result = geocoding._setting_bool(db_session, f"bool_{value}", False)
            assert result is True, f"Expected True for '{value}'"

    def test_parses_false_values(self, db_session):
        """Test parses false values."""
        setting = DomainSetting(
            domain=SettingDomain.geocoding,
            key="bool_false",
            value_text="false",
            is_active=True,
        )
        db_session.add(setting)
        db_session.commit()

        result = geocoding._setting_bool(db_session, "bool_false", True)
        assert result is False


class TestSettingInt:
    """Tests for _setting_int helper."""

    def test_returns_default_when_not_found(self, db_session):
        """Test returns default when setting not found."""
        result = geocoding._setting_int(db_session, "nonexistent", 42)
        assert result == 42

    def test_parses_integer(self, db_session):
        """Test parses integer value."""
        setting = DomainSetting(
            domain=SettingDomain.geocoding,
            key="timeout_sec",
            value_text="10",
            is_active=True,
        )
        db_session.add(setting)
        db_session.commit()

        result = geocoding._setting_int(db_session, "timeout_sec", 5)
        assert result == 10

    def test_returns_default_for_invalid_int(self, db_session):
        """Test returns default when value is not a valid int."""
        setting = DomainSetting(
            domain=SettingDomain.geocoding,
            key="invalid_int",
            value_text="not_a_number",
            is_active=True,
        )
        db_session.add(setting)
        db_session.commit()

        result = geocoding._setting_int(db_session, "invalid_int", 99)
        assert result == 99


class TestComposeAddress:
    """Tests for _compose_address helper."""

    def test_full_address(self):
        """Test composing full address."""
        data = {
            "address_line1": "123 Main St",
            "address_line2": "Apt 4",
            "city": "New York",
            "region": "NY",
            "postal_code": "10001",
            "country_code": "US",
        }
        result = geocoding._compose_address(data)
        assert result == "123 Main St, Apt 4, New York, NY, 10001, US"

    def test_partial_address(self):
        """Test composing partial address."""
        data = {
            "address_line1": "456 Oak Ave",
            "city": "Boston",
            "region": "MA",
        }
        result = geocoding._compose_address(data)
        assert result == "456 Oak Ave, Boston, MA"

    def test_empty_address(self):
        """Test composing empty address returns None."""
        data = {}
        result = geocoding._compose_address(data)
        assert result is None

    def test_only_whitespace_address(self):
        """Test address with only whitespace values."""
        data = {
            "address_line1": "",
            "city": "",
        }
        result = geocoding._compose_address(data)
        assert result is None


# =============================================================================
# Nominatim Search Tests
# =============================================================================


class TestNominatimSearch:
    """Tests for _nominatim_search function."""

    def test_successful_search(self, db_session):
        """Test successful nominatim search."""
        mock_response = MagicMock()
        mock_response.json.return_value = [
            {"lat": "40.7128", "lon": "-74.0060", "display_name": "New York"}
        ]
        mock_response.raise_for_status = MagicMock()

        with patch("app.services.geocoding.httpx.get", return_value=mock_response):
            results = geocoding._nominatim_search(db_session, "New York", 1)

        assert len(results) == 1
        assert results[0]["lat"] == "40.7128"

    def test_search_with_custom_settings(self, db_session):
        """Test search with custom settings from database."""
        # Set up custom settings
        db_session.add(DomainSetting(
            domain=SettingDomain.geocoding,
            key="base_url",
            value_text="https://custom.geocoder.com",
            is_active=True,
        ))
        db_session.add(DomainSetting(
            domain=SettingDomain.geocoding,
            key="user_agent",
            value_text="custom_agent",
            is_active=True,
        ))
        db_session.add(DomainSetting(
            domain=SettingDomain.geocoding,
            key="timeout_sec",
            value_text="15",
            is_active=True,
        ))
        db_session.add(DomainSetting(
            domain=SettingDomain.geocoding,
            key="email",
            value_text="test@example.com",
            is_active=True,
        ))
        db_session.commit()

        mock_response = MagicMock()
        mock_response.json.return_value = []
        mock_response.raise_for_status = MagicMock()

        with patch("app.services.geocoding.httpx.get", return_value=mock_response) as mock_get:
            geocoding._nominatim_search(db_session, "Test", 5)

        # Verify custom URL was used
        mock_get.assert_called_once()
        call_args = mock_get.call_args
        assert "custom.geocoder.com" in call_args[0][0]
        assert call_args[1]["headers"]["User-Agent"] == "custom_agent"
        assert call_args[1]["timeout"] == 15.0
        assert call_args[1]["params"]["email"] == "test@example.com"

    def test_http_error_raises_exception(self, db_session):
        """Test HTTP error raises HTTPException."""
        import httpx

        with patch("app.services.geocoding.httpx.get") as mock_get:
            mock_get.side_effect = httpx.HTTPError("Connection failed")

            with pytest.raises(HTTPException) as exc_info:
                geocoding._nominatim_search(db_session, "Test", 1)

            assert exc_info.value.status_code == 502
            assert "Geocoding request failed" in exc_info.value.detail

    def test_invalid_response_format_raises_exception(self, db_session):
        """Test invalid response format raises HTTPException."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"error": "something"}  # Not a list
        mock_response.raise_for_status = MagicMock()

        with patch("app.services.geocoding.httpx.get", return_value=mock_response):
            with pytest.raises(HTTPException) as exc_info:
                geocoding._nominatim_search(db_session, "Test", 1)

            assert exc_info.value.status_code == 502
            assert "Invalid geocoding response" in exc_info.value.detail

    def test_limit_minimum_is_one(self, db_session):
        """Test limit is at least 1."""
        mock_response = MagicMock()
        mock_response.json.return_value = []
        mock_response.raise_for_status = MagicMock()

        with patch("app.services.geocoding.httpx.get", return_value=mock_response) as mock_get:
            geocoding._nominatim_search(db_session, "Test", 0)

        call_args = mock_get.call_args
        assert call_args[1]["params"]["limit"] == 1


# =============================================================================
# geocode_address Tests
# =============================================================================


class TestGeocodeAddress:
    """Tests for geocode_address function."""

    def test_returns_data_when_already_has_coords(self, db_session):
        """Test returns data unchanged if already has coordinates."""
        data = {
            "address_line1": "123 Main St",
            "latitude": 40.7128,
            "longitude": -74.0060,
        }
        result = geocoding.geocode_address(db_session, data)
        assert result == data

    def test_returns_data_when_geocoding_disabled(self, db_session):
        """Test returns data unchanged if geocoding is disabled."""
        db_session.add(DomainSetting(
            domain=SettingDomain.geocoding,
            key="enabled",
            value_text="false",
            is_active=True,
        ))
        db_session.commit()

        data = {"address_line1": "123 Main St"}
        result = geocoding.geocode_address(db_session, data)
        assert result == data
        assert "latitude" not in result

    def test_returns_data_when_provider_not_nominatim(self, db_session):
        """Test returns data unchanged if provider is not nominatim."""
        db_session.add(DomainSetting(
            domain=SettingDomain.geocoding,
            key="provider",
            value_text="google",
            is_active=True,
        ))
        db_session.commit()

        data = {"address_line1": "123 Main St"}
        result = geocoding.geocode_address(db_session, data)
        assert result == data

    def test_returns_data_when_no_address(self, db_session):
        """Test returns data unchanged if no address to geocode."""
        data = {}
        result = geocoding.geocode_address(db_session, data)
        assert result == data

    def test_geocodes_address_successfully(self, db_session):
        """Test successfully geocodes an address."""
        mock_response = MagicMock()
        mock_response.json.return_value = [
            {"lat": "40.7128", "lon": "-74.0060"}
        ]
        mock_response.raise_for_status = MagicMock()

        data = {
            "address_line1": "123 Main St",
            "city": "New York",
            "region": "NY",
        }

        with patch("app.services.geocoding.httpx.get", return_value=mock_response):
            result = geocoding.geocode_address(db_session, data)

        assert result["latitude"] == 40.7128
        assert result["longitude"] == -74.0060

    def test_returns_data_when_no_results(self, db_session):
        """Test returns data unchanged when no geocoding results."""
        mock_response = MagicMock()
        mock_response.json.return_value = []
        mock_response.raise_for_status = MagicMock()

        data = {"address_line1": "Unknown Address 12345"}

        with patch("app.services.geocoding.httpx.get", return_value=mock_response):
            result = geocoding.geocode_address(db_session, data)

        assert "latitude" not in result
        assert "longitude" not in result

    def test_raises_exception_for_invalid_coords(self, db_session):
        """Test raises HTTPException for invalid coordinate values."""
        mock_response = MagicMock()
        mock_response.json.return_value = [
            {"lat": "not_a_number", "lon": "-74.0060"}
        ]
        mock_response.raise_for_status = MagicMock()

        data = {"address_line1": "123 Main St"}

        with patch("app.services.geocoding.httpx.get", return_value=mock_response):
            with pytest.raises(HTTPException) as exc_info:
                geocoding.geocode_address(db_session, data)

            assert exc_info.value.status_code == 502


# =============================================================================
# geocode_preview Tests
# =============================================================================


class TestGeocodePreview:
    """Tests for geocode_preview function."""

    def test_returns_empty_when_disabled(self, db_session):
        """Test returns empty list when geocoding is disabled."""
        db_session.add(DomainSetting(
            domain=SettingDomain.geocoding,
            key="enabled",
            value_text="false",
            is_active=True,
        ))
        db_session.commit()

        data = {"address_line1": "123 Main St"}
        result = geocoding.geocode_preview(db_session, data)
        assert result == []

    def test_returns_empty_when_provider_not_nominatim(self, db_session):
        """Test returns empty list when provider is not nominatim."""
        db_session.add(DomainSetting(
            domain=SettingDomain.geocoding,
            key="provider",
            value_text="mapbox",
            is_active=True,
        ))
        db_session.commit()

        data = {"address_line1": "123 Main St"}
        result = geocoding.geocode_preview(db_session, data)
        assert result == []

    def test_raises_exception_when_no_address(self, db_session):
        """Test raises HTTPException when no address provided."""
        data = {}

        with pytest.raises(HTTPException) as exc_info:
            geocoding.geocode_preview(db_session, data)

        assert exc_info.value.status_code == 400
        assert "Address fields required" in exc_info.value.detail

    def test_returns_preview_results(self, db_session):
        """Test returns formatted preview results."""
        mock_response = MagicMock()
        mock_response.json.return_value = [
            {
                "lat": "40.7128",
                "lon": "-74.0060",
                "display_name": "New York, NY",
                "class": "place",
                "type": "city",
                "importance": 0.9,
            },
            {
                "lat": "40.7580",
                "lon": "-73.9855",
                "display_name": "Times Square, New York",
                "class": "place",
                "type": "square",
                "importance": 0.8,
            },
        ]
        mock_response.raise_for_status = MagicMock()

        data = {"address_line1": "New York"}

        with patch("app.services.geocoding.httpx.get", return_value=mock_response):
            result = geocoding.geocode_preview(db_session, data, limit=3)

        assert len(result) == 2
        assert result[0]["display_name"] == "New York, NY"
        assert result[0]["latitude"] == 40.7128
        assert result[0]["longitude"] == -74.0060
        assert result[0]["class"] == "place"
        assert result[0]["type"] == "city"
        assert result[0]["importance"] == 0.9

    def test_skips_invalid_results(self, db_session):
        """Test skips results with invalid coordinates."""
        mock_response = MagicMock()
        mock_response.json.return_value = [
            {"lat": "40.7128", "lon": "-74.0060", "display_name": "Valid"},
            {"lat": "invalid", "lon": "-74.0060", "display_name": "Invalid"},
            {"lat": None, "lon": "-74.0060", "display_name": "Null lat"},
        ]
        mock_response.raise_for_status = MagicMock()

        data = {"address_line1": "Test"}

        with patch("app.services.geocoding.httpx.get", return_value=mock_response):
            result = geocoding.geocode_preview(db_session, data)

        assert len(result) == 1
        assert result[0]["display_name"] == "Valid"


# =============================================================================
# geocode_preview_from_request Tests
# =============================================================================


class TestGeocodePreviewFromRequest:
    """Tests for geocode_preview_from_request function."""

    def test_extracts_data_from_payload(self, db_session):
        """Test extracts data from request payload."""
        mock_payload = MagicMock()
        mock_payload.model_dump.return_value = {
            "address_line1": "123 Main St",
            "city": "Boston",
            "limit": 5,
        }
        mock_payload.limit = 5

        mock_response = MagicMock()
        mock_response.json.return_value = [
            {"lat": "42.3601", "lon": "-71.0589", "display_name": "Boston"}
        ]
        mock_response.raise_for_status = MagicMock()

        with patch("app.services.geocoding.httpx.get", return_value=mock_response):
            result = geocoding.geocode_preview_from_request(db_session, mock_payload)

        assert len(result) == 1
        mock_payload.model_dump.assert_called_once_with(exclude={"limit"})

    def test_uses_default_limit_when_none(self, db_session):
        """Test uses default limit when payload.limit is None."""
        mock_payload = MagicMock()
        mock_payload.model_dump.return_value = {
            "address_line1": "123 Main St",
        }
        mock_payload.limit = None

        mock_response = MagicMock()
        mock_response.json.return_value = []
        mock_response.raise_for_status = MagicMock()

        with patch("app.services.geocoding.httpx.get", return_value=mock_response) as mock_get:
            geocoding.geocode_preview_from_request(db_session, mock_payload)

        # Verify default limit of 3 was used
        call_args = mock_get.call_args
        assert call_args[1]["params"]["limit"] == 3
