import pytest

from app.services.customer_portal_flow_billing import submit_payment_arrangement


def test_payment_arrangement_service_requires_terms_acceptance():
    with pytest.raises(ValueError, match="must agree to the payment arrangement terms"):
        submit_payment_arrangement(
            db=None,
            customer={},
            total_amount="1000",
            installments=3,
            frequency="monthly",
            start_date="2026-07-15",
            terms_accepted=False,
        )
