from datetime import UTC, datetime, timedelta

import pytest
from starlette.requests import Request

from app.models.admin_whats_new import AdminWhatsNewItem
from app.services import admin_whats_new
from app.web.admin import system_whats_new


def test_get_visible_items_limits_and_orders_items(db_session):
    now = datetime.now(UTC)
    db_session.add_all(
        [
            AdminWhatsNewItem(
                title="Featured",
                message="Featured message",
                button_text="Open",
                button_link="/admin/dashboard",
                status="featured",
                created_at=now - timedelta(minutes=1),
                updated_at=now - timedelta(minutes=1),
            ),
            AdminWhatsNewItem(
                title="Newest Active",
                message="Newest active message",
                button_text="Open",
                button_link="/admin/dashboard",
                status="active",
                created_at=now,
                updated_at=now,
            ),
            AdminWhatsNewItem(
                title="Older Active",
                message="Older active message",
                button_text="Open",
                button_link="/admin/dashboard",
                status="active",
                created_at=now - timedelta(minutes=2),
                updated_at=now - timedelta(minutes=2),
            ),
            AdminWhatsNewItem(
                title="Oldest Active",
                message="Oldest active message",
                button_text="Open",
                button_link="/admin/dashboard",
                status="active",
                created_at=now - timedelta(minutes=3),
                updated_at=now - timedelta(minutes=3),
            ),
            AdminWhatsNewItem(
                title="Hidden Draft",
                message="Draft message",
                button_text="Open",
                button_link="/admin/dashboard",
                status="draft",
                created_at=now - timedelta(minutes=4),
                updated_at=now - timedelta(minutes=4),
            ),
            AdminWhatsNewItem(
                title="Hidden Inactive",
                message="Inactive message",
                button_text="Open",
                button_link="/admin/dashboard",
                status="inactive",
                created_at=now - timedelta(minutes=5),
                updated_at=now - timedelta(minutes=5),
            ),
            AdminWhatsNewItem(
                title="Expired Active",
                message="Expired message",
                button_text="Open",
                button_link="/admin/dashboard",
                status="active",
                ends_at=now - timedelta(minutes=1),
                created_at=now - timedelta(minutes=6),
                updated_at=now - timedelta(minutes=6),
            ),
            AdminWhatsNewItem(
                title="Future Active",
                message="Future message",
                button_text="Open",
                button_link="/admin/dashboard",
                status="active",
                starts_at=now + timedelta(minutes=5),
                created_at=now - timedelta(minutes=7),
                updated_at=now - timedelta(minutes=7),
            ),
        ]
    )
    db_session.commit()

    visible = admin_whats_new.get_visible_items(db_session, now=now, limit=4)

    assert [item.title for item in visible] == [
        "Featured",
        "Newest Active",
        "Older Active",
        "Oldest Active",
    ]


def test_serialize_for_dashboard_collects_non_empty_benefits(db_session):
    item = AdminWhatsNewItem(
        title="Slide",
        message="Slide message",
        benefit_one="First",
        benefit_two="",
        benefit_three="Third",
        button_text="Open",
        button_link="/admin/dashboard",
        status="active",
    )
    db_session.add(item)
    db_session.commit()

    payload = admin_whats_new.serialize_for_dashboard([item])

    assert payload[0]["benefits"] == ["First", "Third"]


def test_parse_form_values_rejects_invalid_start_datetime():
    with pytest.raises(ValueError, match="Start date must be a valid ISO date/time."):
        admin_whats_new.parse_form_values(
            {
                "title": "Slide",
                "message": "Message",
                "button_text": "Open",
                "button_link": "/admin/dashboard",
                "starts_at": "not-a-date",
            }
        )


def test_whats_new_create_returns_form_error_for_invalid_datetime(
    db_session, monkeypatch
):
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/admin/system/whats-new/new",
            "headers": [(b"host", b"testserver")],
        }
    )
    monkeypatch.setattr(
        system_whats_new,
        "parse_form_data_sync",
        lambda request: {
            "title": "Slide",
            "message": "Message",
            "button_text": "Open",
            "button_link": "/admin/dashboard",
            "status": "active",
            "starts_at": "not-a-date",
            "ends_at": "",
        },
    )
    monkeypatch.setattr(
        system_whats_new,
        "_base_context",
        lambda request, db, active_page="settings-hub": {"request": request},
    )

    response = system_whats_new.whats_new_create(request, db_session)

    assert response.status_code == 400
    assert response.context["error"] == "Start date must be a valid ISO date/time."
    assert response.context["form_values"]["starts_at"] == "not-a-date"


def test_whats_new_index_ignores_invalid_status_filter(db_session, monkeypatch):
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/admin/system/whats-new",
            "query_string": b"status=invalid&status_error=invalid",
            "headers": [(b"host", b"testserver")],
        }
    )
    monkeypatch.setattr(
        system_whats_new,
        "_base_context",
        lambda request, db, active_page="settings-hub": {"request": request},
    )

    response = system_whats_new.whats_new_index(
        request, status="invalid", db=db_session
    )

    assert response.context["status_filter"] == ""
    assert response.context["status_error"] == "invalid"


def test_whats_new_status_update_uses_error_query_for_invalid_status(
    db_session, monkeypatch
):
    item = AdminWhatsNewItem(
        title="Slide",
        message="Slide message",
        button_text="Open",
        button_link="/admin/dashboard",
        status="draft",
    )
    db_session.add(item)
    db_session.commit()
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"/admin/system/whats-new/{item.id}/status",
            "headers": [(b"host", b"testserver")],
        }
    )
    monkeypatch.setattr(
        system_whats_new,
        "parse_form_data_sync",
        lambda request: {"status": "invalid"},
    )

    response = system_whats_new.whats_new_update_status(
        request, str(item.id), db_session
    )

    assert response.status_code == 303
    assert (
        response.headers["location"] == "/admin/system/whats-new?status_error=invalid"
    )
