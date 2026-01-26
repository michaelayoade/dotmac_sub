"""Tests for Celery tasks."""

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

        with patch("app.tasks.billing.SessionLocal", return_value=mock_session):
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

        with patch("app.tasks.billing.SessionLocal", return_value=mock_session):
            with patch(
                "app.tasks.billing.billing_automation_service.run_invoice_cycle",
                side_effect=Exception("Billing error"),
            ):
                from app.tasks.billing import run_invoice_cycle

                with pytest.raises(Exception, match="Billing error"):
                    run_invoice_cycle()

                mock_session.rollback.assert_called_once()
                mock_session.close.assert_called_once()


# =============================================================================
# Collections Task Tests
# =============================================================================


class TestCollectionsTask:
    """Tests for collections.run_dunning task."""

    def test_run_dunning_success(self):
        """Test successful dunning run."""
        mock_session = MagicMock()

        with patch("app.tasks.collections.SessionLocal", return_value=mock_session):
            with patch(
                "app.tasks.collections.collections_service.dunning_workflow.run"
            ) as mock_run:
                from app.tasks.collections import run_dunning

                run_dunning()

                mock_run.assert_called_once()
                args = mock_run.call_args
                assert args[0][0] == mock_session
                mock_session.close.assert_called_once()

    def test_run_dunning_exception_closes_session(self):
        """Test exception still closes session."""
        mock_session = MagicMock()

        with patch("app.tasks.collections.SessionLocal", return_value=mock_session):
            with patch(
                "app.tasks.collections.collections_service.dunning_workflow.run",
                side_effect=Exception("Dunning error"),
            ):
                from app.tasks.collections import run_dunning

                with pytest.raises(Exception, match="Dunning error"):
                    run_dunning()

                mock_session.close.assert_called_once()

    def test_run_prepaid_enforcement_success(self):
        """Test successful prepaid enforcement run."""
        mock_session = MagicMock()

        with patch("app.tasks.collections.SessionLocal", return_value=mock_session):
            with patch(
                "app.tasks.collections.collections_service.prepaid_enforcement.run"
            ) as mock_run:
                from app.tasks.collections import run_prepaid_enforcement

                run_prepaid_enforcement()

                mock_run.assert_called_once()
                args = mock_run.call_args
                assert args[0][0] == mock_session
                mock_session.close.assert_called_once()

    def test_run_prepaid_enforcement_exception_closes_session(self):
        """Test prepaid enforcement exception still closes session."""
        mock_session = MagicMock()

        with patch("app.tasks.collections.SessionLocal", return_value=mock_session):
            with patch(
                "app.tasks.collections.collections_service.prepaid_enforcement.run",
                side_effect=Exception("Prepaid error"),
            ):
                from app.tasks.collections import run_prepaid_enforcement

                with pytest.raises(Exception, match="Prepaid error"):
                    run_prepaid_enforcement()

                mock_session.close.assert_called_once()


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
                side_effect=[True, True, False],  # sync_pops, sync_addresses, deactivate_missing
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
                side_effect=[True, False, False],  # sync_pops=True, sync_addresses=False
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
                side_effect=[False, True, False],  # sync_pops=False, sync_addresses=True
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
