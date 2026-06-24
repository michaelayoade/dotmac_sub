"""Tests for Celery tasks."""

import logging
from unittest.mock import MagicMock, patch

import pytest

# =============================================================================
# Billing Task Tests
# =============================================================================


class TestBillingTask:
    """Tests for billing.run_invoice_cycle task."""

    def test_run_invoice_cycle_success(self):
        """Test successful invoice cycle run."""
        mock_session = MagicMock()
        mock_idempotency_session = MagicMock()
        # Mock scalars().first() to return None (no existing execution)
        mock_idempotency_session.scalars.return_value.first.return_value = None

        with patch("app.tasks.billing.SessionLocal", return_value=mock_session):
            with patch(
                "app.services.task_idempotency.SessionLocal",
                return_value=mock_idempotency_session,
            ):
                with patch(
                    "app.tasks.billing.billing_automation_service.run_invoice_cycle"
                ) as mock_run:
                    from app.tasks.billing import run_invoice_cycle

                    run_invoice_cycle()

                    mock_run.assert_called_once_with(mock_session)
                    mock_session.close.assert_called_once()

    def test_run_invoice_cycle_exception_rollback(self):
        """Test exception triggers rollback."""
        mock_session = MagicMock()
        mock_idempotency_session = MagicMock()
        mock_idempotency_session.scalars.return_value.first.return_value = None

        with patch("app.tasks.billing.SessionLocal", return_value=mock_session):
            with patch(
                "app.services.task_idempotency.SessionLocal",
                return_value=mock_idempotency_session,
            ):
                with patch(
                    "app.tasks.billing.billing_automation_service.run_invoice_cycle",
                    side_effect=Exception("Billing error"),
                ):
                    from app.tasks.billing import run_invoice_cycle

                    with pytest.raises(Exception, match="Billing error"):
                        run_invoice_cycle()

                    mock_session.rollback.assert_called_once()
                    mock_session.close.assert_called_once()

    def test_check_billing_switch_task_reports_enforcement_health(self):
        """Hourly billing guard includes payment/enforcement health state."""
        mock_session = MagicMock()
        enforcement = MagicMock(
            ok=False,
            reasons=["recent_payment_volume_below_floor"],
            details={"payment_recent_successes": 0},
        )
        notification = MagicMock(
            ok=True,
            reasons=[],
            details={"recent_failed": 0},
        )

        with patch("app.tasks.billing.SessionLocal", return_value=mock_session):
            with patch(
                "app.tasks.billing.check_billing_switch",
                return_value={"ok": True, "expected": True, "actual": True},
            ):
                with patch(
                    "app.tasks.billing.billing_enforcement_health",
                    return_value=enforcement,
                ):
                    with patch(
                        "app.tasks.billing.notification_delivery_health",
                        return_value=notification,
                    ):
                        from app.tasks.billing import check_billing_switch_task

                        result = check_billing_switch_task()

        assert result["ok"] is False
        assert result["billing_switch"]["ok"] is True
        assert result["billing_enforcement_health"]["ok"] is False
        assert result["billing_enforcement_health"]["reasons"] == [
            "recent_payment_volume_below_floor"
        ]
        assert result["notification_delivery_health"]["ok"] is True
        mock_session.close.assert_called_once()

    def test_check_billing_switch_task_alerts_notification_delivery_health(
        self, caplog
    ):
        """Notification failures alert operators without gating billing by default."""
        mock_session = MagicMock()
        enforcement = MagicMock(ok=True, reasons=[], details={})
        notification = MagicMock(
            ok=False,
            reasons=["critical_notifications_failed"],
            details={"recent_failed": 10},
        )

        with patch("app.tasks.billing.SessionLocal", return_value=mock_session):
            with patch(
                "app.tasks.billing.check_billing_switch",
                return_value={"ok": True, "expected": True, "actual": True},
            ):
                with patch(
                    "app.tasks.billing.billing_enforcement_health",
                    return_value=enforcement,
                ):
                    with patch(
                        "app.tasks.billing.notification_delivery_health",
                        return_value=notification,
                    ):
                        from app.tasks.billing import check_billing_switch_task

                        with caplog.at_level(logging.ERROR, logger="app.tasks.billing"):
                            result = check_billing_switch_task()

        assert result["ok"] is True
        assert result["notification_delivery_health"]["ok"] is False
        assert "billing_notification_delivery_unhealthy" in caplog.text
        assert "critical_notifications_failed" in caplog.text
        mock_session.close.assert_called_once()


