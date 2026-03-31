from __future__ import annotations

from app.services import paystack
from tests.mocks import FakeHTTPXResponse


def test_list_banks_returns_rows(monkeypatch, db_session):
    monkeypatch.setattr(paystack, "_get_secret_key", lambda db=None: "sk_test_123")

    def mock_get(url, **kwargs):
        assert url == f"{paystack.PAYSTACK_API_BASE}/bank"
        assert kwargs["params"] == {"country": "nigeria"}
        return FakeHTTPXResponse(
            json_data={
                "status": True,
                "data": [
                    {"name": "Access Bank", "code": "044"},
                    {"name": "GTBank", "code": "058"},
                ],
            }
        )

    monkeypatch.setattr(paystack.httpx, "get", mock_get)

    rows = paystack.list_banks(db_session, country="nigeria")

    assert rows == [
        {"name": "Access Bank", "code": "044"},
        {"name": "GTBank", "code": "058"},
    ]


def test_resolve_account_number_returns_payload(monkeypatch, db_session):
    monkeypatch.setattr(paystack, "_get_secret_key", lambda db=None: "sk_test_123")

    def mock_get(url, **kwargs):
        assert url == f"{paystack.PAYSTACK_API_BASE}/bank/resolve"
        assert kwargs["params"] == {
            "account_number": "0123456789",
            "bank_code": "058",
        }
        return FakeHTTPXResponse(
            json_data={
                "status": True,
                "data": {
                    "account_name": "DOTMAC TECHNOLOGIES LTD",
                    "account_number": "0123456789",
                },
            }
        )

    monkeypatch.setattr(paystack.httpx, "get", mock_get)

    payload = paystack.resolve_account_number(
        db_session,
        account_number="0123456789",
        bank_code="058",
    )

    assert payload["account_name"] == "DOTMAC TECHNOLOGIES LTD"
    assert payload["account_number"] == "0123456789"
