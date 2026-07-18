from unittest.mock import MagicMock, patch

from app.models.subscriber import SubscriberStatus
from app.services.prepaid_funding_reconstruction import (
    PrepaidFundingBaselineMissingError,
)


def test_restricted_dashboard_renders_when_balance_authority_is_unavailable(
    db_session, subscriber
) -> None:
    from app.web.customer.routes import customer_dashboard

    subscriber.status = SubscriberStatus.blocked
    db_session.commit()

    customer = {
        "subscriber_id": str(subscriber.id),
        "account_id": str(subscriber.id),
    }
    request = MagicMock()
    response = MagicMock(name="template_response")

    with (
        patch(
            "app.web.customer.routes.get_current_customer_from_request",
            return_value=customer,
        ),
        patch(
            "app.services.customer_portal_context.get_total_outstanding_balance",
            side_effect=PrepaidFundingBaselineMissingError(
                "prepaid funding authority cutover has not been materialized"
            ),
        ),
        patch(
            "app.web.customer.routes.templates.TemplateResponse",
            return_value=response,
        ) as render,
    ):
        result = customer_dashboard(request=request, db=db_session)

    assert result is response
    assert render.call_args.args[0] == "customer/dashboard/restricted.html"
    context = render.call_args.args[1]
    assert context["balance_unavailable"] is True
    assert context["outstanding_balance"] is None


def test_restricted_dashboard_template_handles_unavailable_balance() -> None:
    from pathlib import Path

    template = Path("templates/customer/dashboard/restricted.html").read_text()

    assert "Balance temporarily unavailable" in template
    assert "not balance_unavailable and outstanding_balance > 0" in template