# =============================================================================
# OLT Profile Sync Task Tests
# =============================================================================


class TestOltProfileSyncTask:
    """Tests for profile_sync.execute_due_profile_sync_tasks task."""

    def test_execute_due_profile_sync_tasks_routes_to_tr069_queue(self):
        """Profile sync applies OLT commands, so it must run on the OLT queue."""
        from app.celery_app import celery_app

        assert celery_app.conf.task_routes[
            "app.tasks.profile_sync.execute_due_profile_sync_tasks"
        ] == {"queue": "tr069"}

    def test_app_cache_refresh_tasks_are_registered(self):
        """Beat-scheduled cache refresh tasks must be in the worker registry."""
        from app.celery_app import celery_app

        assert "app.tasks.app_cache.refresh_dashboard_stats_cache" in celery_app.tasks
        assert (
            "app.tasks.app_cache.refresh_ont_zabbix_snapshot_cache" in celery_app.tasks
        )

    def test_execute_due_profile_sync_tasks_success(self):
        """Test successful due profile sync execution."""
        mock_session = MagicMock()
        expected = {"total": 1, "completed": 1, "failed": 0, "results": []}

        with patch(
            "app.tasks.profile_sync.db_session_adapter.create_session",
            return_value=mock_session,
        ):
            with patch(
                "app.tasks.profile_sync.profile_sync_service.execute_due_profile_sync_tasks",
                return_value=expected,
            ) as mock_execute:
                from app.tasks.profile_sync import execute_due_profile_sync_tasks

                result = execute_due_profile_sync_tasks(limit=10)

                assert result == expected
                mock_execute.assert_called_once_with(
                    mock_session,
                    executed_by="profile-sync-worker",
                    actor_is_admin=True,
                    limit=10,
                )
                mock_session.close.assert_called_once()

    def test_execute_due_profile_sync_tasks_exception_rollback(self):
        """Test exception triggers rollback and closes session."""
        mock_session = MagicMock()

        with patch(
            "app.tasks.profile_sync.db_session_adapter.create_session",
            return_value=mock_session,
        ):
            with patch(
                "app.tasks.profile_sync.profile_sync_service.execute_due_profile_sync_tasks",
                side_effect=Exception("sync failed"),
            ):
                from app.tasks.profile_sync import execute_due_profile_sync_tasks

                with pytest.raises(Exception, match="sync failed"):
                    execute_due_profile_sync_tasks()

                mock_session.rollback.assert_called_once()
                mock_session.close.assert_called_once()


# =============================================================================
# Collections Task Tests
# =============================================================================


class TestCollectionsTask:
    """Tests for collections billing-enforcement tasks."""

    def test_run_billing_enforcement_success(self):
        """Unified enforcement run returns the real run metrics."""
        from datetime import UTC, datetime

        from app.schemas.collections import BillingEnforcementRunResponse

        mock_session = MagicMock()

        with patch("app.tasks.collections.SessionLocal", return_value=mock_session):
            with patch("app.tasks.collections.billing_enabled", return_value=True):
                with patch(
                    "app.tasks.collections.collections_service."
                    "billing_enforcement_reconciler.run"
                ) as mock_run:
                    mock_run.return_value = BillingEnforcementRunResponse(
                        run_at=datetime.now(UTC),
                        accounts_scanned=7,
                        cases_created=3,
                        actions_created=2,
                        skipped=1,
                        dunning_accounts_scanned=7,
                        dunning_cases_created=3,
                        dunning_actions_created=2,
                        dunning_skipped=1,
                    )
                    from app.tasks.collections import run_billing_enforcement

                    result = run_billing_enforcement()

                    mock_run.assert_called_once()
                    args = mock_run.call_args
                    assert args[0][0] == mock_session
                    mock_session.close.assert_called_once()
                    assert result == {
                        "accounts_scanned": 7,
                        "cases_created": 3,
                        "actions_created": 2,
                        "skipped": 1,
                        "credit_accounts_scanned": 0,
                        "credit_accounts_settled": 0,
                        "credit_invoices_touched": 0,
                        "credit_settlement_errors": 0,
                        "credit_applied": "0.00",
                    }

    def test_run_dunning_alias_uses_unified_enforcer(self):
        """Legacy task name remains an alias for the unified enforcer."""
        from datetime import UTC, datetime

        from app.schemas.collections import BillingEnforcementRunResponse

        mock_session = MagicMock()

        with patch("app.tasks.collections.SessionLocal", return_value=mock_session):
            with patch("app.tasks.collections.billing_enabled", return_value=True):
                with patch(
                    "app.tasks.collections.collections_service."
                    "billing_enforcement_reconciler.run"
                ) as mock_run:
                    mock_run.return_value = BillingEnforcementRunResponse(
                        run_at=datetime.now(UTC),
                        accounts_scanned=1,
                        cases_created=0,
                        actions_created=0,
                        skipped=0,
                        dunning_accounts_scanned=1,
                        dunning_cases_created=0,
                        dunning_actions_created=0,
                        dunning_skipped=0,
                    )
                    from app.tasks.collections import run_dunning

                    result = run_dunning()

                    mock_run.assert_called_once()
                    assert result["accounts_scanned"] == 1

    def test_run_billing_enforcement_exception_closes_session(self):
        """Test exception still closes session."""
        mock_session = MagicMock()

        with patch("app.tasks.collections.SessionLocal", return_value=mock_session):
            with patch("app.tasks.collections.billing_enabled", return_value=True):
                with patch(
                    "app.tasks.collections.collections_service."
                    "billing_enforcement_reconciler.run",
                    side_effect=Exception("Dunning error"),
                ):
                    from app.tasks.collections import run_billing_enforcement

                    with pytest.raises(Exception, match="Dunning error"):
                        run_billing_enforcement()

                    mock_session.close.assert_called_once()


