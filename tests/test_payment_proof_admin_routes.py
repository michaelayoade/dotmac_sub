"""Admin payment-proof back office: page registration + receipt file auth."""

import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.db import get_db
from app.services import payment_proofs as svc
from app.services.auth_dependencies import require_user_auth


def _routes(router) -> set[tuple[str, str]]:
    return {
        (getattr(route, "path", ""), method)
        for route in router.routes
        for method in getattr(route, "methods", set())
    }


class TestAdminWebRouteRegistration:
    def test_list_detail_file_and_actions_registered(self) -> None:
        from app.web.admin.billing_payment_proofs import router

        routes = _routes(router)
        assert ("/billing/payment-proofs", "GET") in routes
        assert ("/billing/payment-proofs/{proof_id}", "GET") in routes
        assert ("/billing/payment-proofs/{proof_id}/file", "GET") in routes
        assert ("/billing/payment-proofs/{proof_id}/verify", "POST") in routes
        assert ("/billing/payment-proofs/{proof_id}/reject", "POST") in routes

    def test_router_is_mounted_on_admin(self) -> None:
        from app.web.admin import router as admin_router

        paths = {getattr(route, "path", "") for route in admin_router.routes}
        assert "/admin/billing/payment-proofs" in paths
        assert "/admin/billing/payment-proofs/{proof_id}" in paths

    def test_every_route_declares_a_billing_permission_guard(self) -> None:
        from app.web.admin.billing_payment_proofs import router

        for route in router.routes:
            names = set()
            stack = [route.dependant]
            while stack:
                dep = stack.pop()
                call = getattr(dep, "call", None)
                if call is not None:
                    names.add(getattr(call, "__name__", ""))
                stack.extend(getattr(dep, "dependencies", []) or [])
            assert "_require_permission" in names, (
                f"{route.path} has no permission guard"
            )


class TestAdminWebVerifyRejectHandlers:
    def test_verify_redirects_to_detail_on_success(self) -> None:
        from app.web.admin.billing_payment_proofs import payment_proofs_verify

        proof_id = uuid.uuid4()
        with patch(
            "app.web.admin.billing_payment_proofs.web_payment_proofs_service"
        ) as service:
            response = payment_proofs_verify(
                request=MagicMock(),
                proof_id=proof_id,
                amount="4500.00",
                auto_allocate="no",
                review_notes="checked",
                db=MagicMock(),
                auth={"principal_id": "admin-1"},
            )
        kwargs = service.verify_proof.call_args.kwargs
        assert kwargs["amount"] == "4500.00"
        assert kwargs["auto_allocate"] is False
        assert kwargs["verified_by"] == "admin-1"
        assert response.status_code == 303
        assert response.headers["location"].startswith(
            f"/admin/billing/payment-proofs/{proof_id}"
        )
        assert "message=" in response.headers["location"]

    def test_verify_redirects_with_error_when_service_rejects(self) -> None:
        from app.web.admin.billing_payment_proofs import payment_proofs_verify

        proof_id = uuid.uuid4()
        with patch(
            "app.web.admin.billing_payment_proofs.web_payment_proofs_service"
        ) as service:
            service.verify_proof.side_effect = HTTPException(
                status_code=409, detail="Reference already verified"
            )
            response = payment_proofs_verify(
                request=MagicMock(),
                proof_id=proof_id,
                amount="",
                auto_allocate="yes",
                review_notes="",
                db=MagicMock(),
                auth={"principal_id": "admin-1"},
            )
        assert response.status_code == 303
        assert "error=" in response.headers["location"]

    def test_reject_passes_reason_through(self) -> None:
        from app.web.admin.billing_payment_proofs import payment_proofs_reject

        proof_id = uuid.uuid4()
        with patch(
            "app.web.admin.billing_payment_proofs.web_payment_proofs_service"
        ) as service:
            response = payment_proofs_reject(
                request=MagicMock(),
                proof_id=proof_id,
                review_notes="No matching transfer",
                db=MagicMock(),
                auth={"principal_id": "admin-2"},
            )
        kwargs = service.reject_proof.call_args.kwargs
        assert kwargs["review_notes"] == "No matching transfer"
        assert kwargs["verified_by"] == "admin-2"
        assert response.status_code == 303


# ---------------------------------------------------------------------------
# Receipt file endpoint auth (API): admin any, customer own only.
# ---------------------------------------------------------------------------


