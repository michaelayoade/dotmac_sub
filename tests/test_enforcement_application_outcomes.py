"""Outcome coverage for the per-NAS enforcement helpers (ADR-0017 §4).

Exercises ``_enforce_address_list_on_nas`` and ``_api_kick_session`` against
the ``_record_enforcement_application`` writer seam (patched out so these are
pure unit tests of the outcome classification, not the writer itself — see
``tests/test_enforcement_application_writer.py`` for that).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
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
            patch(
                "app.services.enforcement._record_enforcement_application"
            ) as record,
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
            patch(
                "app.services.enforcement._nas_with_api_creds", return_value=None
            ),
            patch(
                "app.services.enforcement._record_enforcement_application"
            ) as record,
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
            patch(
                "app.services.enforcement._nas_with_api_creds", return_value=api_dev
            ),
            patch(
                "app.services.nas._mikrotik.apply_mikrotik_address_list_via_api",
                side_effect=_ROUTEROS_LOGIN_REJECTION,
            ),
            patch(
                "app.services.enforcement._record_enforcement_application"
            ) as record,
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
            patch(
                "app.services.enforcement._nas_with_api_creds", return_value=None
            ),
            patch(
                "app.services.enforcement._record_enforcement_application"
            ) as record,
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
            patch(
                "app.services.enforcement._nas_with_api_creds", return_value=api_dev
            ),
            patch(
                "app.services.nas._mikrotik.apply_mikrotik_address_list_via_api",
                return_value=True,
            ),
            patch(
                "app.services.enforcement._record_enforcement_application"
            ) as record,
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
            patch(
                "app.services.enforcement._record_enforcement_application"
            ) as record,
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
            patch(
                "app.services.enforcement._record_enforcement_application"
            ) as record,
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
            result = _api_kick_session(
                db, nas_device, "alice", subscription_id=sub_id
            )

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
            patch(
                "app.services.enforcement._nas_with_api_creds", return_value=api_dev
            ),
            patch(
                "app.services.nas._mikrotik.disconnect_mikrotik_pppoe_bulk",
                side_effect=TimeoutError(),
            ),
            patch(
                "app.services.enforcement._record_enforcement_application"
            ) as record,
        ):
            result = _api_kick_session(
                db, nas_device, "alice", subscription_id=sub_id
            )

        assert result is False
        outcome = record.call_args.kwargs["outcome"]
        assert outcome.outcome == EnforcementOutcomeValue.failed
        assert outcome.failure_class == EnforcementFailureClass.timeout
        assert outcome.path == EnforcementPath.api

    def test_api_success_records_applied_api(self):
        db = MagicMock()
        nas_device = _mikrotik_device()
        api_dev = _mikrotik_device()
        sub_id = uuid4()

        with (
            patch(
                "app.services.enforcement._nas_with_api_creds", return_value=api_dev
            ),
            patch(
                "app.services.nas._mikrotik.disconnect_mikrotik_pppoe_bulk",
                return_value={"alice"},
            ),
            patch(
                "app.services.enforcement._record_enforcement_application"
            ) as record,
        ):
            result = _api_kick_session(
                db, nas_device, "alice", subscription_id=sub_id
            )

        assert result is True
        outcome = record.call_args.kwargs["outcome"]
        assert outcome.outcome == EnforcementOutcomeValue.applied
        assert outcome.path == EnforcementPath.api
