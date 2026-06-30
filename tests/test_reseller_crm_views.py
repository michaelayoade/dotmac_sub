"""Reseller aggregations of the CRM mirrors: scoped to the reseller's customers."""

from __future__ import annotations

import uuid

from app.models.project_mirror import ProjectMirror
from app.models.quote_mirror import QuoteMirror
from app.models.subscriber import Reseller, Subscriber
from app.models.work_order_mirror import WorkOrderMirror
from app.services import reseller_crm_views


def _reseller(db) -> Reseller:
    r = Reseller(name=f"Reseller {uuid.uuid4().hex[:6]}", is_active=True)
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def _customer(db, reseller_id=None) -> Subscriber:
    sub = Subscriber(
        first_name="Cust",
        last_name="Omer",
        email=f"c-{uuid.uuid4().hex[:8]}@example.com",
        reseller_id=reseller_id,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def test_quotes_scoped_to_reseller_customers(db_session):
    reseller = _reseller(db_session)
    mine = _customer(db_session, reseller_id=reseller.id)
    other = _customer(db_session, reseller_id=None)  # not this reseller's

    db_session.add(
        QuoteMirror(
            crm_quote_id="qA",
            subscriber_id=mine.id,
            status="draft",
            currency="NGN",
            total="75000.00",
            deposit_amount="37500.00",
            payload={"id": "qA", "status": "draft", "total": "75000.00"},
        )
    )
    db_session.add(
        QuoteMirror(
            crm_quote_id="qOther",
            subscriber_id=other.id,
            status="draft",
            currency="NGN",
        )
    )
    db_session.commit()

    out = reseller_crm_views.quotes_for_reseller(db_session, str(reseller.id))
    assert out["total"] == 1
    assert out["open"] == 1
    item = out["quotes"][0]
    assert item["id"] == "qA"
    assert item["account_id"] == str(mine.id)
    assert item["account_name"]  # name resolved


def test_projects_and_work_orders_scoped(db_session):
    reseller = _reseller(db_session)
    mine = _customer(db_session, reseller_id=reseller.id)
    other = _customer(db_session, reseller_id=None)

    db_session.add(
        ProjectMirror(
            crm_project_id="pA",
            subscriber_id=mine.id,
            name="Install",
            status="open",
            progress_pct=40,
        )
    )
    db_session.add(
        ProjectMirror(
            crm_project_id="pOther", subscriber_id=other.id, name="X", status="open"
        )
    )
    db_session.add(
        WorkOrderMirror(
            crm_work_order_id="woA",
            subscriber_id=mine.id,
            title="Repair",
            status="dispatched",
        )
    )
    db_session.add(
        WorkOrderMirror(
            crm_work_order_id="woOther",
            subscriber_id=other.id,
            title="Y",
            status="scheduled",
        )
    )
    db_session.commit()

    projects = reseller_crm_views.projects_for_reseller(db_session, str(reseller.id))
    assert projects["total"] == 1
    assert projects["projects"][0]["id"] == "pA"
    assert projects["projects"][0]["progress_pct"] == 40
    assert projects["active"] == 1

    wos = reseller_crm_views.work_orders_for_reseller(db_session, str(reseller.id))
    assert wos["total"] == 1
    assert wos["work_orders"][0]["id"] == "woA"
    assert wos["work_orders"][0]["account_id"] == str(mine.id)
    assert wos["upcoming"] == 1


def test_empty_when_reseller_has_no_customers(db_session):
    reseller = _reseller(db_session)
    out = reseller_crm_views.quotes_for_reseller(db_session, str(reseller.id))
    assert out == {"quotes": [], "total": 0, "open": 0}
