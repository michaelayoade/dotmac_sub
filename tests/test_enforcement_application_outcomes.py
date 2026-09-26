"""Outcome coverage for the per-NAS enforcement helpers (ADR-0017 §4).

Exercises ``_enforce_address_list_on_nas`` and ``_api_kick_session`` against
the ``_record_enforcement_application`` writer seam (patched out so these are
pure unit tests of the outcome classification, not the writer itself — see
``tests/test_enforcement_application_writer.py`` for that).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi import HTTPException
from routeros_api.exceptions import RouterOsApiCommunicationError

from app.models.catalog import NasDevice, NasVendor
from app.models.enforcement_application import (
    EnforcementEffect,
    EnforcementFailureClass,
    EnforcementOutcomeValue,
    EnforcementPath,
)
from app.services.enforcement import _api_kick_session, _enforce_address_list_on_nas

_ROUTEROS_LOGIN_REJECTION = RouterOsApiCommunicationError(
    'Error "invalid user name or password (6)" executing command '
    "b'/login =name=X =password=SECRET .tag=1'",
    b"invalid user name or password (6)",
)


def _mikrotik_device() -> NasDevice:
    nas_device = MagicMock(spec=NasDevice)
    nas_device.vendor = NasVendor.mikrotik
    nas_device.name = "BNG-1"
    nas_device.id = uuid4()
    return nas_device


def _working_ssh_cm() -> MagicMock:
    ssh_cm = MagicMock()
    ssh_cm.__enter__.return_value = MagicMock()
    ssh_cm.__exit__.return_value = False
    return ssh_cm


_SSH_NOT_CONFIGURED = HTTPException(
    status_code=400, detail="Device has no SSH credentials"
)


class TestEnforceAddressListOnNas:
    def test_ssh_ok_records_applied_ssh_and_returns_true(self):
        db = MagicMock()
        nas_device = _mikrotik_device()
        sub_id = uuid4()

        with (
            patch(
                "app.services.enforcement.DeviceProvisioner.ssh_session",
                return_value=_working_ssh_cm(),
            ),
            patch("app.services.enforcement._record_enforcement_application") as record,
        ):
            result = _enforce_address_list_on_nas(
                db, nas_device, "blocked", "10.0.0.5", add=True, subscription_id=sub_id
            )

        assert result is True
        record.assert_called_once()
        kwargs = record.call_args.kwargs
        assert kwargs["subscription_id"] == sub_id
        assert kwargs["nas_device_id"] == nas_device.id
        assert kwargs["effect"] == EnforcementEffect.address_list_block
        outcome = kwargs["outcome"]
        assert outcome.outcome == EnforcementOutcomeValue.applied
        assert outcome.path == EnforcementPath.ssh
        assert outcome.failure_class is None

    def test_ssh_not_configured_and_no_api_creds_is_not_applicable(self):
        """The API-only-and-unconfigured case must NOT be recorded as a failure."""
        db = MagicMock()
        nas_device = _mikrotik_device()
        sub_id = uuid4()

        with (
            patch(
                "app.services.enforcement.DeviceProvisioner.ssh_session",
                side_effect=_SSH_NOT_CONFIGURED,
            ),
            patch("app.services.enforcement._nas_with_api_creds", return_value=None),
            patch("app.services.enforcement._record_enforcement_application") as record,
        ):
            result = _enforce_address_list_on_nas(
                db, nas_device, "blocked", "10.0.0.5", add=True, subscription_id=sub_id
            )

        assert result is False
        record.assert_called_once()
        outcome = record.call_args.kwargs["outcome"]
        assert outcome.outcome == EnforcementOutcomeValue.not_applicable
        assert outcome.failure_class is None

    def test_ssh_not_configured_then_api_auth_rejection_records_failed_api(self):
        db = MagicMock()
        nas_device = _mikrotik_device()
        api_dev = _mikrotik_device()
        sub_id = uuid4()

        with (
            patch(
                "app.services.enforcement.DeviceProvisioner.ssh_session",
                side_effect=_SSH_NOT_CONFIGURED,
            ),
            patch("app.services.enforcement._nas_with_api_creds", return_value=api_dev),
            patch(
                "app.services.nas._mikrotik.apply_mikrotik_address_list_via_api",
                side_effect=_ROUTEROS_LOGIN_REJECTION,
            ),
            patch("app.services.enforcement._record_enforcement_application") as record,
        ):
            result = _enforce_address_list_on_nas(
                db, nas_device, "blocked", "10.0.0.5", add=True, subscription_id=sub_id
            )

        assert result is False
        outcome = record.call_args.kwargs["outcome"]
        assert outcome.outcome == EnforcementOutcomeValue.failed
        assert outcome.failure_class == EnforcementFailureClass.auth_rejected
        assert outcome.path == EnforcementPath.api

    def test_real_ssh_transport_failure_is_kept_over_no_api_creds(self):
        """ADR-0017 §4: a real SSH failure is never overwritten by
        not_applicable just because the API fallback isn't configured."""
        db = MagicMock()
        nas_device = _mikrotik_device()
        sub_id = uuid4()
        import errno

        with (
            patch(
                "app.services.enforcement.DeviceProvisioner.ssh_session",
                side_effect=OSError(errno.EHOSTUNREACH, "no route to host"),
            ),
            patch("app.services.enforcement._nas_with_api_creds", return_value=None),
            patch("app.services.enforcement._record_enforcement_application") as record,
        ):
            result = _enforce_address_list_on_nas(
                db, nas_device, "blocked", "10.0.0.5", add=True, subscription_id=sub_id
            )

        assert result is False
        outcome = record.call_args.kwargs["outcome"]
        assert outcome.outcome == EnforcementOutcomeValue.failed
        assert outcome.failure_class == EnforcementFailureClass.unreachable
        assert outcome.path == EnforcementPath.ssh

    def test_api_ok_records_applied_api_and_returns_true(self):
        db = MagicMock()
        nas_device = _mikrotik_device()
        api_dev = _mikrotik_device()
        sub_id = uuid4()

        with (
            patch(
                "app.services.enforcement.DeviceProvisioner.ssh_session",
                side_effect=_SSH_NOT_CONFIGURED,
            ),
            patch("app.services.enforcement._nas_with_api_creds", return_value=api_dev),
            patch(
                "app.services.nas._mikrotik.apply_mikrotik_address_list_via_api",
                return_value=True,
            ),
            patch("app.services.enforcement._record_enforcement_application") as record,
        ):
            result = _enforce_address_list_on_nas(
                db, nas_device, "blocked", "10.0.0.5", add=True, subscription_id=sub_id
            )

        assert result is True
        outcome = record.call_args.kwargs["outcome"]
        assert outcome.outcome == EnforcementOutcomeValue.applied
        assert outcome.path == EnforcementPath.api
        assert outcome.failure_class is None

    def test_no_subscription_id_skips_recording_but_returns_same_value(self):
        db = MagicMock()
        nas_device = _mikrotik_device()

        with (
            patch(
                "app.services.enforcement.DeviceProvisioner.ssh_session",
                return_value=_working_ssh_cm(),
            ),
            patch("app.services.enforcement._record_enforcement_application") as record,
        ):
            result = _enforce_address_list_on_nas(
                db, nas_device, "blocked", "10.0.0.5", add=True, subscription_id=None
            )

        assert result is True
        record.assert_not_called()

    def test_unblock_records_address_list_unblock_effect(self):
        db = MagicMock()
        nas_device = _mikrotik_device()
        sub_id = uuid4()

        with (
            patch(
                "app.services.enforcement.DeviceProvisioner.ssh_session",
                return_value=_working_ssh_cm(),
            ),
            patch("app.services.enforcement._record_enforcement_application") as record,
        ):
            result = _enforce_address_list_on_nas(
                db,
                nas_device,
                "blocked",
                "10.0.0.5",
                add=False,
                subscription_id=sub_id,
            )

        assert result is True
        kwargs = record.call_args.kwargs
        assert kwargs["effect"] == EnforcementEffect.address_list_unblock


