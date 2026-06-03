from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class TestCustomerBillingRouteRegistration:
    def test_topup_get_route_exists(self) -> None:
        from app.web.customer.routes import router

        routes = {
            (getattr(route, "path", ""), method)
            for route in router.routes
            for method in getattr(route, "methods", set())
        }
        assert ("/portal/billing/topup", "GET") in routes

    def test_topup_verify_get_route_exists(self) -> None:
        from app.web.customer.routes import router

        routes = {
            (getattr(route, "path", ""), method)
            for route in router.routes
            for method in getattr(route, "methods", set())
        }
        assert ("/portal/billing/topup/verify", "GET") in routes

    def test_topup_intent_post_route_exists(self) -> None:
        from app.web.customer.routes import router

        routes = {
            (getattr(route, "path", ""), method)
            for route in router.routes
            for method in getattr(route, "methods", set())
        }
        assert ("/portal/billing/topup/intent", "POST") in routes


class TestPaymentSuccessBanner:
    def test_payment_success_only_marks_service_restored_after_post_payment_check(
        self,
    ) -> None:
        from app.web.customer.routes import customer_verify_payment

        request = MagicMock()
        customer = {"subscriber_id": "sub-1", "account_id": "acct-1"}
        result = {
            "payment": SimpleNamespace(receipt_number="RCT-1"),
            "invoice": SimpleNamespace(id="inv-1", invoice_number="INV-1"),
            "amount": 5000,
            "reference": "ref-1",
        }

        template_response = MagicMock(name="template_response")

        with (
            patch(
                "app.web.customer.routes.get_current_customer_from_request",
                return_value=customer,
            ),
            patch(
                "app.web.customer.routes.customer_portal.verify_and_record_payment",
                return_value=result,
            ),
            patch(
                "app.web.customer.routes.is_subscriber_restricted",
                side_effect=[True, False],
            ),
            patch(
                "app.web.customer.routes.templates.TemplateResponse",
                return_value=template_response,
            ) as render,
        ):
            response = customer_verify_payment(
                request=request,
                reference="ref-1",
                provider="paystack",
                db=MagicMock(),
            )

        assert response is template_response
        context = render.call_args.args[1]
        assert context["was_restricted"] is True
        assert context["service_restored"] is True

    def test_payment_success_does_not_claim_restoration_after_partial_payment(
        self,
    ) -> None:
        from app.web.customer.routes import customer_verify_payment

        request = MagicMock()
        customer = {"subscriber_id": "sub-1", "account_id": "acct-1"}
        result = {
            "payment": SimpleNamespace(receipt_number="RCT-2"),
            "invoice": SimpleNamespace(id="inv-2", invoice_number="INV-2"),
            "amount": 1000,
            "reference": "ref-2",
        }

        template_response = MagicMock(name="template_response")

        with (
            patch(
                "app.web.customer.routes.get_current_customer_from_request",
                return_value=customer,
            ),
            patch(
                "app.web.customer.routes.customer_portal.verify_and_record_payment",
                return_value=result,
            ),
            patch(
                "app.web.customer.routes.is_subscriber_restricted",
                side_effect=[True, True],
            ),
            patch(
                "app.web.customer.routes.templates.TemplateResponse",
                return_value=template_response,
            ) as render,
        ):
            response = customer_verify_payment(
                request=request,
                reference="ref-2",
                provider="paystack",
                db=MagicMock(),
            )

        assert response is template_response
        context = render.call_args.args[1]
        assert context["was_restricted"] is True
        assert context["service_restored"] is False


class TestCustomerTopupRoutes:
    def test_topup_page_renders_dedicated_template(self) -> None:
        from app.web.customer.routes import customer_billing_topup

        request = MagicMock()
        customer = {"subscriber_id": "sub-1", "account_id": "acct-1"}
        page_data = {
            "provider_type": "paystack",
            "provider_public_key": "pk_test",
            "customer_email": "test@example.com",
            "prepaid_balance": 2500,
            "min_amount": 1000,
            "max_amount": 500000,
            "preset_amounts": [1000, 2000, 5000],
        }
        template_response = MagicMock(name="template_response")

        with (
            patch(
                "app.web.customer.routes.get_current_customer_from_request",
                return_value=customer,
            ),
            patch(
                "app.web.customer.routes.customer_portal.get_topup_page",
                return_value=page_data,
            ),
            patch(
                "app.web.customer.routes.templates.TemplateResponse",
                return_value=template_response,
            ) as render,
        ):
            response = customer_billing_topup(request=request, db=MagicMock())

        assert response is template_response
        assert render.call_args.args[0] == "customer/billing/topup.html"
        context = render.call_args.args[1]
        assert "payment_reference" not in context
        assert context["active_page"] == "billing"

    def test_topup_intent_route_returns_json_payload(self) -> None:
        import json

        from app.web.customer.routes import customer_create_topup_intent

        request = MagicMock()
        customer = {"subscriber_id": "sub-1", "account_id": "acct-1"}

        with (
            patch(
                "app.web.customer.routes.get_current_customer_from_request",
                return_value=customer,
            ),
            patch(
                "app.web.customer.routes.customer_portal.create_topup_intent",
                return_value={
                    "intent_id": "intent-1",
                    "provider_type": "paystack",
                    "provider_public_key": "pk_test",
                    "reference": "ref-topup",
                    "requested_amount": 5000,
                    "currency": "NGN",
                    "checkout_metadata": {
                        "payment_flow": "account_topup",
                        "topup_intent_id": "intent-1",
                        "account_id": "acct-1",
                    },
                },
            ),
        ):
            response = customer_create_topup_intent(
                request=request,
                payload={"amount": 5000, "provider": "paystack"},
                db=MagicMock(),
            )

        assert response.status_code == 200
        payload = json.loads(response.body)
        assert payload["reference"] == "ref-topup"
        assert payload["checkout_metadata"]["topup_intent_id"] == "intent-1"

    def test_topup_success_marks_service_restored_after_post_payment_check(
        self,
    ) -> None:
        from app.web.customer.routes import customer_verify_topup

        request = MagicMock()
        customer = {"subscriber_id": "sub-1", "account_id": "acct-1"}
        result = {
            "payment": SimpleNamespace(receipt_number="RCT-T1"),
            "amount": 5000,
            "reference": "ref-topup-1",
            "already_recorded": False,
            "allocated_to_invoices": [],
            "allocated_total": 0,
            "credit_added": 5000,
            "available_balance": 5000,
            "policy_warnings": [],
        }

        template_response = MagicMock(name="template_response")

        with (
            patch(
                "app.web.customer.routes.get_current_customer_from_request",
                return_value=customer,
            ),
            patch(
                "app.web.customer.routes.customer_portal.verify_and_record_topup",
                return_value=result,
            ),
            patch(
                "app.web.customer.routes.is_subscriber_restricted",
                side_effect=[True, False],
            ),
            patch(
                "app.web.customer.routes.templates.TemplateResponse",
                return_value=template_response,
            ) as render,
        ):
            response = customer_verify_topup(
                request=request,
                reference="ref-topup-1",
                provider="paystack",
                db=MagicMock(),
            )

        assert response is template_response
        assert render.call_args.args[0] == "customer/billing/topup_success.html"
        context = render.call_args.args[1]
        assert context["was_restricted"] is True
        assert context["service_restored"] is True
        assert context["credit_added"] == 5000
