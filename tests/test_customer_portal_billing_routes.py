from pathlib import Path
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

    def test_pay_intent_post_route_exists(self) -> None:
        from app.web.customer.routes import router

        routes = {
            (getattr(route, "path", ""), method)
            for route in router.routes
            for method in getattr(route, "methods", set())
        }
        assert ("/portal/billing/pay/intent", "POST") in routes


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
                "app.web.customer.routes.autopay_service.get_status",
                return_value={"enabled": False, "payment_method_id": None},
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
        assert context["payment_options"] == [
            {"provider_type": "paystack", "label": "Pay with Paystack"},
        ]

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

    def test_pay_intent_route_returns_json_payload(self) -> None:
        import json

        from app.web.customer.routes import customer_create_invoice_payment_intent

        request = MagicMock()
        request.url_for.return_value = "https://selfcare.test/portal/billing/pay/verify"
        customer = {"subscriber_id": "sub-1", "account_id": "acct-1"}

        with (
            patch(
                "app.web.customer.routes.get_current_customer_from_request",
                return_value=customer,
            ),
            patch(
                "app.web.customer.routes.customer_portal.create_invoice_payment_intent",
                return_value={
                    "provider_type": "paystack",
                    "provider_public_key": "pk_test",
                    "reference": "pay-ref",
                    "amount": 2500,
                    "currency": "NGN",
                    "checkout_metadata": {
                        "payment_flow": "invoice_payment",
                        "invoice_id": "inv-1",
                    },
                    "charged": False,
                    "checkout_url": None,
                },
            ) as create_intent,
        ):
            response = customer_create_invoice_payment_intent(
                request=request,
                payload={"invoice": "inv-1", "provider": "paystack"},
                db=MagicMock(),
            )

        assert response.status_code == 200
        payload = json.loads(response.body)
        assert payload["reference"] == "pay-ref"
        assert payload["checkout_metadata"]["invoice_id"] == "inv-1"
        assert create_intent.call_args.args[2] == "inv-1"

    def test_pay_intent_route_requires_invoice(self) -> None:
        import json

        from app.web.customer.routes import customer_create_invoice_payment_intent

        request = MagicMock()
        customer = {"subscriber_id": "sub-1", "account_id": "acct-1"}

        with patch(
            "app.web.customer.routes.get_current_customer_from_request",
            return_value=customer,
        ):
            response = customer_create_invoice_payment_intent(
                request=request, payload={"provider": "paystack"}, db=MagicMock()
            )

        assert response.status_code == 400
        assert "invoice is required" in json.loads(response.body)["detail"]

    def test_pay_intent_route_blocks_read_only_session(self) -> None:
        import json

        from app.web.customer.routes import customer_create_invoice_payment_intent

        request = MagicMock()
        customer = {"subscriber_id": "sub-1", "account_id": "acct-1", "read_only": True}

        with patch(
            "app.web.customer.routes.get_current_customer_from_request",
            return_value=customer,
        ):
            response = customer_create_invoice_payment_intent(
                request=request,
                payload={"invoice": "inv-1", "provider": "paystack"},
                db=MagicMock(),
            )

        assert response.status_code == 403
        assert "View-only" in json.loads(response.body)["detail"]

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