class TestApiKickSession:
    def test_non_mikrotik_is_not_applicable(self):
        db = MagicMock()
        nas_device = MagicMock(spec=NasDevice)
        nas_device.vendor = NasVendor.cisco
        nas_device.id = uuid4()
        sub_id = uuid4()

        with patch(
            "app.services.enforcement._record_enforcement_application"
        ) as record:
            result = _api_kick_session(db, nas_device, "alice", subscription_id=sub_id)

        assert result is False
        outcome = record.call_args.kwargs["outcome"]
        assert outcome.outcome == EnforcementOutcomeValue.not_applicable
        assert outcome.failure_class is None

    def test_api_raising_records_failed_with_its_classified_class(self):
        db = MagicMock()
        nas_device = _mikrotik_device()
        api_dev = _mikrotik_device()
        sub_id = uuid4()

        with (
            patch("app.services.enforcement._nas_with_api_creds", return_value=api_dev),
            patch(
                "app.services.nas._mikrotik.disconnect_mikrotik_pppoe_bulk",
                side_effect=TimeoutError(),
            ),
            patch("app.services.enforcement._record_enforcement_application") as record,
        ):
            result = _api_kick_session(db, nas_device, "alice", subscription_id=sub_id)

        assert result is False
        outcome = record.call_args.kwargs["outcome"]
        assert outcome.outcome == EnforcementOutcomeValue.failed
        assert outcome.failure_class == EnforcementFailureClass.timeout
        assert outcome.path == EnforcementPath.api

    def test_api_returning_no_confirmation_records_failed_not_applied(self):
        """The read-back returned nothing and nothing raised: the session is
        still live. That must never be recorded as applied."""
        db = MagicMock()
        nas_device = _mikrotik_device()
        api_dev = _mikrotik_device()
        sub_id = uuid4()

        with (
            patch("app.services.enforcement._nas_with_api_creds", return_value=api_dev),
            patch(
                "app.services.nas._mikrotik.disconnect_mikrotik_pppoe_bulk",
                return_value=set(),
            ),
            patch("app.services.enforcement._record_enforcement_application") as record,
        ):
            result = _api_kick_session(db, nas_device, "alice", subscription_id=sub_id)

        assert result is False
        outcome = record.call_args.kwargs["outcome"]
        assert outcome.outcome == EnforcementOutcomeValue.failed
        assert outcome.failure_class == EnforcementFailureClass.command_failed
        assert outcome.path == EnforcementPath.api
        assert outcome.detail == "session_kick_unconfirmed 1/1"

    def test_api_success_records_applied_api(self):
        db = MagicMock()
        nas_device = _mikrotik_device()
        api_dev = _mikrotik_device()
        sub_id = uuid4()

        with (
            patch("app.services.enforcement._nas_with_api_creds", return_value=api_dev),
            patch(
                "app.services.nas._mikrotik.disconnect_mikrotik_pppoe_bulk",
                return_value={"alice"},
            ),
            patch("app.services.enforcement._record_enforcement_application") as record,
        ):
            result = _api_kick_session(db, nas_device, "alice", subscription_id=sub_id)

        assert result is True
        outcome = record.call_args.kwargs["outcome"]
        assert outcome.outcome == EnforcementOutcomeValue.applied
        assert outcome.path == EnforcementPath.api