def _subscriber(db_session, email: str):
    from app.models.subscriber import Subscriber

    sub = Subscriber(first_name="Proof", last_name="Owner", email=email)
    db_session.add(sub)
    db_session.commit()
    return sub


@pytest.fixture()
def proof_env(db_session, tmp_path, monkeypatch):
    """Two subscribers; the first owns a proof whose receipt really exists."""
    monkeypatch.setattr(svc, "_UPLOAD_DIR", tmp_path)
    owner = _subscriber(db_session, "proof.owner@example.com")
    other = _subscriber(db_session, "proof.other@example.com")
    receipt = tmp_path / "receipt.png"
    receipt.write_bytes(b"\x89PNG-not-really-but-fine")
    proof = svc.submit_proof(
        db_session,
        str(owner.id),
        submitted_by=str(owner.id),
        amount="5000",
        reference="TRF-FILE",
        file_path=str(receipt),
    )
    return {"owner": owner, "other": other, "proof": proof, "receipt": receipt}


def _client(db_session, principal: dict) -> TestClient:
    from app.api.payment_proofs import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    def _db():
        yield db_session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_user_auth] = lambda: principal
    for route in router.routes:
        dependant = getattr(route, "dependant", None)
        for dependency in getattr(dependant, "dependencies", []) or []:
            call = getattr(dependency, "call", None)
            if getattr(call, "__name__", "") != "_require_permission":
                continue

            def _permission_guard(principal=principal):
                if "admin" not in set(principal.get("roles") or []):
                    raise HTTPException(status_code=403, detail="Forbidden")
                return principal

            app.dependency_overrides[call] = _permission_guard
    return TestClient(app)


def _admin_principal() -> dict:
    admin_id = str(uuid.uuid4())
    return {
        "roles": ["admin"],
        "scopes": [],
        "principal_type": "system_user",
        "principal_id": admin_id,
        "subscriber_id": admin_id,
    }


def _customer_principal(sub) -> dict:
    return {
        "roles": [],
        "scopes": [],
        "principal_type": "subscriber",
        "principal_id": str(sub.id),
        "subscriber_id": str(sub.id),
    }


class TestProofFileEndpointAuth:
    def test_admin_can_fetch_any_proof_file(self, db_session, proof_env) -> None:
        from app.api.payment_proofs import payment_proof_file

        resp = payment_proof_file(
            proof_id=proof_env["proof"]["id"],
            db=db_session,
        )
        assert resp.status_code == 200
        assert resp.media_type == "image/png"
        assert resp.body == proof_env["receipt"].read_bytes()

    def test_owner_can_fetch_their_own_proof_file(self, db_session, proof_env) -> None:
        from app.api.payment_proofs import my_payment_proof_file

        resp = my_payment_proof_file(
            proof_id=proof_env["proof"]["id"],
            db=db_session,
            principal=_customer_principal(proof_env["owner"]),
        )
        assert resp.status_code == 200
        assert resp.body == proof_env["receipt"].read_bytes()

    def test_other_customer_cannot_fetch_someone_elses_file(
        self, db_session, proof_env
    ) -> None:
        from app.api.payment_proofs import my_payment_proof_file

        with pytest.raises(HTTPException) as exc:
            my_payment_proof_file(
                proof_id=proof_env["proof"]["id"],
                db=db_session,
                principal=_customer_principal(proof_env["other"]),
            )
        assert exc.value.status_code == 404

    def test_customer_cannot_use_admin_file_route(self, db_session, proof_env) -> None:
        from app.api.payment_proofs import router

        route = next(
            route
            for route in router.routes
            if getattr(route, "path", "") == "/payment-proofs/admin/{proof_id}/file"
        )
        dependency_names = {
            getattr(dependency.call, "__name__", "")
            for dependency in route.dependant.dependencies
        }
        assert "_require_permission" in dependency_names

    def test_unknown_proof_is_404(self, db_session, proof_env) -> None:
        from app.api.payment_proofs import payment_proof_file

        with pytest.raises(HTTPException) as exc:
            payment_proof_file(proof_id=str(uuid.uuid4()), db=db_session)
        assert exc.value.status_code == 404

    def test_missing_file_on_disk_is_404(self, db_session, proof_env, tmp_path):
        proof_env["receipt"].unlink()
        from app.api.payment_proofs import payment_proof_file

        with pytest.raises(HTTPException) as exc:
            payment_proof_file(proof_id=proof_env["proof"]["id"], db=db_session)
        assert exc.value.status_code == 404

    def test_path_traversal_outside_upload_dir_is_404(
        self, db_session, proof_env
    ) -> None:
        """A file_path pointing outside uploads/payment_proofs must never be
        served, even to an admin."""
        evil = svc.submit_proof(
            db_session,
            str(proof_env["owner"].id),
            submitted_by=str(proof_env["owner"].id),
            amount="5000",
            reference="TRF-EVIL",
            file_path="/etc/passwd",
        )
        from app.api.payment_proofs import payment_proof_file

        with pytest.raises(HTTPException) as exc:
            payment_proof_file(proof_id=evil["id"], db=db_session)
        assert exc.value.status_code == 404


