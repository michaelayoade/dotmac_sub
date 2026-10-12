from unittest.mock import Mock

import pytest

from app import metrics


@pytest.mark.parametrize("src", ["user_credential", "access_credential", "radius_user"])
def test_portal_metric_has_only_bounded_source_label(monkeypatch, src):
    counter = Mock()
    monkeypatch.setattr(metrics, "PORTAL_AUTHENTICATION_SUCCESSES", counter)
    metrics.record_portal_authentication_success(src=src)
    counter.labels.assert_called_once_with(src=src)
    counter.labels.return_value.inc.assert_called_once_with()


@pytest.mark.parametrize("src", ["", "unknown", "submitted-identifier@example.test"])
def test_unknown_metric_source_cannot_create_a_label(monkeypatch, src):
    counter = Mock()
    monkeypatch.setattr(metrics, "PORTAL_AUTHENTICATION_SUCCESSES", counter)
    metrics.record_portal_authentication_success(src=src)
    counter.labels.assert_not_called()


def test_metric_failure_does_not_change_delivered_login(monkeypatch):
    counter = Mock()
    counter.labels.return_value.inc.side_effect = RuntimeError("metrics unavailable")
    monkeypatch.setattr(metrics, "PORTAL_AUTHENTICATION_SUCCESSES", counter)
    metrics.record_portal_authentication_success(src="user_credential")
