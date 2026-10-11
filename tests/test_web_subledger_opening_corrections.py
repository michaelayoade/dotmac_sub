"""Admin correction of a captured customer-subledger opening position.

Covers the owner read queries (opening history and the read-only impact
preview), the admin adapter's fingerprint-bound confirmation, and the routes:
permissions, preview equals confirm, stale fingerprint refusal, and
validation errors.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal
from html import unescape
from pathlib import Path
from urllib.parse import unquote_plus
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db import get_db
from app.models.billing_shadow_verification import BillingCutoverVerificationRun
from app.models.catalog import BillingMode
from app.models.customer_subledger import (
    CustomerPostingGroup,
    CustomerSubledgerAuthorityCutover,
    CustomerSubledgerOpeningCorrection,
    CustomerSubledgerOpeningPosition,
)
from app.models.subscriber import Subscriber
from app.models.system_user import SystemUser
from app.services import web_subledger_opening_corrections as web_corrections
from app.services.access_resolution import PrepaidFundingDecision
from app.services.billing import subledger_opening as owner
from app.services.billing.subledger_opening import (
    CORRECTION_SCOPE,
    CustomerSubledgerOpeningsQuery,
    OpeningCorrectionEnforcementConsequence,
    OpeningPositionProvenance,
    PreviewCustomerSubledgerOpeningCorrectionQuery,
    list_customer_subledger_openings,
    preview_customer_subledger_opening_correction_impact,
)
from app.web.admin import billing_subledger_openings as routes

CUTOVER_AT = datetime(2026, 10, 8, 18, 0, tzinfo=UTC)
CAPTURE_REFERENCE = "finance-capture:2026-10-08/cohort-72"
BASE = "/admin/billing/accounts/{account_id}/subledger-opening/NGN/correction"


def _activate_authority(db) -> BillingCutoverVerificationRun:  # noqa: ANN001
    command_id = uuid4()
    run = BillingCutoverVerificationRun(
        phase="customer_subledger_phase3",
        cohort_name="pytest-opening-correction-ui",
        evidence_schema_version=1,
        policy_version="pytest",
        cutoff_at=CUTOVER_AT,
        observation_started_at=CUTOVER_AT,
        observation_ended_at=CUTOVER_AT,
        cohort_count=0,
        covered_count=0,
        unresolved_count=0,
        ambiguous_count=0,
        unexpected_unlinked_count=0,
        duplicate_count=0,
        shadow_variance_count=0,
        expected_difference_count=0,
        gap_count=0,
        overlap_count=0,
        source_fingerprint="a" * 64,
        result_fingerprint="b" * 64,
        currency_totals={},
        cohort_classification={},
        event_outcomes={},
        code_version="pytest",
        database_schema_version="652",
        idempotency_key=f"pytest-opening-correction-ui:{command_id}",
        command_id=command_id,
        correlation_id=command_id,
        actor="pytest:operator",
        reason="Reviewed test cutover",
        operator_approved_by="pytest:operator",
        operator_approved_at=CUTOVER_AT,
        finance_approved_by="pytest:finance",
        finance_approved_at=CUTOVER_AT,
        created_at=CUTOVER_AT,
    )
    db.add(run)
    db.flush()
    cutover_command = uuid4()
    db.add(
        CustomerSubledgerAuthorityCutover(
            verification_run_id=run.id,
            result_fingerprint="e" * 64,
            review_reference="pytest:approved-cutover",
            activated_by="pytest:operator",
            command_id=cutover_command,
            correlation_id=cutover_command,
            cutover_at=CUTOVER_AT,
        )
    )
    db.flush()
    return run


def _capture_opening(
    db,  # noqa: ANN001
    *,
    run: BillingCutoverVerificationRun,
    account: Subscriber,
    amount: Decimal = Decimal("0.00"),
) -> CustomerSubledgerOpeningPosition:
    command_id = uuid4()
    opening = CustomerSubledgerOpeningPosition(
        verification_run_id=run.id,
        account_id=account.id,
        currency="NGN",
        legacy_position=amount,
        shadow_position_before=Decimal("0.00"),
        opening_delta=amount,
        evidence_fingerprint="c" * 64,
        review_reference=CAPTURE_REFERENCE,
        captured_by="finance:capture",
        command_id=command_id,
        correlation_id=command_id,
        occurred_at=CUTOVER_AT,
        created_at=datetime(2026, 10, 9, 9, 30, tzinfo=UTC),
    )
    db.add(opening)
    db.flush()
    return opening


@pytest.fixture()
def opening_account(db_session):  # noqa: ANN001
    run = _activate_authority(db_session)
    account = Subscriber(
        first_name="Opening",
        last_name="Customer",
        email=f"opening-{uuid4().hex}@example.com",
        billing_mode=BillingMode.prepaid,
        billing_enabled=True,
    )
    db_session.add(account)
    db_session.flush()
    _capture_opening(db_session, run=run, account=account)
    db_session.commit()
    return account


@pytest.fixture()
def staff(db_session) -> SystemUser:  # noqa: ANN001
    user = SystemUser(
        first_name="Finance",
        last_name="Reviewer",
        email=f"finance-reviewer-{uuid4().hex}@example.com",
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    return user


def _values(**overrides: str) -> web_corrections.OpeningCorrectionFormValues:
    values = {
        "currency": "NGN",
        "corrected_opening_amount": "4500.00",
        "reason": "Confirmed variance: Splynx deposit of 4,500 omitted at capture.",
        "review_reference": "FIN-REVIEW-2026-10-09/ACC-0001",
    }
    values.update(overrides)
    return web_corrections.OpeningCorrectionFormValues(**values)


def _decision(
    available: str, required: str = "1000.00", **extra: object
) -> PrepaidFundingDecision:
    return PrepaidFundingDecision(
        account_id=str(uuid4()),
        available_balance=Decimal(available),
        required_balance=Decimal(required),
        currency="NGN",
        **extra,  # type: ignore[arg-type]
    )


def _pin_funding(monkeypatch, decision: PrepaidFundingDecision) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        owner, "resolve_prepaid_funding", lambda _db, _account, now=None: decision
    )


# --- owner queries --------------------------------------------------------


def test_opening_view_lists_capture_evidence_and_correction_history(
    db_session, opening_account, staff
):
    empty = list_customer_subledger_openings(
        db_session, CustomerSubledgerOpeningsQuery(account_id=uuid4())
    )
    assert empty.openings == ()
    assert empty.correction_available is False

    view = list_customer_subledger_openings(
        db_session, CustomerSubledgerOpeningsQuery(account_id=opening_account.id)
    )
    assert view.authority_active is True
    assert view.correction_available is True
    opening = view.opening("ngn")
    assert opening is not None
    assert opening.provenance is OpeningPositionProvenance.verification_run
    assert opening.captured_amount == Decimal("0.00")
    assert opening.current_amount == Decimal("0.00")
    assert opening.position_at == CUTOVER_AT
    assert opening.review_reference == CAPTURE_REFERENCE
    assert opening.evidence_fingerprint == "c" * 64
    assert opening.corrections == ()


def test_impact_preview_writes_nothing_and_exposes_owner_preview(
    db_session, opening_account, monkeypatch
):
    _pin_funding(monkeypatch, _decision("200.00"))
    query = PreviewCustomerSubledgerOpeningCorrectionQuery(
        account_id=opening_account.id,
        currency="NGN",
        corrected_opening_amount=Decimal("4500"),
        reason="Confirmed variance",
        review_reference="FIN-1",
    )
    before = db_session.scalar(select(func.count(CustomerPostingGroup.id)))

    impact = preview_customer_subledger_opening_correction_impact(db_session, query)

    owner_preview = owner.preview_customer_subledger_opening_correction(
        db_session, query
    )
    assert impact.preview == owner_preview
    assert impact.preview.previous_opening_amount == Decimal("0.00")
    assert impact.preview.delta == Decimal("4500.00")
    assert impact.current_available_balance == Decimal("200.00")
    assert impact.resulting_available_balance == Decimal("4700.00")
    assert impact.required_balance == Decimal("1000.00")
    assert impact.enforcement_consequence is (
        OpeningCorrectionEnforcementConsequence.restoration_eligible
    )
    assert not db_session.new and not db_session.dirty
    assert db_session.scalar(select(func.count(CustomerPostingGroup.id))) == before
    assert (
        db_session.scalar(select(func.count(CustomerSubledgerOpeningCorrection.id)))
        == 0
    )


@pytest.mark.parametrize(
    ("available", "amount", "expected"),
    [
        (
            "1500.00",
            "-1000",
            OpeningCorrectionEnforcementConsequence.suspension_eligible,
        ),
        ("1500.00", "250", OpeningCorrectionEnforcementConsequence.unchanged),
        ("100.00", "200", OpeningCorrectionEnforcementConsequence.unchanged),
    ],
)
def test_impact_preview_names_enforcement_consequence(
    db_session, opening_account, monkeypatch, available, amount, expected
):
    _pin_funding(monkeypatch, _decision(available))
    impact = preview_customer_subledger_opening_correction_impact(
        db_session,
        PreviewCustomerSubledgerOpeningCorrectionQuery(
            account_id=opening_account.id,
            currency="NGN",
            corrected_opening_amount=Decimal(amount),
            reason="Confirmed variance",
            review_reference="FIN-1",
        ),
    )
    assert impact.enforcement_consequence is expected
    assert impact.resulting_available_balance == (Decimal(available) + Decimal(amount))


def test_impact_preview_is_not_applicable_for_postpaid_or_unknown_funding(
    db_session, opening_account, monkeypatch
):
    query = PreviewCustomerSubledgerOpeningCorrectionQuery(
        account_id=opening_account.id,
        currency="NGN",
        corrected_opening_amount=Decimal("10"),
        reason="Confirmed variance",
        review_reference="FIN-1",
    )

    def _missing(*_args, **_kwargs):
        raise owner.PrepaidFundingBaselineMissingError("baseline missing")

    monkeypatch.setattr(owner, "resolve_prepaid_funding", _missing)
    unknown = preview_customer_subledger_opening_correction_impact(db_session, query)
    assert unknown.enforcement_consequence is (
        OpeningCorrectionEnforcementConsequence.undetermined
    )
    assert unknown.resulting_available_balance is None

    opening_account.billing_mode = BillingMode.postpaid
    db_session.flush()
    postpaid = preview_customer_subledger_opening_correction_impact(db_session, query)
    assert postpaid.enforcement_consequence is (
        OpeningCorrectionEnforcementConsequence.not_prepaid
    )
    assert postpaid.current_available_balance is None


# --- admin route harness ----------------------------------------------------


def _client(db_session, principal: dict) -> TestClient:  # noqa: ANN001
    app = FastAPI()
    app.include_router(routes.router, prefix="/admin")

    @app.middleware("http")
    async def _session_state(request: Request, call_next):  # noqa: ANN202
        request.state.auth = principal
        request.state.csrf_token = "csrf-test-token"
        return await call_next(request)

    def _db():  # noqa: ANN202
        yield db_session

    app.dependency_overrides[get_db] = _db
    return TestClient(app, raise_server_exceptions=True)


def _admin(staff: SystemUser) -> dict:
    return {
        "principal_id": str(staff.id),
        "principal_type": "system_user",
        "roles": ["admin"],
        "scopes": [],
    }


def _hidden(html: str) -> dict[str, str]:
    return {
        name: unescape(value)
        for name, value in re.findall(
            r'<input type="hidden" name="([^"]+)" value="([^"]*)">', html
        )
    }


def _form(values: web_corrections.OpeningCorrectionFormValues) -> dict[str, str]:
    return values.as_mapping()


def _preview(client: TestClient, account_id: UUID, **overrides: str):
    return client.post(
        BASE.format(account_id=account_id) + "/preview",
        data=_form(_values(**overrides)),
    )


def _route(path: str, method: str) -> APIRoute:
    for route in routes.router.routes:
        if (
            isinstance(route, APIRoute)
            and route.path == path
            and method in (route.methods or set())
        ):
            return route
    raise AssertionError(f"missing route {method} {path}")


# --- permissions ------------------------------------------------------------


def test_every_correction_route_requires_the_dedicated_permission():
    base = "/billing/accounts/{account_id}/subledger-opening/{currency}/correction"
    for path, method in (
        (base, "GET"),
        (f"{base}/preview", "POST"),
        (f"{base}/confirm", "POST"),
    ):
        route = _route(path, method)
        closures = [
            cell.cell_contents
            for dependency in route.dependant.dependencies
            for cell in (getattr(dependency.call, "__closure__", None) or ())
        ]
        assert CORRECTION_SCOPE in closures, f"{method} {path}"


def test_staff_without_permission_is_refused(db_session, opening_account, staff):
    principal = {
        "principal_id": str(staff.id),
        "principal_type": "system_user",
        "roles": ["support"],
        "scopes": ["billing:account:read"],
    }
    client = _client(db_session, principal)

    page = client.get(BASE.format(account_id=opening_account.id))
    preview = _preview(client, opening_account.id)

    assert page.status_code == 403
    assert preview.status_code == 403
    assert (
        db_session.scalar(select(func.count(CustomerSubledgerOpeningCorrection.id)))
        == 0
    )


def test_non_staff_principal_is_refused(db_session, opening_account):
    client = _client(
        db_session,
        {
            "principal_id": str(uuid4()),
            "principal_type": "api_key",
            "roles": ["admin"],
            "scopes": [],
        },
    )
    assert client.get(BASE.format(account_id=opening_account.id)).status_code == 403


def test_account_panel_shows_action_only_with_permission(db_session, opening_account):
    from types import SimpleNamespace

    from app.web.admin import billing_accounts

    view = web_corrections.account_opening_panel(
        db_session, account_id=opening_account.id
    )
    template = billing_accounts.templates.env.get_template(
        "admin/billing/_subledger_opening_panel.html"
    )

    def _render(keys: frozenset[str]) -> str:
        return template.render(
            request=SimpleNamespace(
                state=SimpleNamespace(auth={"permission_keys": keys})
            ),
            subledger_opening=view,
            opening_correction_permission=CORRECTION_SCOPE,
        )

    allowed = _render(frozenset({CORRECTION_SCOPE}))
    denied = _render(frozenset({"billing:account:read"}))

    assert 'data-testid="subledger-opening-panel"' in allowed
    assert CAPTURE_REFERENCE in allowed
    assert "No corrections recorded." in allowed
    assert 'data-testid="correct-opening-action"' in allowed
    assert CAPTURE_REFERENCE in denied
    assert 'data-testid="correct-opening-action"' not in denied
    assert web_corrections.account_opening_panel(db_session, account_id=uuid4()) is None


def test_account_billing_page_renders_opening_panel_and_history(
    db_session, opening_account, staff, monkeypatch
):
    from app.web.admin import billing_accounts

    _pin_funding(monkeypatch, _decision("200.00"))
    client = _client(db_session, _admin(staff))
    reviewed = _hidden(_preview(client, opening_account.id).text)
    client.post(
        BASE.format(account_id=opening_account.id) + "/confirm",
        data={**reviewed, "confirmed": "yes"},
        follow_redirects=False,
    )
    app = FastAPI()
    app.include_router(billing_accounts.router, prefix="/admin")

    @app.middleware("http")
    async def _session_state(request: Request, call_next):  # noqa: ANN202
        request.state.auth = _admin(staff)
        request.state.csrf_token = "csrf-test-token"
        return await call_next(request)

    def _db():  # noqa: ANN202
        yield db_session

    app.dependency_overrides[get_db] = _db

    page = TestClient(app).get(
        f"/admin/billing/accounts/{opening_account.id}"
        "?opening_correction_message=Opening+corrected"
    )

    assert page.status_code == 200, page.text
    assert 'data-testid="subledger-opening-panel"' in page.text
    assert 'data-testid="correct-opening-action"' in page.text
    assert 'data-testid="opening-correction-history"' in page.text
    assert "NGN 4,500.00" in page.text
    assert _values().review_reference in page.text
    assert "Opening corrected" in page.text


# --- preview, confirm, stale, validation --------------------------------------


def test_preview_renders_owner_values_and_confirm_applies_exact_fingerprint(
    db_session, opening_account, staff, monkeypatch
):
    _pin_funding(monkeypatch, _decision("200.00"))
    client = _client(db_session, _admin(staff))

    entry = client.get(BASE.format(account_id=opening_account.id))
    assert entry.status_code == 200
    assert "Preview correction" in entry.text

    preview = _preview(client, opening_account.id)
    assert preview.status_code == 200
    assert 'data-testid="opening-correction-preview"' in preview.text
    assert 'data-consequence="restoration_eligible"' in preview.text
    assert "NGN 4,700.00" in preview.text
    hidden = _hidden(preview.text)
    owner_preview = owner.preview_customer_subledger_opening_correction(
        db_session,
        PreviewCustomerSubledgerOpeningCorrectionQuery(
            account_id=opening_account.id,
            currency="NGN",
            corrected_opening_amount=Decimal("4500.00"),
            reason=_values().reason,
            review_reference=_values().review_reference,
        ),
    )
    assert hidden["preview_fingerprint"] == owner_preview.preview_fingerprint
    assert hidden["_csrf_token"] == "csrf-test-token"

    confirmed = client.post(
        BASE.format(account_id=opening_account.id) + "/confirm",
        data={**hidden, "confirmed": "yes"},
        follow_redirects=False,
    )

    assert confirmed.status_code == 303, confirmed.text
    location = unquote_plus(confirmed.headers["location"])
    assert "opening_correction_message=Opening corrected from NGN 0.00" in location
    correction = db_session.scalar(select(CustomerSubledgerOpeningCorrection))
    assert correction is not None
    assert correction.preview_fingerprint == owner_preview.preview_fingerprint
    assert correction.corrected_opening_amount == Decimal("4500.00")
    assert correction.delta == Decimal("4500.00")
    assert correction.review_reference == _values().review_reference
    assert correction.authorized_system_user_id == staff.id
    assert correction.applied_by == f"system_user:{staff.id}"
    assert correction.idempotency_key.startswith("subledger-opening-correction-admin:")

    history = list_customer_subledger_openings(
        db_session, CustomerSubledgerOpeningsQuery(account_id=opening_account.id)
    ).openings[0]
    assert history.current_amount == Decimal("4500.00")
    assert [record.correction_id for record in history.corrections] == [correction.id]

    replay = client.post(
        BASE.format(account_id=opening_account.id) + "/confirm",
        data={**hidden, "confirmed": "yes"},
        follow_redirects=False,
    )
    assert replay.status_code == 303
    assert "already recorded" in unquote_plus(replay.headers["location"])
    assert (
        db_session.scalar(select(func.count(CustomerSubledgerOpeningCorrection.id)))
        == 1
    )


def test_stale_fingerprint_is_refused_and_a_fresh_preview_is_issued(
    db_session, opening_account, staff, monkeypatch
):
    _pin_funding(monkeypatch, _decision("200.00"))
    client = _client(db_session, _admin(staff))
    reviewed = _hidden(_preview(client, opening_account.id).text)

    # Another reviewed correction lands after this preview was issued.
    other = _hidden(
        _preview(
            client,
            opening_account.id,
            corrected_opening_amount="100.00",
            review_reference="FIN-REVIEW-OTHER",
        ).text
    )
    applied = client.post(
        BASE.format(account_id=opening_account.id) + "/confirm",
        data={**other, "confirmed": "yes"},
        follow_redirects=False,
    )
    assert applied.status_code == 303

    stale = client.post(
        BASE.format(account_id=opening_account.id) + "/confirm",
        data={**reviewed, "confirmed": "yes"},
        follow_redirects=False,
    )

    assert stale.status_code == 409
    assert "changed after review" in stale.text
    fresh = _hidden(stale.text)
    assert fresh["preview_fingerprint"] != reviewed["preview_fingerprint"]
    assert "NGN 100.00" in stale.text
    assert (
        db_session.scalar(select(func.count(CustomerSubledgerOpeningCorrection.id)))
        == 1
    )


def test_tampered_hidden_input_cannot_reuse_a_reviewed_fingerprint(
    db_session, opening_account, staff, monkeypatch
):
    _pin_funding(monkeypatch, _decision("200.00"))
    client = _client(db_session, _admin(staff))
    reviewed = _hidden(_preview(client, opening_account.id).text)

    tampered = client.post(
        BASE.format(account_id=opening_account.id) + "/confirm",
        data={**reviewed, "corrected_opening_amount": "9000.00", "confirmed": "yes"},
        follow_redirects=False,
    )
    forged = client.post(
        BASE.format(account_id=opening_account.id) + "/confirm",
        data={**reviewed, "preview_fingerprint": "f" * 64, "confirmed": "yes"},
        follow_redirects=False,
    )

    assert tampered.status_code == 409
    assert forged.status_code == 409
    assert (
        db_session.scalar(select(func.count(CustomerSubledgerOpeningCorrection.id)))
        == 0
    )


def test_confirmation_is_bound_to_the_reviewing_actor(
    db_session, opening_account, staff, monkeypatch
):
    _pin_funding(monkeypatch, _decision("200.00"))
    reviewed = _hidden(
        _preview(_client(db_session, _admin(staff)), opening_account.id).text
    )
    second = SystemUser(
        first_name="Other",
        last_name="Admin",
        email=f"other-admin-{uuid4().hex}@example.com",
        is_active=True,
    )
    db_session.add(second)
    db_session.commit()

    response = _client(db_session, _admin(second)).post(
        BASE.format(account_id=opening_account.id) + "/confirm",
        data={**reviewed, "confirmed": "yes"},
        follow_redirects=False,
    )

    assert response.status_code == 409
    assert "changed; preview again" in response.text
    assert (
        db_session.scalar(select(func.count(CustomerSubledgerOpeningCorrection.id)))
        == 0
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"corrected_opening_amount": ""}, "Enter the corrected opening amount."),
        ({"corrected_opening_amount": "abc"}, "must be a number"),
        ({"corrected_opening_amount": "10.005"}, "at most two decimal places"),
        ({"corrected_opening_amount": "0.00"}, "already matches the current value"),
        ({"reason": "   "}, "A correction reason is required."),
        ({"review_reference": ""}, "durable finance review reference is required"),
        ({"reason": "x" * 501}, "500 characters or fewer"),
    ],
)
def test_preview_validation_errors_rerender_the_entry_form(
    db_session, opening_account, staff, monkeypatch, overrides, message
):
    _pin_funding(monkeypatch, _decision("200.00"))
    client = _client(db_session, _admin(staff))

    response = _preview(client, opening_account.id, **overrides)

    assert response.status_code == 400
    assert message in response.text
    assert 'aria-invalid="true"' in response.text
    assert "opening-correction-preview" not in response.text
    assert "confirmation_token" not in response.text


def test_confirm_requires_explicit_acknowledgement(
    db_session, opening_account, staff, monkeypatch
):
    _pin_funding(monkeypatch, _decision("200.00"))
    client = _client(db_session, _admin(staff))
    reviewed = _hidden(_preview(client, opening_account.id).text)

    response = client.post(
        BASE.format(account_id=opening_account.id) + "/confirm",
        data=reviewed,
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "Confirm the reviewed correction" in response.text
    assert (
        db_session.scalar(select(func.count(CustomerSubledgerOpeningCorrection.id)))
        == 0
    )


def test_missing_opening_redirects_back_to_the_account(db_session, staff):
    _activate_authority(db_session)
    db_session.commit()
    account_id = uuid4()
    client = _client(db_session, _admin(staff))

    response = client.get(BASE.format(account_id=account_id), follow_redirects=False)

    assert response.status_code == 303
    assert "opening_correction_error=" in response.headers["location"]


def test_adapter_calls_owners_with_keyword_arguments():
    source = Path("app/services/web_subledger_opening_corrections.py").read_text()

    assert "preview_customer_subledger_opening_correction_impact(db, query=query)" in (
        source
    )
    assert (
        "correct_customer_subledger_opening_position(\n        db,\n        command="
        in (source)
    )
    assert "list_customer_subledger_openings(\n        db, query=" in source