class TestBillingMasterSwitchGates:
    """Customer-impacting billing tasks must no-op while billing_enabled is off.

    The upstream biller stays authoritative until cutover; these tasks
    must never charge, dun, suspend, or expire an account before billing_enabled
    is flipped on, even though the queue consumes them and they are scheduled.
    """

    def test_dunning_skipped_when_billing_disabled(self):
        mock_session = MagicMock()
        with patch("app.tasks.collections.SessionLocal", return_value=mock_session):
            with patch("app.tasks.collections.billing_enabled", return_value=False):
                with patch(
                    "app.tasks.collections.collections_service."
                    "billing_enforcement_reconciler.run"
                ) as mock_run:
                    from app.tasks.collections import run_dunning

                    result = run_dunning()

                    mock_run.assert_not_called()
                    assert result == {"skipped": "billing_disabled"}

    def test_autopay_skipped_when_billing_disabled(self):
        mock_session = MagicMock()
        with patch("app.tasks.autopay.SessionLocal", return_value=mock_session):
            with patch("app.tasks.autopay.billing_enabled", return_value=False):
                with patch("app.tasks.autopay.autopay_service.run_all_due") as mock_run:
                    from app.tasks.autopay import charge_due_invoices

                    result = charge_due_invoices()

                    mock_run.assert_not_called()
                    assert result == {"skipped": "billing_disabled"}

    def test_arrangement_check_skipped_when_billing_disabled(self):
        mock_session = MagicMock()
        with patch("app.tasks.arrangements.SessionLocal", return_value=mock_session):
            with patch("app.tasks.arrangements.billing_enabled", return_value=False):
                with patch(
                    "app.tasks.arrangements.payment_arrangements"
                    ".check_overdue_installments"
                ) as mock_run:
                    from app.tasks.arrangements import check_overdue_arrangements

                    result = check_overdue_arrangements()

                    mock_run.assert_not_called()
                    assert result["arrangements_defaulted"] == 0


# =============================================================================
# GIS Task Tests
# =============================================================================


