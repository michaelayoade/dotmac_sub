import pytest

from app.models.subscriber import Reseller, UserType
from app.services import web_admin_resellers as web_admin_resellers_service


def _create_reseller(db_session, name: str = "Reseller A") -> Reseller:
    reseller = Reseller(name=name, code=f"{name[:3].upper()}-001")
    db_session.add(reseller)
    db_session.commit()
    db_session.refresh(reseller)
    return reseller


def test_reseller_permissions_are_seeded_and_role_builder_assignable():
    """reseller:read/write must be seeded and buildable into new roles."""
    from scripts.seed.seed_rbac import (
        ADMIN_ONLY_PERMISSION_KEYS,
        DEFAULT_PERMISSIONS,
    )

    seeded = {key for key, _ in DEFAULT_PERMISSIONS}
    for perm in ("reseller:read", "reseller:write"):
        assert perm in seeded, f"{perm} must be seeded"
        assert perm not in ADMIN_ONLY_PERMISSION_KEYS, (
            f"{perm} must be role-builder-assignable"
        )


def test_reseller_list_query_normalizes_status_and_page_size():
    svc = web_admin_resellers_service
    # default + unknown status collapse to "active"; off-menu page size to default
    assert svc.build_reseller_list_query().filter_value("status") == "active"
    assert svc.build_reseller_list_query(status="bogus").filter_value("status") == (
        "active"
    )
    assert svc.build_reseller_list_query(status="all").filter_value("status") == "all"
    assert svc.build_reseller_list_query(per_page=37).per_page == 25
    assert svc.build_reseller_list_query(per_page=200).per_page == 200
    assert svc.build_reseller_list_query(page=0).page == 1
    assert svc.build_reseller_list_query().sort_by == "name"


def test_list_page_context_can_include_inactive_resellers(db_session):
    active = _create_reseller(db_session, "Active Reseller")
    inactive = _create_reseller(db_session, "Inactive Reseller")
    inactive.is_active = False
    db_session.commit()

    active_context = web_admin_resellers_service.list_page_context(
        db_session, page=1, per_page=25
    )
    inactive_context = web_admin_resellers_service.list_page_context(
        db_session, page=1, per_page=25, status_filter="inactive"
    )
    all_context = web_admin_resellers_service.list_page_context(
        db_session, page=1, per_page=25, status_filter="all"
    )

    assert [item.id for item in active_context["resellers"]] == [active.id]
    assert [item.id for item in inactive_context["resellers"]] == [inactive.id]
    assert {item.id for item in all_context["resellers"]} == {active.id, inactive.id}


def test_link_existing_subscriber_to_reseller_rejects_non_customer(
    db_session, subscriber
):
    reseller = _create_reseller(db_session, "Reseller Link Test")
    with pytest.raises(ValueError, match="Only customer subscribers"):
        web_admin_resellers_service.link_existing_subscriber_to_reseller(
            db_session,
            reseller_id=str(reseller.id),
            subscriber_id=str(subscriber.id),
        )


def test_link_existing_subscriber_to_reseller_links_customer(db_session, subscriber):
    reseller = _create_reseller(db_session, "Reseller Link Test 2")
    subscriber.user_type = UserType.customer
    db_session.commit()

    linked = web_admin_resellers_service.link_existing_subscriber_to_reseller(
        db_session,
        reseller_id=str(reseller.id),
        subscriber_id=str(subscriber.id),
    )

    assert linked is True
    db_session.refresh(subscriber)
    assert subscriber.reseller_id == reseller.id
