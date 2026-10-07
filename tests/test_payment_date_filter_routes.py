"""Exercise payment date parsing through real HTTP adapters and typed list scope."""

from collections.abc import Iterator
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from app.db import get_db
from app.services.list_query import ListQuery
from app.web import admin
from app.web.admin import billing_payments

PATHS = (
    "/admin/billing/payments",
    "/admin/billing/payments/export.csv",
    "/admin/billing/payments/unallocated",
)


@pytest.fixture
def payment_client(monkeypatch) -> Iterator[tuple[TestClient, Mock, Mock]]:
    app = FastAPI()
    app.include_router(billing_payments.router, prefix="/admin")
    app.dependency_overrides[get_db] = lambda: None
    for route in billing_payments.router.routes:
        for dependency in getattr(route, "dependencies", ()):
            if dependency.dependency is not None:
                app.dependency_overrides[dependency.dependency] = lambda: None

    service = billing_payments.web_billing_payments_service
    list_read = Mock(return_value={})
    csv_read = Mock(return_value=iter(("reference,amount\n",)))
    monkeypatch.setattr(service, "build_payments_list_data", list_read)
    monkeypatch.setattr(service, "stream_payments_csv", csv_read)
    monkeypatch.setattr(admin, "get_current_user", lambda request: {})
    monkeypatch.setattr(admin, "get_sidebar_stats", lambda db: {})
    monkeypatch.setattr(
        billing_payments.templates,
        "TemplateResponse",
        lambda *args, **kwargs: HTMLResponse("payments"),
    )
    with TestClient(app) as client:
        yield client, list_read, csv_read


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize(
    "dates,expected_start,expected_end",
    [
        ({}, None, None),
        ({"start_date": "", "end_date": ""}, None, None),
        ({"start_date": "2026-10-01", "end_date": ""}, "2026-10-01", None),
        ({"start_date": "", "end_date": "2026-10-06"}, None, "2026-10-06"),
        (
            {"start_date": "2026-10-01", "end_date": "2026-10-06"},
            "2026-10-01",
            "2026-10-06",
        ),
    ],
)
def test_payment_http_dates_preserve_typed_filter_scope(
    payment_client, path, dates, expected_start, expected_end
):
    client, list_read, csv_read = payment_client
    response = client.get(
        path,
        params={**dates, "status": "succeeded", "method": "cash", "search": "bank"},
    )

    assert response.status_code == 200
    read = csv_read if path.endswith(".csv") else list_read
    read.assert_called_once()
    query = read.call_args.kwargs["list_query"]
    assert isinstance(query, ListQuery)
    assert query.filter_value("start_date") == expected_start
    assert query.filter_value("end_date") == expected_end
    assert query.filter_value("status") == "succeeded"
    assert query.filter_value("method") == "cash"
    assert query.search == "bank"
    assert query.filter_value("unallocated_only") == (
        "true" if path.endswith("/unallocated") else None
    )


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize(
    "dates",
    [
        {"start_date": "not-a-date", "end_date": ""},
        {"start_date": "", "end_date": "2026-02-30"},
        {"start_date": "2026-10-06", "end_date": "2026-10-01"},
    ],
)
def test_invalid_payment_http_dates_fail_before_reading(payment_client, path, dates):
    client, list_read, csv_read = payment_client

    assert client.get(path, params=dates).status_code == 422
    list_read.assert_not_called()
    csv_read.assert_not_called()