class TestGisTask:
    """Tests for gis.sync_gis_sources task."""

    def test_sync_gis_sources_both_enabled(self):
        """Test sync with both pop sites and addresses enabled."""
        mock_session = MagicMock()
        mock_result = MagicMock(created=1, updated=2, skipped=0)

        with patch("app.tasks.gis.SessionLocal", return_value=mock_session):
            with patch(
                "app.tasks.gis._effective_bool",
                side_effect=[
                    True,
                    True,
                    False,
                ],  # sync_pops, sync_addresses, deactivate_missing
            ):
                with patch(
                    "app.tasks.gis.gis_sync_service.geo_sync.sync_pop_sites",
                    return_value=mock_result,
                ) as mock_pops:
                    with patch(
                        "app.tasks.gis.gis_sync_service.geo_sync.sync_addresses",
                        return_value=mock_result,
                    ) as mock_addresses:
                        with patch("app.tasks.gis.observe_job"):
                            from app.tasks.gis import sync_gis_sources

                            sync_gis_sources()

                            mock_pops.assert_called_once()
                            mock_addresses.assert_called_once()

    def test_sync_gis_sources_only_pop_sites(self):
        """Test sync with only pop sites enabled."""
        mock_session = MagicMock()
        mock_result = MagicMock(created=1, updated=0, skipped=0)

        with patch("app.tasks.gis.SessionLocal", return_value=mock_session):
            with patch(
                "app.tasks.gis._effective_bool",
                side_effect=[
                    True,
                    False,
                    False,
                ],  # sync_pops=True, sync_addresses=False
            ):
                with patch(
                    "app.tasks.gis.gis_sync_service.geo_sync.sync_pop_sites",
                    return_value=mock_result,
                ) as mock_pops:
                    with patch(
                        "app.tasks.gis.gis_sync_service.geo_sync.sync_addresses",
                    ) as mock_addresses:
                        with patch("app.tasks.gis.observe_job"):
                            from app.tasks.gis import sync_gis_sources

                            sync_gis_sources()

                            mock_pops.assert_called_once()
                            mock_addresses.assert_not_called()

    def test_sync_gis_sources_only_addresses(self):
        """Test sync with only addresses enabled."""
        mock_session = MagicMock()
        mock_result = MagicMock(created=0, updated=1, skipped=0)

        with patch("app.tasks.gis.SessionLocal", return_value=mock_session):
            with patch(
                "app.tasks.gis._effective_bool",
                side_effect=[
                    False,
                    True,
                    False,
                ],  # sync_pops=False, sync_addresses=True
            ):
                with patch(
                    "app.tasks.gis.gis_sync_service.geo_sync.sync_pop_sites",
                ) as mock_pops:
                    with patch(
                        "app.tasks.gis.gis_sync_service.geo_sync.sync_addresses",
                        return_value=mock_result,
                    ) as mock_addresses:
                        with patch("app.tasks.gis.observe_job"):
                            from app.tasks.gis import sync_gis_sources

                            sync_gis_sources()

                            mock_pops.assert_not_called()
                            mock_addresses.assert_called_once()

    def test_sync_gis_sources_exception_reports_error(self):
        """Test exception reports error status."""
        mock_session = MagicMock()

        with patch("app.tasks.gis.SessionLocal", return_value=mock_session):
            with patch(
                "app.tasks.gis._effective_bool",
                side_effect=[True, True, False],
            ):
                with patch(
                    "app.tasks.gis.gis_sync_service.geo_sync.sync_pop_sites",
                    side_effect=Exception("GIS error"),
                ):
                    with patch("app.tasks.gis.observe_job") as mock_observe:
                        from app.tasks.gis import sync_gis_sources

                        with pytest.raises(Exception, match="GIS error"):
                            sync_gis_sources()

                        mock_session.close.assert_called_once()
                        # Check that error status was reported
                        mock_observe.assert_called_once()
                        args = mock_observe.call_args[0]
                        assert args[0] == "gis_sync"
                        assert args[1] == "error"


# =============================================================================
# Integrations Task Tests
# =============================================================================


class TestIntegrationsTask:
    """Tests for integrations.run_integration_job task."""

    def test_run_integration_job_success(self):
        """Test successful integration job run."""
        mock_session = MagicMock()
        # Set up mock query chain to return None (no running job)
        mock_session.query.return_value.filter.return_value.filter.return_value.first.return_value = None
        job_id = "00000000-0000-0000-0000-000000000123"

        with patch("app.tasks.integrations.SessionLocal", return_value=mock_session):
            with patch(
                "app.tasks.integrations.integration_service.integration_jobs.run"
            ) as mock_run:
                with patch("app.tasks.integrations.observe_job") as mock_observe:
                    from app.tasks.integrations import run_integration_job

                    run_integration_job(job_id)

                    mock_run.assert_called_once_with(mock_session, job_id)
                    mock_session.close.assert_called_once()
                    mock_observe.assert_called_once()
                    args = mock_observe.call_args[0]
                    assert args[0] == "integration_job"
                    assert args[1] == "success"

    def test_run_integration_job_exception_reports_error(self):
        """Test exception reports error status."""
        mock_session = MagicMock()
        # Set up mock query chain to return None (no running job)
        mock_session.query.return_value.filter.return_value.filter.return_value.first.return_value = None
        job_id = "00000000-0000-0000-0000-000000000456"

        with patch("app.tasks.integrations.SessionLocal", return_value=mock_session):
            with patch(
                "app.tasks.integrations.integration_service.integration_jobs.run",
                side_effect=Exception("Integration error"),
            ):
                with patch("app.tasks.integrations.observe_job") as mock_observe:
                    from app.tasks.integrations import run_integration_job

                    with pytest.raises(Exception, match="Integration error"):
                        run_integration_job(job_id)

                    mock_session.close.assert_called_once()
                    mock_observe.assert_called_once()
                    args = mock_observe.call_args[0]
                    assert args[1] == "error"