class TestSaveCardOnVerify:
    @staticmethod
    def _pay_result():
        return {
            "payment": SimpleNamespace(receipt_number="RCT-1"),
            "invoice": SimpleNamespace(id="inv-1", invoice_number="INV-1"),
            "amount": 5000,
            "reference": "ref-1",
        }

    @staticmethod
    def _topup_result():
        return {
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

    def test_pay_verify_saves_card_when_requested(self) -> None:
        from app.web.customer.routes import customer_verify_payment

        request = MagicMock()
        db = MagicMock()
        customer = {"subscriber_id": "sub-1", "account_id": "acct-1"}

        with (
            patch(
                "app.web.customer.routes.get_current_customer_from_request",
                return_value=customer,
            ),
            patch(
                "app.web.customer.routes.customer_portal.verify_and_record_payment",
                return_value=self._pay_result(),
            ),
            patch(
                "app.web.customer.routes.is_subscriber_restricted",
                return_value=False,
            ),
            patch(
                "app.web.customer.routes.customer_cards.capture_card_after_payment"
            ) as capture,
            patch(
                "app.web.customer.routes.templates.TemplateResponse",
                return_value=MagicMock(name="template_response"),
            ),
        ):
            customer_verify_payment(
                request=request,
                reference="ref-1",
                provider="paystack",
                save_card=True,
                db=db,
            )

        capture.assert_called_once_with(db, "acct-1", "ref-1", "paystack")

    def test_pay_verify_does_not_save_card_by_default(self) -> None:
        from app.web.customer.routes import customer_verify_payment

        request = MagicMock()
        customer = {"subscriber_id": "sub-1", "account_id": "acct-1"}

        with (
            patch(
                "app.web.customer.routes.get_current_customer_from_request",
                return_value=customer,
            ),
            patch(
                "app.web.customer.routes.customer_portal.verify_and_record_payment",
                return_value=self._pay_result(),
            ),
            patch(
                "app.web.customer.routes.is_subscriber_restricted",
                return_value=False,
            ),
            patch(
                "app.web.customer.routes.customer_cards.capture_card_after_payment"
            ) as capture,
            patch(
                "app.web.customer.routes.templates.TemplateResponse",
                return_value=MagicMock(name="template_response"),
            ),
        ):
            customer_verify_payment(
                request=request,
                reference="ref-1",
                provider="paystack",
                save_card=False,
                db=MagicMock(),
            )

        capture.assert_not_called()

    def test_pay_verify_skips_save_when_verification_fails(self) -> None:
        from app.web.customer.routes import customer_verify_payment

        request = MagicMock()
        customer = {"subscriber_id": "sub-1", "account_id": "acct-1"}

        with (
            patch(
                "app.web.customer.routes.get_current_customer_from_request",
                return_value=customer,
            ),
            patch(
                "app.web.customer.routes.customer_portal.verify_and_record_payment",
                side_effect=ValueError("verification failed"),
            ),
            patch(
                "app.web.customer.routes.is_subscriber_restricted",
                return_value=False,
            ),
            patch(
                "app.web.customer.routes.customer_cards.capture_card_after_payment"
            ) as capture,
            patch(
                "app.web.customer.routes.templates.TemplateResponse",
                return_value=MagicMock(name="template_response"),
            ),
        ):
            customer_verify_payment(
                request=request,
                reference="ref-1",
                provider="paystack",
                save_card=True,
                db=MagicMock(),
            )

        capture.assert_not_called()

    def test_pay_verify_uses_safe_message_for_gateway_exception(self) -> None:
        from app.web.customer.routes import (
            PAYMENT_VERIFICATION_ERROR_MESSAGE,
            customer_verify_payment,
        )

        request = MagicMock()
        customer = {"subscriber_id": "sub-1", "account_id": "acct-1"}
        template_response = MagicMock(name="template_response")

        with (
            patch(
                "app.web.customer.routes.get_current_customer_from_request",
                return_value=customer,
            ),
            patch(
                "app.web.customer.routes.customer_portal.verify_and_record_payment",
                side_effect=RuntimeError("gateway raw stack detail"),
            ),
            patch(
                "app.web.customer.routes.is_subscriber_restricted",
                return_value=False,
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
                save_card=True,
                db=MagicMock(),
            )

        assert response is template_response
        assert render.call_args.args[0] == "customer/errors/400.html"
        assert render.call_args.args[1]["message"] == PAYMENT_VERIFICATION_ERROR_MESSAGE
        assert "gateway raw stack detail" not in render.call_args.args[1]["message"]
        assert render.call_args.kwargs["status_code"] == 400

    def test_invoice_pay_template_blocks_paystack_without_email(self) -> None:
        template = Path("templates/customer/billing/pay.html").read_text()

        assert "const checkoutEmail =" in template
        assert "Add an email address to your account before paying with Paystack." in template
        assert "email: checkoutEmail" in template

    def test_topup_verify_saves_card_when_requested(self) -> None:
        from app.web.customer.routes import customer_verify_topup

        request = MagicMock()
        db = MagicMock()
        customer = {"subscriber_id": "sub-1", "account_id": "acct-1"}

        with (
            patch(
                "app.web.customer.routes.get_current_customer_from_request",
                return_value=customer,
            ),
            patch(
                "app.web.customer.routes.customer_portal.verify_and_record_topup",
                return_value=self._topup_result(),
            ),
            patch(
                "app.web.customer.routes.is_subscriber_restricted",
                return_value=False,
            ),
            patch(
                "app.web.customer.routes.customer_cards.capture_card_after_payment"
            ) as capture,
            patch(
                "app.web.customer.routes.templates.TemplateResponse",
                return_value=MagicMock(name="template_response"),
            ),
        ):
            customer_verify_topup(
                request=request,
                reference="ref-topup-1",
                provider="paystack",
                save_card=True,
                db=db,
            )

        capture.assert_called_once_with(db, "acct-1", "ref-topup-1", "paystack")

    def test_topup_verify_does_not_save_card_by_default(self) -> None:
        from app.web.customer.routes import customer_verify_topup

        request = MagicMock()
        customer = {"subscriber_id": "sub-1", "account_id": "acct-1"}

        with (
            patch(
                "app.web.customer.routes.get_current_customer_from_request",
                return_value=customer,
            ),
            patch(
                "app.web.customer.routes.customer_portal.verify_and_record_topup",
                return_value=self._topup_result(),
            ),
            patch(
                "app.web.customer.routes.is_subscriber_restricted",
                return_value=False,
            ),
            patch(
                "app.web.customer.routes.customer_cards.capture_card_after_payment"
            ) as capture,
            patch(
                "app.web.customer.routes.templates.TemplateResponse",
                return_value=MagicMock(name="template_response"),
            ),
        ):
            customer_verify_topup(
                request=request,
                reference="ref-topup-1",
                provider="paystack",
                save_card=False,
                db=MagicMock(),
            )

        capture.assert_not_called()


class TestCustomerAutopayRoutes:
    def test_autopay_routes_registered(self) -> None:
        from app.web.customer.routes import router

        routes = {
            (getattr(route, "path", ""), method)
            for route in router.routes
            for method in getattr(route, "methods", set())
        }
        assert ("/portal/billing/autopay/enable", "POST") in routes
        assert ("/portal/billing/autopay/disable", "POST") in routes

    def test_enable_calls_service_and_redirects(self) -> None:
        from app.web.customer.routes import customer_autopay_enable

        request = MagicMock()
        db = MagicMock()
        customer = {"subscriber_id": "sub-1", "account_id": "acct-1"}

        with (
            patch(
                "app.web.customer.routes.get_current_customer_from_request",
                return_value=customer,
            ),
            patch("app.web.customer.routes.autopay_service.enable") as enable,
        ):
            response = customer_autopay_enable(
                request=request, payment_method_id=None, db=db
            )

        enable.assert_called_once_with(db, "acct-1", None)
        assert response.status_code == 303
        assert response.headers["location"].startswith(
            "/portal/billing/topup?autopay_success="
        )

    def test_enable_with_card_passes_method_id(self) -> None:
        from app.web.customer.routes import customer_autopay_enable

        request = MagicMock()
        db = MagicMock()
        customer = {"subscriber_id": "sub-1", "account_id": "acct-1"}

        with (
            patch(
                "app.web.customer.routes.get_current_customer_from_request",
                return_value=customer,
            ),
            patch("app.web.customer.routes.autopay_service.enable") as enable,
        ):
            customer_autopay_enable(request=request, payment_method_id="pm-1", db=db)

        enable.assert_called_once_with(db, "acct-1", "pm-1")

    def test_enable_without_card_redirects_with_error(self) -> None:
        from app.web.customer.routes import customer_autopay_enable

        request = MagicMock()
        customer = {"subscriber_id": "sub-1", "account_id": "acct-1"}

        with (
            patch(
                "app.web.customer.routes.get_current_customer_from_request",
                return_value=customer,
            ),
            patch(
                "app.web.customer.routes.autopay_service.enable",
                side_effect=ValueError("Add a saved card before enabling autopay"),
            ),
        ):
            response = customer_autopay_enable(
                request=request, payment_method_id=None, db=MagicMock()
            )

        assert response.status_code == 303
        assert "autopay_error=" in response.headers["location"]

    def test_disable_calls_service_and_redirects(self) -> None:
        from app.web.customer.routes import customer_autopay_disable

        request = MagicMock()
        db = MagicMock()
        customer = {"subscriber_id": "sub-1", "account_id": "acct-1"}

        with (
            patch(
                "app.web.customer.routes.get_current_customer_from_request",
                return_value=customer,
            ),
            patch("app.web.customer.routes.autopay_service.disable") as disable,
        ):
            response = customer_autopay_disable(request=request, db=db)

        disable.assert_called_once_with(db, "acct-1")
        assert response.status_code == 303
        assert response.headers["location"].startswith(
            "/portal/billing/topup?autopay_success="
        )

    def test_autopay_routes_require_login(self) -> None:
        from app.web.customer.routes import (
            customer_autopay_disable,
            customer_autopay_enable,
        )

        with patch(
            "app.web.customer.routes.get_current_customer_from_request",
            return_value=None,
        ):
            enable_response = customer_autopay_enable(
                request=MagicMock(), payment_method_id=None, db=MagicMock()
            )
            disable_response = customer_autopay_disable(
                request=MagicMock(), db=MagicMock()
            )

        assert enable_response.status_code == 303
        assert enable_response.headers["location"] == "/portal/auth/login"
        assert disable_response.status_code == 303
        assert disable_response.headers["location"] == "/portal/auth/login"
