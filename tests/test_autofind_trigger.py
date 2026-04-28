"""Tests for autofind trigger service."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.models.network import OLTDevice
from app.services.autofind_trigger import (
    AUTOFIND_COOLDOWN_SECONDS,
    AutofindTriggerResult,
    find_olt_by_id,
    find_olt_by_ip,
    find_olt_by_name,
    is_in_cooldown,
    set_cooldown,
    trigger_autofind,
    trigger_autofind_by_identifier,
    trigger_autofind_by_ip,
)


@pytest.fixture
def sample_olt(db_session):
    """Create a sample OLT for testing."""
    olt = OLTDevice(
        name="Test-OLT-Trigger",
        mgmt_ip="10.0.0.100",
        is_active=True,
    )
    db_session.add(olt)
    db_session.commit()
    return olt


class TestOltLookup:
    """Tests for OLT lookup functions."""

    def test_find_olt_by_ip_found(self, db_session, sample_olt):
        """Test finding OLT by IP address."""
        result = find_olt_by_ip(db_session, "10.0.0.100")
        assert result is not None
        assert result.id == sample_olt.id

    def test_find_olt_by_ip_not_found(self, db_session):
        """Test finding OLT by IP when not exists."""
        result = find_olt_by_ip(db_session, "192.168.1.1")
        assert result is None

    def test_find_olt_by_ip_inactive(self, db_session):
        """Test that inactive OLTs are not found."""
        olt = OLTDevice(
            name="Inactive-OLT",
            mgmt_ip="10.0.0.101",
            is_active=False,
        )
        db_session.add(olt)
        db_session.commit()

        result = find_olt_by_ip(db_session, "10.0.0.101")
        assert result is None

    def test_find_olt_by_name_found(self, db_session, sample_olt):
        """Test finding OLT by name."""
        result = find_olt_by_name(db_session, "Test-OLT-Trigger")
        assert result is not None
        assert result.id == sample_olt.id

    def test_find_olt_by_name_case_insensitive(self, db_session, sample_olt):
        """Test that name lookup is case-insensitive."""
        result = find_olt_by_name(db_session, "test-olt-trigger")
        assert result is not None
        assert result.id == sample_olt.id

    def test_find_olt_by_name_not_found(self, db_session):
        """Test finding OLT by name when not exists."""
        result = find_olt_by_name(db_session, "NonExistent-OLT")
        assert result is None

    def test_find_olt_by_id_found(self, db_session, sample_olt):
        """Test finding OLT by UUID."""
        result = find_olt_by_id(db_session, str(sample_olt.id))
        assert result is not None
        assert result.id == sample_olt.id

    def test_find_olt_by_id_invalid_uuid(self, db_session):
        """Test finding OLT with invalid UUID."""
        result = find_olt_by_id(db_session, "not-a-uuid")
        assert result is None

    def test_find_olt_by_id_not_found(self, db_session):
        """Test finding OLT by UUID when not exists."""
        result = find_olt_by_id(db_session, "00000000-0000-0000-0000-000000000000")
        assert result is None


class TestCooldown:
    """Tests for cooldown mechanism."""

    def test_cooldown_not_set_initially(self):
        """Test that OLT is not in cooldown initially."""
        with patch("app.services.autofind_trigger.safe_get", return_value=None):
            assert is_in_cooldown("test-olt-id") is False

    def test_cooldown_set(self):
        """Test setting cooldown."""
        with patch("app.services.autofind_trigger.safe_set", return_value=True) as mock:
            result = set_cooldown("test-olt-id", 60)
            assert result is True
            mock.assert_called_once()
            call_args = mock.call_args
            assert "autofind:cooldown:test-olt-id" in call_args[0][0]
            assert call_args[1]["ttl"] == 60

    def test_cooldown_is_set(self):
        """Test that cooldown check returns True when set."""
        with patch("app.services.autofind_trigger.safe_get", return_value="1"):
            assert is_in_cooldown("test-olt-id") is True


class TestTriggerAutofind:
    """Tests for trigger_autofind function."""

    def test_trigger_autofind_success(self):
        """Test successful autofind trigger."""
        mock_dispatch = MagicMock()
        mock_dispatch.queued = True
        mock_dispatch.task_id = "task-123"

        with (
            patch("app.services.autofind_trigger.is_in_cooldown", return_value=False),
            patch("app.services.autofind_trigger.set_cooldown", return_value=True),
            patch("app.services.autofind_trigger.enqueue_task", return_value=mock_dispatch),
        ):
            result = trigger_autofind("olt-uuid-123", "Test OLT", "test")

            assert result.triggered is True
            assert result.olt_id == "olt-uuid-123"
            assert result.olt_name == "Test OLT"
            assert result.task_id == "task-123"

    def test_trigger_autofind_in_cooldown(self):
        """Test autofind trigger skipped due to cooldown."""
        with patch("app.services.autofind_trigger.is_in_cooldown", return_value=True):
            result = trigger_autofind("olt-uuid-123", "Test OLT", "test")

            assert result.triggered is False
            assert "cooldown" in result.reason.lower()

    def test_trigger_autofind_force_bypasses_cooldown(self):
        """Test that force=True bypasses cooldown check."""
        mock_dispatch = MagicMock()
        mock_dispatch.queued = True
        mock_dispatch.task_id = "task-456"

        with (
            patch("app.services.autofind_trigger.is_in_cooldown", return_value=True) as cooldown_mock,
            patch("app.services.autofind_trigger.set_cooldown", return_value=True),
            patch("app.services.autofind_trigger.enqueue_task", return_value=mock_dispatch),
        ):
            result = trigger_autofind("olt-uuid-123", "Test OLT", "test", force=True)

            assert result.triggered is True
            # Cooldown check should not be called when force=True
            cooldown_mock.assert_not_called()

    def test_trigger_autofind_queue_failure(self):
        """Test autofind trigger when queue fails."""
        mock_dispatch = MagicMock()
        mock_dispatch.queued = False
        mock_dispatch.error = "Queue full"

        with (
            patch("app.services.autofind_trigger.is_in_cooldown", return_value=False),
            patch("app.services.autofind_trigger.set_cooldown", return_value=True),
            patch("app.services.autofind_trigger.enqueue_task", return_value=mock_dispatch),
        ):
            result = trigger_autofind("olt-uuid-123", "Test OLT", "test")

            assert result.triggered is False
            assert "Queue full" in result.reason


class TestTriggerAutofindByIp:
    """Tests for trigger_autofind_by_ip function."""

    def test_trigger_by_ip_success(self, db_session, sample_olt):
        """Test triggering autofind by IP address."""
        mock_dispatch = MagicMock()
        mock_dispatch.queued = True
        mock_dispatch.task_id = "task-789"

        with (
            patch("app.services.autofind_trigger.is_in_cooldown", return_value=False),
            patch("app.services.autofind_trigger.set_cooldown", return_value=True),
            patch("app.services.autofind_trigger.enqueue_task", return_value=mock_dispatch),
        ):
            result = trigger_autofind_by_ip(db_session, "10.0.0.100", "syslog")

            assert result.triggered is True
            assert result.olt_id == str(sample_olt.id)
            assert result.olt_name == "Test-OLT-Trigger"

    def test_trigger_by_ip_not_found(self, db_session):
        """Test triggering autofind for unknown IP."""
        result = trigger_autofind_by_ip(db_session, "192.168.99.99", "syslog")

        assert result.triggered is False
        assert "No active OLT found" in result.reason


class TestTriggerAutofindByIdentifier:
    """Tests for trigger_autofind_by_identifier function."""

    def test_trigger_by_uuid(self, db_session, sample_olt):
        """Test triggering autofind by UUID."""
        mock_dispatch = MagicMock()
        mock_dispatch.queued = True
        mock_dispatch.task_id = "task-abc"

        with (
            patch("app.services.autofind_trigger.is_in_cooldown", return_value=False),
            patch("app.services.autofind_trigger.set_cooldown", return_value=True),
            patch("app.services.autofind_trigger.enqueue_task", return_value=mock_dispatch),
        ):
            result = trigger_autofind_by_identifier(
                db_session, str(sample_olt.id), "webhook"
            )

            assert result.triggered is True
            assert result.olt_id == str(sample_olt.id)

    def test_trigger_by_ip_identifier(self, db_session, sample_olt):
        """Test triggering autofind by IP as identifier."""
        mock_dispatch = MagicMock()
        mock_dispatch.queued = True
        mock_dispatch.task_id = "task-def"

        with (
            patch("app.services.autofind_trigger.is_in_cooldown", return_value=False),
            patch("app.services.autofind_trigger.set_cooldown", return_value=True),
            patch("app.services.autofind_trigger.enqueue_task", return_value=mock_dispatch),
        ):
            result = trigger_autofind_by_identifier(db_session, "10.0.0.100", "webhook")

            assert result.triggered is True
            assert result.olt_id == str(sample_olt.id)

    def test_trigger_by_name_identifier(self, db_session, sample_olt):
        """Test triggering autofind by name as identifier."""
        mock_dispatch = MagicMock()
        mock_dispatch.queued = True
        mock_dispatch.task_id = "task-ghi"

        with (
            patch("app.services.autofind_trigger.is_in_cooldown", return_value=False),
            patch("app.services.autofind_trigger.set_cooldown", return_value=True),
            patch("app.services.autofind_trigger.enqueue_task", return_value=mock_dispatch),
        ):
            result = trigger_autofind_by_identifier(
                db_session, "Test-OLT-Trigger", "webhook"
            )

            assert result.triggered is True
            assert result.olt_id == str(sample_olt.id)

    def test_trigger_by_identifier_not_found(self, db_session):
        """Test triggering autofind for unknown identifier."""
        result = trigger_autofind_by_identifier(db_session, "unknown-olt", "webhook")

        assert result.triggered is False
        assert "No active OLT found" in result.reason


class TestAutofindTriggerResult:
    """Tests for AutofindTriggerResult dataclass."""

    def test_to_dict(self):
        """Test conversion to dictionary."""
        result = AutofindTriggerResult(
            triggered=True,
            olt_id="uuid-123",
            olt_name="Test OLT",
            task_id="task-xyz",
        )
        d = result.to_dict()

        assert d["triggered"] is True
        assert d["olt_id"] == "uuid-123"
        assert d["olt_name"] == "Test OLT"
        assert d["task_id"] == "task-xyz"
        assert d["reason"] is None

    def test_to_dict_with_reason(self):
        """Test conversion with failure reason."""
        result = AutofindTriggerResult(
            triggered=False,
            reason="OLT not found",
        )
        d = result.to_dict()

        assert d["triggered"] is False
        assert d["reason"] == "OLT not found"