# =============================================================================
# Radius Task Tests
# =============================================================================


class TestRadiusTask:
    """Tests for radius.run_radius_sync_job task."""

    def test_run_radius_sync_job_success(self):
        """Test successful radius sync job run."""
        mock_session = MagicMock()

        with patch("app.tasks.radius.SessionLocal", return_value=mock_session):
            with patch(
                "app.tasks.radius.radius_service.radius_sync_jobs.run"
            ) as mock_run:
                from app.tasks.radius import run_radius_sync_job

                run_radius_sync_job("sync-job-123")

                mock_run.assert_called_once_with(mock_session, "sync-job-123")
                mock_session.close.assert_called_once()

    def test_run_radius_sync_job_exception_closes_session(self):
        """Test exception still closes session."""
        mock_session = MagicMock()

        with patch("app.tasks.radius.SessionLocal", return_value=mock_session):
            with patch(
                "app.tasks.radius.radius_service.radius_sync_jobs.run",
                side_effect=Exception("Radius error"),
            ):
                from app.tasks.radius import run_radius_sync_job

                with pytest.raises(Exception, match="Radius error"):
                    run_radius_sync_job("sync-job-456")

                mock_session.close.assert_called_once()


# =============================================================================
# Usage Task Tests
# =============================================================================


class TestUsageTask:
    """Tests for usage.run_usage_rating task."""

    def test_run_usage_rating_success(self):
        """Test successful usage rating run."""
        mock_session = MagicMock()

        with patch("app.tasks.usage.SessionLocal", return_value=mock_session):
            with patch(
                "app.tasks.usage.usage_service.usage_rating_runs.run"
            ) as mock_run:
                from app.tasks.usage import run_usage_rating

                run_usage_rating()

                mock_run.assert_called_once()
                args = mock_run.call_args
                assert args[0][0] == mock_session
                mock_session.close.assert_called_once()

    def test_run_usage_rating_exception_closes_session(self):
        """Test exception still closes session."""
        mock_session = MagicMock()

        with patch("app.tasks.usage.SessionLocal", return_value=mock_session):
            with patch(
                "app.tasks.usage.usage_service.usage_rating_runs.run",
                side_effect=Exception("Usage error"),
            ):
                from app.tasks.usage import run_usage_rating

                with pytest.raises(Exception, match="Usage error"):
                    run_usage_rating()

                mock_session.close.assert_called_once()


class TestDailyRunnerQueueRouting:
    """Daily business runners must not share the (backlogged) default queue."""

    def test_daily_runners_route_to_billing_queue(self):
        from app.celery_app import celery_app

        for task in (
            "app.tasks.billing.run_invoice_cycle",
            "app.tasks.collections.run_billing_enforcement",
            "app.tasks.collections.run_dunning",
            "app.tasks.catalog.expire_subscriptions",
            "app.tasks.enforcement.cleanup_subscription_block_sessions",
            "app.tasks.usage.run_usage_rating",
            "app.tasks.usage.evaluate_fup_rules",
        ):
            assert celery_app.conf.task_routes[task] == {"queue": "billing"}, task

    def test_billing_queue_is_declared(self):
        from app.celery_app import celery_app

        assert "billing" in {q.name for q in celery_app.conf.task_queues}

    def test_whole_base_runners_get_long_time_limits(self):
        from app.services.scheduler_config import get_celery_config

        annotations = get_celery_config()["task_annotations"]
        for task in (
            "app.tasks.billing.run_invoice_cycle",
            "app.tasks.collections.run_billing_enforcement",
            "app.tasks.collections.run_dunning",
        ):
            assert annotations[task]["time_limit"] >= 1800, task
