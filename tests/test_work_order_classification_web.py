"""Browser admission, preserved forms and typed owner reads (fast unit lane)."""

import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.work_order import WorkOrder
from app.services.auth_dependencies import require_user_auth
from app.services.work_order_classification_contracts import (
    ClassificationEligibility,
    WorkOrderClassificationQuery,
)
from app.services.work_order_commands import work_order_commands
from app.services.work_order_templates import WorkOrderPurpose
from app.web.admin import work_order_classification as web


@pytest.fixture()
def browser(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    import app.web.admin as admin

    monkeypatch.setattr(admin, "get_current_user", lambda request: {"name": "Planner"})
    monkeypatch.setattr(admin, "get_sidebar_stats", lambda db: {})
    monkeypatch.setattr(
        web.templates.env,
        "loader",
        ChoiceLoader(
            [
                DictLoader(
                    {
                        "layouts/admin.html": "<html><body>{% block content %}{% endblock %}</body></html>"
                    }
                ),
                FileSystemLoader("templates"),
            ]
        ),
    )
    app = FastAPI()
    app.include_router(web.router, prefix="/admin")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user_auth] = lambda: {
        "roles": ["admin"],
        "scopes": [],
        "principal_id": str(uuid4()),
    }
    with TestClient(app) as client:
        yield client


def _seed(db: Session, *, kind: str = "customer", status: str = "draft") -> UUID:
    row = WorkOrder(
        public_id=f"sub-ui-{uuid4().hex}",
        title="Plan fiber <scope>",
        work_order_kind=kind,
        status=status,
        required_skills=[],
        tags=[],
        updated_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    db.add(row)
    db.flush()
    identity = row.id
    db.commit()
    return identity


def _url(identity: UUID) -> str:
    return f"/admin/dispatch/work-orders/{identity}/classification"


def _form(browser: TestClient, identity: UUID) -> dict[str, str]:
    response = browser.get(_url(identity))
    assert response.status_code == 200

    def hidden(name: str) -> str:
        match = re.search(rf'name="{name}"\s+value="([^"]*)"', response.text)
        assert match is not None
        return match.group(1)

    return {
        "command_id": hidden("command_id"),
        "expected_revision": hidden("expected_revision"),
        "_csrf_token": hidden("_csrf_token"),
        "purpose": "customer_deployment",
        "access_technology": "fiber",
        "reason": "Approved field scope",
    }


def test_page_and_submit_require_existing_write_permission(
    browser: TestClient, db_session: Session
) -> None:
    identity = _seed(db_session)
    browser.app.dependency_overrides[require_user_auth] = lambda: {
        "roles": [],
        "scopes": [],
        "principal_type": "system_user",
        "principal_id": str(uuid4()),
    }
    assert browser.get(_url(identity)).status_code == 403
    assert browser.post(_url(identity), data={}).status_code == 403


def test_post_without_matching_browser_csrf_is_rejected(
    browser: TestClient, db_session: Session
) -> None:
    identity = _seed(db_session)
    values = _form(browser, identity)
    values.pop("_csrf_token")
    assert browser.post(_url(identity), data=values).status_code == 403
    for invalid_token in ("different-browser-token", "invalid-δ-token"):
        values["_csrf_token"] = invalid_token
        assert browser.post(_url(identity), data=values).status_code == 403
    row = db_session.get(WorkOrder, identity)
    assert row is not None and row.purpose is None


def test_valid_post_uses_typed_owner_and_redirects_to_public_detail(
    browser: TestClient, db_session: Session
) -> None:
    identity = _seed(db_session)
    values = _form(browser, identity)
    response = browser.post(_url(identity), data=values, follow_redirects=False)
    assert response.status_code == 303
    row = db_session.get(WorkOrder, identity)
    assert row is not None and row.purpose == "customer_deployment"
    assert response.headers["location"].startswith(
        f"/admin/dispatch/work-orders/{row.public_id}?"
    )
    assert row.status == "draft" and row.completed_at is None
    snapshot = work_order_commands.classification_snapshot(
        db_session, query=WorkOrderClassificationQuery(work_order_id=identity)
    )
    assert snapshot.recommendation is not None
    assert snapshot.recommendation.use.value == "recommendation_only"


def test_stale_post_preserves_inputs_and_original_operation_identity(
    browser: TestClient, db_session: Session
) -> None:
    identity = _seed(db_session)
    values = _form(browser, identity)
    values["reason"] = "Preserve this reason <review>"
    row = db_session.get(WorkOrder, identity)
    assert row is not None
    row.title = "A newer planning change"
    db_session.commit()
    response = browser.post(_url(identity), data=values)
    assert response.status_code == 409
    assert (
        values["command_id"] in response.text
        and values["expected_revision"] in response.text
    )
    assert "Preserve this reason &lt;review&gt;" in response.text
    assert "Your submitted choices are preserved" in response.text
    assert re.search(r"<button[^>]*disabled", response.text)
    db_session.expire_all()
    assert db_session.get(WorkOrder, identity).purpose is None


def test_validation_preserves_and_escapes_submitted_values(
    browser: TestClient, db_session: Session
) -> None:
    identity = _seed(db_session)
    values = _form(browser, identity)
    values["purpose"] = "<script>alert(1)</script>"
    values["reason"] = "Retain the proposed scope"
    response = browser.post(_url(identity), data=values)
    assert response.status_code == 422
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
    assert "Retain the proposed scope" in response.text
    assert values["command_id"] in response.text
    assert 'aria-invalid="true"' in response.text


def test_owner_snapshot_supplies_kind_options_and_execution_blocker(
    browser: TestClient, db_session: Session
) -> None:
    identity = _seed(db_session, kind="infrastructure", status="completed")
    snapshot = work_order_commands.classification_snapshot(
        db_session, query=WorkOrderClassificationQuery(work_order_id=identity)
    )
    assert snapshot.eligibility == ClassificationEligibility.STATUS_BLOCKED
    assert snapshot.blocked_reason
    assert WorkOrderPurpose.INFRASTRUCTURE_ROLLOUT in snapshot.purpose_options
    assert WorkOrderPurpose.CUSTOMER_DEPLOYMENT not in snapshot.purpose_options
    response = browser.get(_url(identity))
    assert response.status_code == 200
    assert snapshot.blocked_reason in response.text
    assert re.search(r"<button[^>]*disabled", response.text)
    assert 'value="infrastructure_rollout"' in response.text
    assert 'value="customer_deployment"' not in response.text