class TestSshKickTierPrecedence:
    """The SSH tier's final-outcome rules (ADR-0017 section 4), tested on the
    pure helper both kick paths use, so a tier-precedence regression cannot
    hide inside the large disconnect/update functions."""

    def _auth_rejected(self):
        from app.services.enforcement import EnforcementOutcome

        return EnforcementOutcome.failed_from(
            _ROUTEROS_LOGIN_REJECTION, path=EnforcementPath.api
        )

    def test_ssh_success_supersedes_an_earlier_api_failure(self):
        from app.services.enforcement import _ssh_kick_outcome

        outcome = _ssh_kick_outcome(
            self._auth_rejected(), ssh_possible=True, kicked=2, remaining=2, targeted=2
        )
        assert outcome.outcome == EnforcementOutcomeValue.applied
        assert outcome.path == EnforcementPath.ssh

    def test_a_silent_ssh_failure_keeps_the_classified_api_cause(self):
        """The Eagle case: the API rejected the login and the SSH fallback
        failed without an exception. auth_rejected is the actionable cause."""
        from app.services.enforcement import _ssh_kick_outcome

        prior = self._auth_rejected()
        assert prior.failure_class == EnforcementFailureClass.auth_rejected
        outcome = _ssh_kick_outcome(
            prior, ssh_possible=True, kicked=0, remaining=1, targeted=1
        )
        assert outcome == prior

    def test_ssh_not_possible_with_no_earlier_failure_is_not_applicable(self):
        from app.services.enforcement import EnforcementOutcome, _ssh_kick_outcome

        for prior in (None, EnforcementOutcome.not_applicable("no_api_credentials")):
            outcome = _ssh_kick_outcome(
                prior, ssh_possible=False, kicked=0, remaining=1, targeted=1
            )
            assert outcome.outcome == EnforcementOutcomeValue.not_applicable
            assert outcome.failure_class is None

    def test_ssh_ran_and_left_sessions_with_no_earlier_failure_is_failed(self):
        from app.services.enforcement import _ssh_kick_outcome

        outcome = _ssh_kick_outcome(
            None, ssh_possible=True, kicked=1, remaining=3, targeted=4
        )
        assert outcome.outcome == EnforcementOutcomeValue.failed
        assert outcome.failure_class == EnforcementFailureClass.command_failed
        assert outcome.detail == "session_kick_unconfirmed 2/4"


