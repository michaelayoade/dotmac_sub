"""Work-order errors become HTTP responses only in the app adapter."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.errors import register_error_handlers
from app.services.work_order_errors import WorkOrderCommandError


@pytest.mark.parametrize(
    ("kind", "expected_status"),
    [
        ("invalid", 422),
        ("forbidden", 403),
        ("not_found", 404),
        ("conflict", 409),
    ],
)
def test_work_order_error_maps_at_http_boundary(kind, expected_status):
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/api/work-order-error")
    def work_order_error():
        raise WorkOrderCommandError("work_order_test", "Rejected by owner", kind=kind)

    response = TestClient(app).get(
        "/api/work-order-error", headers={"accept": "application/json"}
    )

    assert response.status_code == expected_status
    assert response.json()["code"] == "work_order_test"
    assert response.json()["message"] == "Rejected by owner"
