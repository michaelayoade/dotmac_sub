from __future__ import annotations

from decimal import Decimal

from app.services import funded_inactive_exposure as exposure


def _row(
    account_id: str,
    *,
    status: str,
    current: str,
    deposit: str = "0.00",
    open_ar: str = "0.00",
    is_active: bool = True,
    active_sibling_count: int = 0,
) -> dict[str, object]:
    return {
        "account_id": account_id,
        "subscriber_name": f"Subscriber {account_id[-1]}",
        "subscriber_status": status,
        "subscriber_is_active": is_active,
        "splynx_customer_id": "123",
        "current_available": current,
        "deposit": deposit,
        "open_ar": open_ar,
        "ticket_count": 1,
        "status_event_count": 2,
        "active_sibling_count": active_sibling_count,
        "active_sibling_account_ids": (
            "99999999-9999-4999-8999-999999999999" if active_sibling_count else ""
        ),
        "active_sibling_names": "Live sibling" if active_sibling_count else "",
        "latest_status_event_at": "2026-07-04T00:00:00+00:00",
        "updated_at": "2026-07-04T01:00:00+00:00",
    }


def test_funded_inactive_exposure_groups_statuses(monkeypatch):
    monkeypatch.setattr(
        exposure,
        "_rows",
        lambda _db, *, min_amount: [
            _row(
                "11111111-1111-4111-8111-111111111111",
                status="blocked",
                current="25.00",
            ),
            _row(
                "22222222-2222-4222-8222-222222222222",
                status="disabled",
                current="125.50",
                deposit="150.00",
                open_ar="24.50",
            ),
            _row(
                "33333333-3333-4333-8333-333333333333",
                status="suspended",
                current="60000.00",
            ),
        ],
    )

    result = exposure.funded_inactive_exposure(
        object(), material_amount=Decimal("50000.00")
    )

    assert result["ok"] is False
    assert result["inactive_positive_count"] == 3
    assert result["inactive_positive_total"] == "60150.50"
    assert result["blocked_count"] == 1
    assert result["blocked_total"] == "25.00"
    assert result["disabled_count"] == 1
    assert result["disabled_total"] == "125.50"
    assert result["suspended_count"] == 1
    assert result["suspended_total"] == "60000.00"
    assert result["refund_review_count"] == 2
    assert result["refund_review_total"] == "60125.50"
    assert result["material_count"] == 1
    assert result["by_status"]["suspended"]["material_count"] == 1
    assert result["samples"][0]["subscriber_status"] == "suspended"
    assert result["samples"][0]["current_available"] == "60000.00"


def test_blocked_only_exposure_is_reported_but_ok(monkeypatch):
    monkeypatch.setattr(
        exposure,
        "_rows",
        lambda _db, *, min_amount: [
            _row(
                "11111111-1111-4111-8111-111111111111",
                status="blocked",
                current="25.00",
            )
        ],
    )

    result = exposure.funded_inactive_exposure(object())

    assert result["ok"] is True
    assert result["inactive_positive_count"] == 1
    assert result["disabled_count"] == 0
    assert result["suspended_count"] == 0
    assert result["blocked_count"] == 1


def test_sample_limit_truncates_largest_rows(monkeypatch):
    monkeypatch.setattr(
        exposure,
        "_rows",
        lambda _db, *, min_amount: [
            _row(
                "11111111-1111-4111-8111-111111111111",
                status="blocked",
                current="10.00",
            ),
            _row(
                "22222222-2222-4222-8222-222222222222",
                status="blocked",
                current="30.00",
            ),
            _row(
                "33333333-3333-4333-8333-333333333333",
                status="blocked",
                current="20.00",
            ),
        ],
    )

    result = exposure.funded_inactive_exposure(object(), sample_limit=2)

    assert [sample["current_available"] for sample in result["samples"]] == [
        "30.00",
        "20.00",
    ]


def test_soft_deleted_funded_accounts_are_reported(monkeypatch):
    monkeypatch.setattr(
        exposure,
        "_rows",
        lambda _db, *, min_amount: [
            _row(
                "11111111-1111-4111-8111-111111111111",
                status="disabled",
                current="381191.00",
                is_active=False,
                active_sibling_count=1,
            )
        ],
    )

    result = exposure.funded_inactive_exposure(object())

    assert result["ok"] is False
    assert result["inactive_positive_count"] == 1
    assert result["disabled_count"] == 1
    assert result["refund_review_count"] == 1
    assert result["soft_deleted_count"] == 1
    assert result["soft_deleted_total"] == "381191.00"
    assert result["sibling_candidate_count"] == 1
    assert result["samples"][0]["subscriber_is_active"] is False
    assert result["samples"][0]["active_sibling_count"] == 1
    assert result["samples"][0]["active_sibling_names"] == "Live sibling"


def test_canceled_funded_accounts_require_refund_review(monkeypatch):
    monkeypatch.setattr(
        exposure,
        "_rows",
        lambda _db, *, min_amount: [
            _row(
                "11111111-1111-4111-8111-111111111111",
                status="canceled",
                current="50.00",
            )
        ],
    )

    result = exposure.funded_inactive_exposure(object())

    assert result["ok"] is False
    assert result["canceled_count"] == 1
    assert result["refund_review_count"] == 1
    assert result["refund_review_total"] == "50.00"