class TestSshKickPossible:
    def _device(self, *, ssh_username="admin", vendor=NasVendor.mikrotik):
        device = _mikrotik_device()
        device.vendor = vendor
        device.ssh_username = ssh_username
        return device

    def test_kill_control_disabled_means_ssh_cannot_run(self):
        from app.services.enforcement import _ssh_kick_possible

        with patch(
            "app.services.enforcement._mikrotik_kill_enabled", return_value=False
        ):
            assert _ssh_kick_possible(MagicMock(), self._device(), "alice") is False

    def test_missing_username_or_ssh_or_non_mikrotik_means_ssh_cannot_run(self):
        from app.services.enforcement import _ssh_kick_possible

        with patch(
            "app.services.enforcement._mikrotik_kill_enabled", return_value=True
        ):
            assert _ssh_kick_possible(MagicMock(), self._device(), None) is False
            assert (
                _ssh_kick_possible(
                    MagicMock(), self._device(ssh_username=None), "alice"
                )
                is False
            )
            assert (
                _ssh_kick_possible(
                    MagicMock(), self._device(vendor=NasVendor.huawei), "alice"
                )
                is False
            )
            # Sensitivity: the fully configured case really is possible.
            assert _ssh_kick_possible(MagicMock(), self._device(), "alice") is True


class TestTimeLimitPropagatesThroughRecording:
    def test_a_time_limit_while_recording_an_api_success_is_not_swallowed(self):
        """Recording happens outside the helper's try blocks, so a Celery soft
        time limit raised by the writer reaches the task instead of being
        caught as an enforcement failure (ADR-0017 section 7)."""
        import pytest
        from billiard.exceptions import SoftTimeLimitExceeded

        db = MagicMock()
        nas_device = _mikrotik_device()
        api_dev = _mikrotik_device()
        no_ssh = MagicMock()
        no_ssh.__enter__.side_effect = HTTPException(
            status_code=400, detail="Device has no SSH credentials"
        )

        with (
            patch(
                "app.services.enforcement.DeviceProvisioner.ssh_session",
                return_value=no_ssh,
            ),
            patch("app.services.enforcement._nas_with_api_creds", return_value=api_dev),
            patch(
                "app.services.nas._mikrotik.apply_mikrotik_address_list_via_api",
                return_value=True,
            ),
            patch(
                "app.services.enforcement._record_enforcement_application",
                side_effect=SoftTimeLimitExceeded(),
            ),
            pytest.raises(SoftTimeLimitExceeded),
        ):
            _enforce_address_list_on_nas(
                db,
                nas_device,
                "suspended",
                "10.0.0.1",
                add=True,
                subscription_id=uuid4(),
            )
