"""Admin referrals web surface (Phase 3 §2.6, PR 12): route guards, the
``web_referrals`` context builders, admin action flows through the native
``Referrals`` service (qualify override → issue reward → reject), and the
RBAC seeding for the Phase 3 sales keys (``crm:quote:*``,
``crm:sales_order:*``)."""

from __future__ import annotations

import importlib.util
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from app.models.billing import CreditNote
from app.models.domain_settings import DomainSetting, SettingDomain, SettingValueType
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services import web_referrals
from app.services.referrals import referrals
from app.web.admin import crm_referrals as admin_referrals_web


def _unique_email() -> str:
    return f"refweb-{uuid.uuid4().hex[:10]}@example.com"


def _subscriber(db, *, status=SubscriberStatus.active) -> Subscriber:
    sub = Subscriber(
        first_name="Refer",
        last_name="Rer",
        email=_unique_email(),
        status=status,
        is_active=True,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _program(db, *, enabled=True, amount="2500", auto_approve=None):
    rows = {
        "referral_program_enabled": (
            "true" if enabled else "false",
            SettingValueType.boolean,
        ),
        "referral_reward_amount": (amount, SettingValueType.string),
    }
    if auto_approve is not None:
        rows["referral_auto_approve_reward"] = (
            "true" if auto_approve else "false",
            SettingValueType.boolean,
        )
    for key, (text, value_type) in rows.items():
        db.add(
            DomainSetting(
                domain=SettingDomain.subscriber,
                key=key,
                value_type=value_type,
                value_text=text,
                is_active=True,
            )
        )
    db.commit()


def _captured_referral(db, referrer=None, **capture_kwargs):
    referrer = referrer or _subscriber(db)
    code = referrals.ensure_code(db, str(referrer.id))
    capture_kwargs.setdefault("email", _unique_email())
    return referrals.capture(db, code=code.code, **capture_kwargs), referrer


# ── route guards (crm:lead:*, same keys as the staff API) ────────────────────


def _get_route(module_router, path: str, method: str) -> APIRoute:
    for route in module_router.routes:
        if (
            isinstance(route, APIRoute)
            and route.path == path
            and method in route.methods
        ):
            return route
    raise AssertionError(f"Route not found: {method} {path}")


def _contains_value(value, expected: str) -> bool:
    if isinstance(value, str):
        return value == expected
    if isinstance(value, (tuple, list, set)):
        return any(_contains_value(item, expected) for item in value)
    if isinstance(value, dict):
        return any(_contains_value(item, expected) for item in value.values())
    return False


def _route_has_permission(module_router, path: str, method: str, expected: str) -> bool:
    route = _get_route(module_router, path, method)
    for dependency in route.dependant.dependencies:
        call = dependency.call
        closure = getattr(call, "__closure__", None) or ()
        for cell in closure:
            if _contains_value(cell.cell_contents, expected):
                return True
    return False


def test_router_is_mounted_on_admin():
    from app.web.admin import router as admin_router

    paths = {getattr(route, "path", "") for route in admin_router.routes}
    assert "/admin/referrals" in paths
    assert "/admin/referrals/{referral_id}" in paths
    assert "/admin/referrals/{referral_id}/qualify" in paths
    assert "/admin/referrals/{referral_id}/issue-reward" in paths
    assert "/admin/referrals/{referral_id}/reject" in paths


def test_read_routes_require_crm_lead_read():
    for path in ("/referrals", "/referrals/{referral_id}"):
        assert _route_has_permission(
            admin_referrals_web.router, path, "GET", "crm:lead:read"
        )


def test_action_routes_require_crm_lead_write():
    for path in (
        "/referrals/{referral_id}/qualify",
        "/referrals/{referral_id}/issue-reward",
        "/referrals/{referral_id}/reject",
    ):
        assert _route_has_permission(
            admin_referrals_web.router, path, "POST", "crm:lead:write"
        )


# ── list context ─────────────────────────────────────────────────────────────


def test_list_data_rows_stats_and_links(db_session):
    _program(db_session)
    referral, referrer = _captured_referral(db_session, name="Ada Prospect")

    data = web_referrals.list_data(db_session, page=1, per_page=25)
    assert data["total"] >= 1
    assert data["stats"]["total"] >= 1
    assert data["stats"]["pending"] >= 1
    assert data["program"]["enabled"] is True
    assert data["program_settings_url"] == web_referrals.PROGRAM_SETTINGS_URL

    row = next(r for r in data["referrals"] if r["id"] == str(referral.id))
    assert row["referrer_href"] == f"/admin/customers/person/{referrer.id}"
    assert row["referred_href"] == (
        f"/admin/customers/person/{referral.referred_subscriber_id}"
    )
    assert row["status"] == "pending"
    assert row["reward"] == "—"  # nothing earned yet
    assert row["can_qualify"] is True
    assert row["can_issue"] is False
    assert row["can_reject"] is True


def test_list_data_filters(db_session):
    _program(db_session)
    pending, _ = _captured_referral(db_session)
    qualified, _ = _captured_referral(db_session)
    referrals.qualify_override(db_session, str(qualified.id))

    only_qualified = web_referrals.list_data(db_session, status="qualified")
    ids = {r["id"] for r in only_qualified["referrals"]}
    assert str(qualified.id) in ids
    assert str(pending.id) not in ids
    assert only_qualified["status_filter"] == "qualified"

    by_reward = web_referrals.list_data(db_session, reward_status="pending")
    ids = {r["id"] for r in by_reward["referrals"]}
    assert str(qualified.id) in ids  # qualified w/o auto-approve → reward pending
    assert str(pending.id) not in ids  # captured rows sit at reward "none"

    # Unknown filter values are cleared, never a 400 from a stale bookmark.
    bad = web_referrals.list_data(db_session, status="bogus", reward_status="nope")
    assert bad["status_filter"] is None
    assert bad["reward_status_filter"] is None


def test_detail_data_shape_and_missing(db_session):
    _program(db_session)
    referral, referrer = _captured_referral(db_session, name="Ada Prospect")

    detail = web_referrals.detail_data(db_session, referral_id=str(referral.id))
    assert detail is not None
    assert detail["referral"].id == referral.id
    assert detail["row"]["referrer"].startswith("Refer")
    assert detail["capture"]["name"] == "Ada Prospect"
    assert detail["code"] is not None
    assert detail["lead_id"] == str(referral.referred_lead_id)
    assert detail["reward_credit_id"] is None

    assert web_referrals.detail_data(db_session, referral_id=str(uuid.uuid4())) is None
    assert web_referrals.detail_data(db_session, referral_id="not-a-uuid") is None


# ── admin action flows through the service ───────────────────────────────────


def test_qualify_override_forces_pending_to_qualified(db_session):
    _program(db_session, amount="2500")
    referral, _ = _captured_referral(db_session)
    # Referred prospect is NOT active — the automatic path would refuse.
    result = referrals.qualify_override(db_session, str(referral.id))
    assert result.status == "qualified"
    assert result.reward_status == "pending"
    assert result.reward_amount == Decimal("2500")
    assert result.qualified_at is not None


def test_qualify_override_auto_approve_and_expired_rescue(db_session):
    _program(db_session, auto_approve=True)
    referral, _ = _captured_referral(db_session)
    referral.status = "expired"  # window lapsed before activation
    db_session.commit()

    result = referrals.qualify_override(db_session, str(referral.id))
    assert result.status == "qualified"
    assert result.reward_status == "approved"


def test_qualify_override_guards(db_session):
    _program(db_session)
    referral, _ = _captured_referral(db_session)
    referrals.reject(db_session, str(referral.id), "dup")
    with pytest.raises(HTTPException) as exc:
        referrals.qualify_override(db_session, str(referral.id))
    assert exc.value.status_code == 409

    with pytest.raises(HTTPException) as exc:
        referrals.qualify_override(db_session, str(uuid.uuid4()))
    assert exc.value.status_code == 404


def test_admin_flow_qualify_override_then_issue_reward(db_session):
    """The §2.6 happy path end-to-end: capture → qualify override → issue
    reward lands exactly one account credit with the CRM-era idempotency key,
    and the detail context flips its action gates at each step."""
    _program(db_session, amount="2500")
    referral, referrer = _captured_referral(db_session)

    referrals.qualify_override(db_session, str(referral.id))
    detail = web_referrals.detail_data(db_session, referral_id=str(referral.id))
    assert detail["row"]["can_qualify"] is False
    assert detail["row"]["can_issue"] is True

    result = referrals.issue_reward(db_session, str(referral.id))
    assert result.status == "rewarded"
    assert result.reward_status == "issued"

    entry = db_session.get(CreditNote, result.metadata_["reward_credit_id"])
    assert entry is not None
    assert entry.total == Decimal("2500")
    assert f"[ref:referral:{referral.id}]" in str(entry.memo)

    detail = web_referrals.detail_data(db_session, referral_id=str(referral.id))
    assert detail["reward_credit_id"] == str(entry.id)
    assert not (
        detail["row"]["can_qualify"]
        or detail["row"]["can_issue"]
        or detail["row"]["can_reject"]
    )


def test_post_handlers_drive_service_and_redirect(db_session):
    """The POST handlers are thin PRG wrappers: success → 303 to the detail
    page with a message, service conflict → 303 with the error surfaced."""
    from unittest.mock import MagicMock

    _program(db_session, amount="2500")
    referral, _ = _captured_referral(db_session)
    rid = str(referral.id)

    response = admin_referrals_web.referral_qualify(
        request=MagicMock(), referral_id=rid, db=db_session
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/admin/referrals/{rid}?message=")
    db_session.refresh(referral)
    assert referral.status == "qualified"

    response = admin_referrals_web.referral_reject(
        request=MagicMock(), referral_id=rid, reason="changed mind", db=db_session
    )
    assert response.status_code == 303
    db_session.refresh(referral)
    assert referral.status == "rejected"
    assert "Rejected: changed mind" in (referral.notes or "")

    # Rejected → issue-reward conflicts (409 in the service) → error redirect.
    response = admin_referrals_web.referral_issue_reward(
        request=MagicMock(), referral_id=rid, db=db_session
    )
    assert response.status_code == 303
    assert f"/admin/referrals/{rid}?error=" in response.headers["location"]


def test_service_list_reward_status_filter(db_session):
    _program(db_session)
    referral, _ = _captured_referral(db_session)
    referrals.qualify_override(db_session, str(referral.id))

    items = referrals.list(db_session, reward_status="pending")
    assert str(referral.id) in {str(r.id) for r in items}
    assert all(r.reward_status == "pending" for r in items)

    with pytest.raises(HTTPException) as exc:
        referrals.list(db_session, reward_status="bogus")
    assert exc.value.status_code == 400


# ── RBAC seeding (§6 PR 12) ──────────────────────────────────────────────────

SALES_KEYS = (
    "crm:quote:read",
    "crm:quote:write",
    "crm:sales_order:read",
    "crm:sales_order:write",
)


def _seed_rbac_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "seed" / "seed_rbac.py"
    spec = importlib.util.spec_from_file_location("seed_rbac_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sales_permission_keys_are_seeded():
    seed = _seed_rbac_module()
    seeded = {key for key, _ in seed.DEFAULT_PERMISSIONS}
    for key in SALES_KEYS + ("crm:lead:read", "crm:lead:write"):
        assert key in seeded, f"{key} not seeded in RBAC"


def test_sales_permission_keys_admin_implicit_only():
    """Deliberate decision (§6 PR 12): the sales keys ride admin's wildcard
    grant and are assigned to no seeded non-admin role — sub has no seeded
    sales role. They stay UI-assignable for a future one."""
    seed = _seed_rbac_module()
    for key in SALES_KEYS:
        assert key in seed.ROLE_PERMISSIONS["admin"]
        assert key not in seed.ADMIN_ONLY_PERMISSION_KEYS  # UI-assignable
        for role, keys in seed.ROLE_PERMISSIONS.items():
            if role != "admin":
                assert key not in keys, f"{key} unexpectedly granted to {role}"
