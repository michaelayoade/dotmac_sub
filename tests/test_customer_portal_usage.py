from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class TestCustomerPortalUsagePage:
    def test_get_usage_page_skips_postgres_fallback_when_disabled(
        self, db_session, subscription
    ) -> None:
        from app.services.customer_portal_flow_services import get_usage_page

        customer = {"subscription_id": str(subscription.id)}

        with (
            patch("app.services.zabbix_engine.get_zabbix_engine") as get_engine,
            patch(
                "app.services.customer_portal_flow_services._daily_bandwidth_usage"
            ) as daily_bandwidth_usage,
            patch(
                "app.services.customer_portal_flow_services._usage_summary_stats"
            ) as usage_summary_stats,
            patch(
                "app.services.customer_portal_flow_services._get_fup_status"
            ) as get_fup_status,
        ):
            get_engine.return_value.get_cached_customer_usage.return_value = None

            page = get_usage_page(
                db_session,
                customer,
                allow_postgres_fallback=False,
            )

        assert page["usage_records"] == []
        assert page["chart_records"] == []
        assert page["has_subscription"] is True
        assert page["usage_source"] == "unavailable"
        daily_bandwidth_usage.assert_not_called()
        usage_summary_stats.assert_not_called()
        get_fup_status.assert_not_called()

    def test_get_usage_page_returns_full_chart_records_with_paginated_table(
        self, db_session, subscription
    ) -> None:
        from app.services.customer_portal_flow_services import get_usage_page

        customer = {"subscription_id": str(subscription.id)}
        chart_source_records = [
            SimpleNamespace(
                recorded_at=datetime(2026, 5, 1, tzinfo=UTC),
                amount=1.25,
                usage_amount=1.25,
                download_amount=0.75,
                upload_amount=0.5,
                unit="GB",
            ),
            SimpleNamespace(
                recorded_at=datetime(2026, 5, 2, tzinfo=UTC),
                amount=2.5,
                usage_amount=2.5,
                download_amount=1.5,
                upload_amount=1.0,
                unit="GB",
            ),
            SimpleNamespace(
                recorded_at=datetime(2026, 5, 3, tzinfo=UTC),
                amount=3.75,
                usage_amount=3.75,
                download_amount=2.25,
                upload_amount=1.5,
                unit="GB",
            ),
        ]

        with (
            patch("app.services.zabbix_engine.get_zabbix_engine") as get_engine,
            patch(
                "app.services.customer_portal_flow_services._daily_bandwidth_usage_records",
                return_value=chart_source_records,
            ) as daily_records,
            patch(
                "app.services.customer_portal_flow_services._usage_summary_stats",
                return_value={
                    "average_daily_usage_gb": 2.5,
                    "average_speed_mbps": 10.0,
                    "average_download_mbps": 6.0,
                    "average_upload_mbps": 4.0,
                },
            ),
            patch(
                "app.services.customer_portal_flow_services._get_fup_status",
                return_value=None,
            ),
        ):
            get_engine.return_value.get_cached_customer_usage.return_value = None

            page = get_usage_page(
                db_session,
                customer,
                page=1,
                per_page=2,
            )

        daily_records.assert_called_once()
        assert len(page["usage_records"]) == 2
        assert [record.amount for record in page["usage_records"]] == [1.25, 2.5]
        assert len(page["chart_records"]) == 3
        assert [record["label"] for record in page["chart_records"]] == [
            "May 01",
            "May 02",
            "May 03",
        ]
        assert page["chart_records"][-1]["value"] == 3.75
        assert page["chart_records"][0]["download_value"] == 0.75
        assert page["chart_records"][0]["upload_value"] == 0.5

    def test_daily_bandwidth_usage_batches_bandwidth_lookup(self) -> None:
        from app.services.customer_portal_flow_services import _daily_bandwidth_usage

        db = MagicMock()
        query = MagicMock()
        db.query.return_value = query
        query.filter.return_value = query
        query.group_by.return_value = query
        query.all.return_value = [
            SimpleNamespace(
                bucket_start=datetime(2026, 3, 20, tzinfo=UTC),
                rx_bps=8_000_000,
                tx_bps=2_000_000,
            )
        ]

        with patch(
            "app.services.customer_portal_flow_services._daily_usage_breakdown_records",
            return_value={},
        ):
            records, total = _daily_bandwidth_usage(
                db,
                subscription_id="00000000-0000-0000-0000-000000000001",
                start_at=datetime(2026, 3, 20, 0, 0, tzinfo=UTC),
                end_at=datetime(2026, 3, 20, 23, 59, tzinfo=UTC),
                page=1,
                per_page=10,
            )

        assert db.query.call_count == 2
        assert query.group_by.call_count == 2
        assert query.all.call_count == 2
        query.first.assert_not_called()
        assert total == 1
        assert len(records) == 1
        assert records[0].usage_amount > 0

    def test_daily_bandwidth_usage_records_falls_back_to_radius_accounting(
        self,
    ) -> None:
        from app.services.customer_portal_flow_services import (
            _daily_bandwidth_usage_records,
        )

        db = MagicMock()
        start_at = datetime(2026, 3, 19, 0, 0, tzinfo=UTC)
        end_at = datetime(2026, 3, 20, 23, 59, tzinfo=UTC)

        with (
            patch(
                "app.services.customer_portal_flow_services._daily_usage_breakdown_records",
                return_value={},
            ),
            patch(
                "app.services.customer_portal_flow_services._daily_bandwidth_averages",
                return_value={},
            ),
            patch(
                "app.services.customer_portal_flow_services._daily_radius_accounting_usage",
                return_value={
                    datetime(2026, 3, 19, tzinfo=UTC).date(): (1.25, 0.5, 1.75)
                },
            ),
        ):
            records = _daily_bandwidth_usage_records(
                db,
                subscription_id="00000000-0000-0000-0000-000000000001",
                start_at=start_at,
                end_at=end_at,
            )

        assert len(records) == 2
        assert records[0].recorded_at == datetime(2026, 3, 20, tzinfo=UTC)
        assert records[0].usage_amount == 0
        assert records[0].download_amount == 0
        assert records[0].upload_amount == 0
        assert records[1].recorded_at == datetime(2026, 3, 19, tzinfo=UTC)
        assert records[1].usage_amount == 1.75
        assert records[1].download_amount == 1.25
        assert records[1].upload_amount == 0.5


