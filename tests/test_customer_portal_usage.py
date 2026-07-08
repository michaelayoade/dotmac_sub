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

    def test_get_usage_history_aggregates_months(
        self, db_session, subscription
    ) -> None:
        from datetime import date

        from app.models.usage import SubscriberDailyUsage
        from app.services.customer_portal_flow_services import get_usage_history

        gb = 1024**3
        rows = [
            (date(2020, 1, 5), 1 * gb, 2 * gb),  # Jan: 3 GB
            (date(2020, 1, 15), 0, 1 * gb),  # Jan: +1 GB -> 4 GB
            (date(2020, 2, 10), 1 * gb, 1 * gb),  # Feb: 2 GB
        ]
        for i, (d, up, down) in enumerate(rows):
            db_session.add(
                SubscriberDailyUsage(
                    subscription_id=subscription.id,
                    splynx_service_id=7000 + i,
                    usage_date=d,
                    upload_bytes=up,
                    download_bytes=down,
                )
            )
        db_session.commit()

        out = get_usage_history(
            db_session, {"subscription_id": str(subscription.id)}, months=12
        )

        assert out["has_history"] is True
        assert out["months_shown"] == 2
        assert out["since"] == "Jan 2020"
        assert out["total_gb"] == 6.0
        assert out["average_gb"] == 3.0
        assert [r["label"] for r in out["chart_records"]] == ["Jan", "Feb"]
        assert out["chart_records"][0]["value"] == 4.0
        assert out["chart_records"][0]["download_value"] == 3.0
        assert out["chart_records"][0]["upload_value"] == 1.0
        assert out["chart_records"][1]["value"] == 2.0

    def test_get_usage_history_aggregates_account_subscriptions(
        self, db_session, subscriber, subscription, catalog_offer
    ) -> None:
        from datetime import date

        from app.models.catalog import Subscription, SubscriptionStatus
        from app.models.usage import SubscriberDailyUsage
        from app.services.customer_portal_flow_services import get_usage_history

        subscription.created_at = datetime(2020, 1, 1, tzinfo=UTC)
        subscription.start_at = datetime(2020, 1, 1, tzinfo=UTC)
        previous_subscription = Subscription(
            subscriber_id=subscriber.id,
            offer_id=catalog_offer.id,
            status=SubscriptionStatus.canceled,
            created_at=datetime(2024, 12, 14, tzinfo=UTC),
            start_at=datetime(2024, 12, 14, tzinfo=UTC),
            canceled_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        db_session.add(previous_subscription)
        db_session.flush()

        gb = 1024**3
        db_session.add_all(
            [
                SubscriberDailyUsage(
                    subscription_id=subscription.id,
                    splynx_service_id=8101,
                    usage_date=date(2026, 1, 5),
                    upload_bytes=1 * gb,
                    download_bytes=2 * gb,
                ),
                SubscriberDailyUsage(
                    subscription_id=previous_subscription.id,
                    splynx_service_id=8102,
                    usage_date=date(2026, 1, 6),
                    upload_bytes=3 * gb,
                    download_bytes=4 * gb,
                ),
            ]
        )
        db_session.commit()

        out = get_usage_history(
            db_session,
            {"account_id": str(subscriber.id), "subscription_id": str(subscription.id)},
            months=12,
        )

        assert out["has_history"] is True
        assert out["total_gb"] == 10.0
        assert out["chart_records"][0]["value"] == 10.0
        assert out["chart_records"][0]["download_value"] == 6.0
        assert out["chart_records"][0]["upload_value"] == 4.0

    def test_get_usage_history_empty_without_history(
        self, db_session, subscription
    ) -> None:
        from app.services.customer_portal_flow_services import get_usage_history

        out = get_usage_history(
            db_session, {"subscription_id": str(subscription.id)}, months=12
        )
        assert out["has_history"] is False
        assert out["chart_records"] == []
        assert out["total_gb"] == 0.0

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

    def test_get_usage_page_uses_all_account_subscriptions(
        self, db_session, subscriber, subscription, catalog_offer
    ) -> None:
        from app.models.catalog import Subscription, SubscriptionStatus
        from app.services.customer_portal_flow_services import get_usage_page

        subscription.created_at = datetime(2020, 1, 1, tzinfo=UTC)
        subscription.start_at = datetime(2020, 1, 1, tzinfo=UTC)
        previous_subscription = Subscription(
            subscriber_id=subscriber.id,
            offer_id=catalog_offer.id,
            status=SubscriptionStatus.canceled,
            created_at=datetime(2024, 12, 14, tzinfo=UTC),
            start_at=datetime(2024, 12, 14, tzinfo=UTC),
            canceled_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        db_session.add(previous_subscription)
        db_session.commit()

        chart_source_records = [
            SimpleNamespace(
                recorded_at=datetime(2026, 5, 1, tzinfo=UTC),
                amount=5.0,
                usage_amount=5.0,
                download_amount=3.0,
                upload_amount=2.0,
                unit="GB",
            )
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
                    "average_daily_usage_gb": 5.0,
                    "average_speed_mbps": 0.0,
                    "average_download_mbps": 0.0,
                    "average_upload_mbps": 0.0,
                },
            ) as usage_summary_stats,
            patch(
                "app.services.customer_portal_flow_services._get_fup_status",
                return_value=None,
            ),
        ):
            page = get_usage_page(
                db_session,
                {"account_id": str(subscriber.id)},
                page=1,
                per_page=10,
            )

        expected_ids = {str(subscription.id), str(previous_subscription.id)}
        assert set(daily_records.call_args.kwargs["subscription_ids"]) == expected_ids
        assert set(usage_summary_stats.call_args.kwargs["subscription_ids"]) == expected_ids
        get_engine.assert_not_called()
        assert page["usage_source"] == "postgres"
        assert page["chart_records"][0]["value"] == 5.0

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
                "app.web.customer.routes.customer_portal.get_usage_history",
                return_value={"has_history": False, "chart_records": []},
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
