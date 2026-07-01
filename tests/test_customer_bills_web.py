from __future__ import annotations

from app.web.customer import bills


def test_customer_bill_error_message_unwraps_structured_detail():
    message = bills._error_message(
        {"code": "insufficient_balance", "message": "Insufficient wallet balance"}
    )

    assert message == "Insufficient wallet balance"