class TestCustomerUsageRoute:
    def test_usage_page_defaults_to_chart_and_includes_initial_bandwidth_stats(
        self,
    ) -> None:
        from app.web.customer.routes import customer_usage

        request = MagicMock()
        customer = {"subscriber_id": "sub-1", "account_id": "acct-1"}
        subscription = SimpleNamespace(id="svc-123")
        usage_page = {
            "usage_records": [],
            "chart_records": [],
            "period": "current",
            "page": 1,
            "per_page": 25,
            "total": 0,
            "total_pages": 1,
            "usage_summary": {},
            "fup_status": None,
            "usage_source": "none",
            "has_subscription": True,
        }
        template_response = MagicMock(name="template_response")

        with (
            patch(
                "app.web.customer.routes.get_current_customer_from_request",
                return_value=customer,
            ),
            patch(
                "app.web.customer.routes.customer_portal.get_usage_page",
                return_value=usage_page,
            ),
            patch(
                "app.web.customer.routes.resolve_customer_subscription",
                return_value=subscription,
            ),
            patch(
                "app.web.customer.routes._load_initial_bandwidth_stats",
                return_value={"current_rx_formatted": "9.41 Kbps"},
            ),
            patch(
                "app.web.customer.routes.templates.TemplateResponse",
                return_value=template_response,
            ) as render,
        ):
            response = customer_usage(
                request=request,
                period="current",
                page=1,
                per_page=25,
                db=MagicMock(),
            )

        assert response is template_response
        assert render.call_args.args[0] == "customer/usage/index.html"
        context = render.call_args.args[1]
        assert context["bandwidth_chart_initial_stats"] == {
            "current_rx_formatted": "9.41 Kbps"
        }
        assert context["usage_chart_records"] == usage_page["chart_records"]
        assert context["usage_enable_records_chart"] is True
        assert context["usage_records_default_view"] == "chart"
        assert context["usage_records_chart_id"] == "portal-usage-records-chart"
        assert context["usage_records_chart_label"] == "Daily Usage (GB)"
