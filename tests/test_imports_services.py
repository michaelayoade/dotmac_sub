"""Tests for imports service."""

from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException, UploadFile

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.services import imports as imports_service


# =============================================================================
# Helper Function Tests
# =============================================================================


class TestImportsIntSetting:
    """Tests for _imports_int_setting function."""

    def test_returns_default_when_no_setting(self, db_session):
        """Test returns default when setting not found."""
        result = imports_service._imports_int_setting(db_session, "nonexistent", 100)
        assert result == 100

    def test_returns_value_text_as_int(self, db_session):
        """Test returns value_text parsed as int."""
        setting = DomainSetting(
            domain=SettingDomain.imports,
            key="max_rows",
            value_type=SettingValueType.string,
            value_text="500",
            is_active=True,
        )
        db_session.add(setting)
        db_session.commit()

        result = imports_service._imports_int_setting(db_session, "max_rows", 100)
        assert result == 500

    def test_returns_default_on_parse_error(self, db_session):
        """Test returns default when value cannot be parsed."""
        setting = DomainSetting(
            domain=SettingDomain.imports,
            key="bad_value",
            value_type=SettingValueType.string,
            value_text="not-a-number",
            is_active=True,
        )
        db_session.add(setting)
        db_session.commit()

        result = imports_service._imports_int_setting(db_session, "bad_value", 100)
        assert result == 100

    def test_returns_default_for_json_values(self, db_session):
        """Test returns value_json when value_text is None."""
        setting = DomainSetting(
            domain=SettingDomain.imports,
            key="json_value",
            value_type=SettingValueType.json,
            value_json=200,
            is_active=True,
        )
        db_session.add(setting)
        db_session.commit()

        result = imports_service._imports_int_setting(db_session, "json_value", 100)
        assert result == 200

    def test_ignores_inactive_settings(self, db_session):
        """Test ignores inactive settings."""
        setting = DomainSetting(
            domain=SettingDomain.imports,
            key="inactive_key",
            value_type=SettingValueType.string,
            value_text="999",
            is_active=False,
        )
        db_session.add(setting)
        db_session.commit()

        result = imports_service._imports_int_setting(db_session, "inactive_key", 100)
        assert result == 100


# =============================================================================
# Import Function Tests
# =============================================================================


class TestImportSubscriberCustomFieldsFromCsv:
    """Tests for import_subscriber_custom_fields_from_csv function."""

    def test_returns_counts(self, db_session):
        """Test returns created count and errors."""
        csv_content = "subscriber_id,field_key,field_value\n"

        with patch.object(imports_service, "load_csv_content") as mock_load:
            mock_load.return_value = ([], [])

            created, errors = imports_service.import_subscriber_custom_fields_from_csv(
                db_session, csv_content
            )

            assert created == 0
            assert errors == []

    def test_handles_row_errors(self, db_session):
        """Test captures row parsing errors."""
        csv_content = "subscriber_id,field_key,field_value\n"

        mock_error = MagicMock()
        mock_error.index = 1
        mock_error.detail = "Invalid row"

        with patch.object(imports_service, "load_csv_content") as mock_load:
            mock_load.return_value = ([], [mock_error])

            created, errors = imports_service.import_subscriber_custom_fields_from_csv(
                db_session, csv_content
            )

            assert created == 0
            assert len(errors) == 1
            assert errors[0]["index"] == 1
            assert errors[0]["detail"] == "Invalid row"


class TestImportSubscriberCustomFieldsUpload:
    """Tests for import_subscriber_custom_fields_upload function."""

    def test_rejects_non_csv_file(self, db_session):
        """Test rejects non-CSV files."""
        mock_file = MagicMock(spec=UploadFile)
        mock_file.filename = "data.xlsx"

        with pytest.raises(HTTPException) as exc_info:
            imports_service.import_subscriber_custom_fields_upload(db_session, mock_file)

        assert exc_info.value.status_code == 400
        assert "CSV file required" in exc_info.value.detail

    def test_rejects_empty_filename(self, db_session):
        """Test rejects empty filename."""
        mock_file = MagicMock(spec=UploadFile)
        mock_file.filename = None

        with pytest.raises(HTTPException) as exc_info:
            imports_service.import_subscriber_custom_fields_upload(db_session, mock_file)

        assert exc_info.value.status_code == 400

    def test_rejects_large_file(self, db_session):
        """Test rejects file exceeding max size."""
        large_content = b"x" * (6 * 1024 * 1024)  # 6MB
        mock_file = MagicMock(spec=UploadFile)
        mock_file.filename = "data.csv"
        mock_file.file = BytesIO(large_content)

        with pytest.raises(HTTPException) as exc_info:
            imports_service.import_subscriber_custom_fields_upload(db_session, mock_file)

        assert exc_info.value.status_code == 413
        assert "too large" in exc_info.value.detail

    def test_rejects_invalid_utf8(self, db_session):
        """Test rejects invalid UTF-8 content."""
        invalid_content = b"\xff\xfe"  # Invalid UTF-8
        mock_file = MagicMock(spec=UploadFile)
        mock_file.filename = "data.csv"
        mock_file.file = BytesIO(invalid_content)

        with pytest.raises(HTTPException) as exc_info:
            imports_service.import_subscriber_custom_fields_upload(db_session, mock_file)

        assert exc_info.value.status_code == 400
        assert "Invalid UTF-8" in exc_info.value.detail

    def test_successful_upload(self, db_session):
        """Test successful CSV upload."""
        csv_content = b"subscriber_id,field_key,field_value\n"
        mock_file = MagicMock(spec=UploadFile)
        mock_file.filename = "data.csv"
        mock_file.file = BytesIO(csv_content)

        with patch.object(
            imports_service, "import_subscriber_custom_fields_from_csv"
        ) as mock_import:
            mock_import.return_value = (5, [])

            result = imports_service.import_subscriber_custom_fields_upload(
                db_session, mock_file
            )

            assert result["created"] == 5
            assert result["errors"] == []
            assert result["error_count"] == 0

    def test_raises_on_row_limit_exceeded(self, db_session):
        """Test raises when row limit exceeded."""
        csv_content = b"subscriber_id,field_key,field_value\n"
        mock_file = MagicMock(spec=UploadFile)
        mock_file.filename = "data.csv"
        mock_file.file = BytesIO(csv_content)

        with patch.object(
            imports_service, "import_subscriber_custom_fields_from_csv"
        ) as mock_import:
            mock_import.return_value = (0, [{"detail": "Row limit exceeded"}])

            with pytest.raises(HTTPException) as exc_info:
                imports_service.import_subscriber_custom_fields_upload(
                    db_session, mock_file
                )

            assert exc_info.value.status_code == 400
            assert "row limit exceeded" in exc_info.value.detail.lower()