class TestAdminPagesRender:
    def _client(self, db_session) -> TestClient:
        from app.web.admin.billing_payment_proofs import router as web_router

        app = FastAPI()
        app.include_router(web_router, prefix="/admin")

        def _db():
            yield db_session

        app.dependency_overrides[get_db] = _db
        app.dependency_overrides[require_user_auth] = lambda: _admin_principal()
        return TestClient(app)

    def test_list_and_detail_pages_render(self, db_session, proof_env) -> None:
        client = self._client(db_session)
        with (
            patch("app.web.admin.get_current_user", return_value={"id": "admin"}),
            patch("app.web.admin.get_sidebar_stats", return_value={}),
        ):
            resp = client.get("/admin/billing/payment-proofs")
            assert resp.status_code == 200
            assert "Bank Transfer Proofs" in resp.text
            assert "TRF-FILE" in resp.text
            assert "sticky left-0 z-30 min-w-56" in resp.text
            assert "sticky left-0 z-20 min-w-56" in resp.text
            assert (
                f'href="/admin/billing/payment-proofs/{proof_env["proof"]["id"]}"'
                in resp.text
            )

            detail = client.get(
                f"/admin/billing/payment-proofs/{proof_env['proof']['id']}"
            )
            assert detail.status_code == 200
            # Claimed amount prefills the verify form; both review forms render.
            assert 'name="amount"' in detail.text
            assert 'value="5000.00"' in detail.text
            assert "/verify" in detail.text
            assert "/reject" in detail.text

    def test_detail_page_shows_duplicate_warning_and_error(
        self, db_session, proof_env
    ) -> None:
        svc.submit_proof(
            db_session,
            str(proof_env["owner"].id),
            submitted_by=str(proof_env["owner"].id),
            amount="5000",
            reference="TRF-FILE",
            file_path=str(proof_env["receipt"]),
        )
        client = self._client(db_session)
        with (
            patch("app.web.admin.get_current_user", return_value={"id": "admin"}),
            patch("app.web.admin.get_sidebar_stats", return_value={}),
        ):
            detail = client.get(
                f"/admin/billing/payment-proofs/{proof_env['proof']['id']}"
                "?error=Reference+already+verified"
            )
            assert detail.status_code == 200
            assert "Possible duplicate submission" in detail.text
            assert "Reference already verified" in detail.text

    def test_unknown_proof_renders_404_page(self, db_session, proof_env) -> None:
        client = self._client(db_session)
        with (
            patch("app.web.admin.get_current_user", return_value={"id": "admin"}),
            patch("app.web.admin.get_sidebar_stats", return_value={}),
        ):
            resp = client.get(f"/admin/billing/payment-proofs/{uuid.uuid4()}")
            assert resp.status_code == 404


class TestWebAdminFileRoute:
    def test_streams_receipt_for_staff(self, db_session, proof_env) -> None:
        from app.web.admin.billing_payment_proofs import payment_proofs_file

        response = payment_proofs_file(
            proof_id=uuid.UUID(proof_env["proof"]["id"]), db=db_session
        )
        assert response.media_type == "image/png"
        assert str(response.path) == str(proof_env["receipt"].resolve())

    def test_unknown_proof_raises_404(self, db_session, proof_env) -> None:
        from app.web.admin.billing_payment_proofs import payment_proofs_file

        with pytest.raises(HTTPException) as exc:
            payment_proofs_file(proof_id=uuid.uuid4(), db=db_session)
        assert exc.value.status_code == 404
