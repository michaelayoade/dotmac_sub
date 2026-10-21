"""Source ratchets for the bounded ordinary reseller session read migration."""

import ast
import inspect
from dataclasses import is_dataclass

from app.services import reseller_portal, reseller_session_standing
from app.services.sot_manifest import OwnerRole, TransactionMode
from app.services.sot_registry.registry import all_services


def test_precise_read_owner_is_registered():
    owner = next(
        s for s in all_services() if s.name == "app_sessions.reseller_standing"
    )
    assert owner.module == "app.services.reseller_session_standing"
    assert owner.contract.transaction.mode == TransactionMode.READ_ONLY
    assert all(c.role == OwnerRole.RESOLVER for c in owner.contract.concerns)
    assert len(owner.owns) == 2
    assert "impersonation" not in " ".join(owner.owns)
    ownership = next(
        value
        for value in owner.contract.authoritative_inputs
        if value.name == "current reseller ownership"
    )
    assert "historical provenance only" in ownership.source


def test_typed_immutable_queries_and_outcomes():
    for name in (
        "ResellerSessionStandingQuery",
        "ResellerSessionStanding",
        "ResellerSessionRefreshQuery",
        "ResellerSessionRefreshOutcome",
    ):
        value = getattr(reseller_session_standing, name)
        assert is_dataclass(value)
        assert value.__dataclass_params__.frozen


def test_read_owner_never_completes_or_mutates_transaction():
    tree = ast.parse(inspect.getsource(reseller_session_standing))
    forbidden = {
        "commit",
        "rollback",
        "flush",
        "add",
        "delete",
        "begin",
        "begin_nested",
    }
    for call in ast.walk(tree):
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute):
            assert call.func.attr not in forbidden
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "select"
        ):
            assert all(isinstance(argument, ast.Attribute) for argument in call.args)
    source = inspect.getsource(reseller_session_standing)
    assert "with db.no_autoflush:" in source
    assert "populate_existing=True" in source
    assert "db.get(" not in source
    assert "db.query(" not in source
    assert "fastapi" not in source


def test_first_class_standing_does_not_read_historical_subscriber_liveness():
    source = inspect.getsource(
        reseller_session_standing.resolve_reseller_session_standing
    )
    first_class_branch = source.split('if query.principal_type == "reseller_user":', 1)[
        1
    ].split('elif query.principal_type == "subscriber":', 1)[0]
    assert "ResellerUser.reseller_id" in first_class_branch
    assert "ResellerUser.is_active" in first_class_branch
    assert "ResellerUser.subscriber_id" not in first_class_branch
    assert "Subscriber.is_active" not in first_class_branch


def test_ordinary_consumers_have_no_truthy_impersonation_bypass():
    for function in (reseller_portal._resolve_context, reseller_portal.refresh_session):
        source = inspect.getsource(function)
        assert 'session.get("is_impersonation") is not True' in source
        assert "_ordinary_standing_query(session)" in source
        assert "resolve_reseller_session_" in source
    refresh = inspect.getsource(reseller_portal.refresh_session)
    assert "db is None or standing_query is None or created is None" in refresh
    assert refresh.index("resolve_reseller_session_refresh") < refresh.index(
        "store_session("
    )
    query = inspect.getsource(reseller_portal._ordinary_standing_query)
    assert 'session["principal_id"]' in query
    assert 'session["auth_session_id"]' in query
    assert 'session.get("principal_type")' in query
    assert "person_id" not in query
    assert "username" not in query
