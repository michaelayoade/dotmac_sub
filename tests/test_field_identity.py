"""Fast non-authoritative identity lifecycle and disclosure regression tests."""

from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.models.field_identity import (
    FieldIdentityReceipt,
    FieldIdentityRevision,
    IdentityState,
)
from app.models.stored_file import StoredFile
from app.models.system_user import SystemUser
from app.services import field_identity as owner
from app.services.field_identity_contracts import (
    REVIEW_PERMISSION,
    WRITE_SCOPE,
    IdentityAccess,
    ReviewIdentity,
    SubmitIdentity,
)
from app.services.file_storage import PreparedFileUpload, file_uploads
from app.services.owner_commands import CommandContext


class PreparedObjectStorage:
    """Synthetic prepared objects exist; object transport is outside this test."""

    def exists(self, key: str) -> bool:
        return True


@pytest.fixture(autouse=True)
def prepared_object_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(file_uploads, "storage", PreparedObjectStorage())


@pytest.fixture
def actors(db_session):
    ids = tuple(uuid4() for _ in range(3))
    for n, uid in enumerate(ids):
        db_session.add(
            SystemUser(
                id=uid,
                first_name="Synthetic",
                last_name="Tester",
                email=f"{uid}@example.invalid",
                is_active=True,
            )
        )
    db_session.commit()
    return ids


def context(uid, key=None, reason="Manual evidence reviewed"):
    return CommandContext.system(
        actor=f"user:{uid}",
        scope=WRITE_SCOPE,
        reason=reason,
        idempotency_key=key or str(uuid4()),
    )


def upload(uid, portrait=True, key="one"):
    kind = "field_identity_portrait" if portrait else "field_identity_evidence"
    return PreparedFileUpload(
        owner_subscriber_id=None,
        entity_type=kind,
        entity_id=str(uid),
        original_filename="synthetic.png",
        storage_key=f"attachments/{kind}/{uid}/{key}.png",
        file_size=123,
        content_type="image/png",
        checksum="a" * 64,
        uploaded_by=None,
    )


def command(uid, revision=0):
    return SubmitIdentity(
        context(uid),
        IdentityAccess(uid),
        uid,
        revision,
        "Synthetic Engineer",
        "Synthetic Company",
        upload(uid),
        upload(uid, False),
    )


def review_command(subject, reviewer, decision=IdentityState.reviewed, version=1):
    return ReviewIdentity(
        context(reviewer),
        IdentityAccess(reviewer, frozenset({REVIEW_PERMISSION})),
        subject,
        1,
        version,
        decision,
    )


def test_manual_review_and_minimal_card(db_session, actors):
    subject, reviewer, _ = actors
    assert owner.approved_customer_card(db_session, subject_id=subject) is None
    db_session.rollback()
    pending = owner.submit(db_session, command=command(subject))
    assert pending.state == IdentityState.pending
    assert not db_session.in_transaction()
    assert owner.approved_customer_card(db_session, subject_id=subject) is None
    db_session.rollback()
    result = owner.review(db_session, command=review_command(subject, reviewer))
    assert result.version == 2
    card = owner.approved_customer_card(db_session, subject_id=subject)
    assert card and card.verified and card.verification_state == IdentityState.reviewed
    assert (
        card.reviewed_at
        and not hasattr(card, "evidence_file_id")
        and not hasattr(card, "review_reason")
    )


def test_self_review_denied_even_with_permission(db_session, actors):
    subject, _, _ = actors
    owner.submit(db_session, command=command(subject))
    with pytest.raises(owner.FieldIdentityError, match="denied"):
        owner.review(db_session, command=review_command(subject, subject))
    assert not db_session.in_transaction()


@pytest.mark.parametrize("grant", ["*", "operations:*", "operations:technician:*"])
def test_independent_reviewer_wildcard_grants(db_session, actors, grant):
    subject, reviewer, _ = actors
    owner.submit(db_session, command=command(subject))
    request = replace(
        review_command(subject, reviewer),
        access=IdentityAccess(reviewer, frozenset({grant})),
    )
    result = owner.review(db_session, command=request)
    assert result.state == IdentityState.reviewed


def test_detail_response_cannot_be_cached(db_session, actors):
    from fastapi import Response

    from app.api.field.identity import get_identity

    subject, _, _ = actors
    owner.submit(db_session, command=command(subject))
    response = Response()
    get_identity(
        subject_id=subject,
        response=response,
        auth={
            "principal_id": str(subject),
            "principal_type": "system_user",
            "permission_keys": frozenset(),
        },
        db=db_session,
    )
    assert response.headers["Cache-Control"] == "private, no-store"


def test_unrelated_actor_cannot_read_or_submit(db_session, actors):
    subject, _, other = actors
    owner.submit(db_session, command=command(subject))
    with pytest.raises(owner.FieldIdentityError, match="denied"):
        owner.detail(db_session, access=IdentityAccess(other), subject_id=subject)
    db_session.rollback()
    with pytest.raises(owner.FieldIdentityError, match="denied"):
        owner.submit(
            db_session,
            command=replace(
                command(subject, 1),
                access=IdentityAccess(other),
                context=context(other),
            ),
        )


def test_stale_review_and_rejection_require_resubmission(db_session, actors):
    subject, reviewer, _ = actors
    owner.submit(db_session, command=command(subject))
    with pytest.raises(owner.FieldIdentityError, match="stale revision"):
        owner.review(db_session, command=review_command(subject, reviewer, version=2))
    owner.review(
        db_session, command=review_command(subject, reviewer, IdentityState.rejected)
    )
    with pytest.raises(owner.FieldIdentityError, match="invalid request"):
        owner.review(db_session, command=review_command(subject, reviewer, version=2))
    assert owner.approved_customer_card(db_session, subject_id=subject) is None


def test_replacement_and_revocation_hide_approved_portrait(db_session, actors):
    subject, reviewer, _ = actors
    owner.submit(db_session, command=command(subject))
    owner.review(db_session, command=review_command(subject, reviewer))
    owner.review(
        db_session, command=review_command(subject, reviewer, IdentityState.revoked, 2)
    )
    with pytest.raises(owner.FieldIdentityError, match="denied"):
        owner.approved_portrait(db_session, subject_id=subject, revision=1)
    db_session.rollback()
    owner.submit(db_session, command=command(subject, 1))
    assert owner.approved_customer_card(db_session, subject_id=subject) is None


def test_exact_replay_conflict_and_access_recheck(db_session, actors):
    subject, _, _ = actors
    cmd = command(subject)
    first = owner.submit(db_session, command=cmd)
    assert owner.submit(db_session, command=cmd) == first
    with pytest.raises(owner.FieldIdentityError, match="idempotency conflict"):
        owner.submit(db_session, command=replace(cmd, display_name="Different"))
    user = db_session.get(SystemUser, subject)
    user.is_active = False
    db_session.commit()
    with pytest.raises(owner.FieldIdentityError, match="denied"):
        owner.submit(db_session, command=cmd)


def test_failure_rolls_back_identity_receipt_and_files(db_session, actors, monkeypatch):
    subject, _, _ = actors

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic event failure")

    monkeypatch.setattr(owner, "emit_event", fail)
    with pytest.raises(RuntimeError):
        owner.submit(db_session, command=command(subject))
    for table in (FieldIdentityRevision, FieldIdentityReceipt, StoredFile):
        assert db_session.scalar(select(func.count()).select_from(table)) == 0


@pytest.mark.parametrize("kind", ["field_identity_portrait", "field_identity_evidence"])
def test_generic_download_never_streams_restricted_identity(
    db_session, actors, monkeypatch, kind
):
    from app.api.files import download_file

    subject, _, other = actors
    file = StoredFile(
        id=uuid4(),
        entity_type=kind,
        entity_id=str(subject),
        original_filename="synthetic.png",
        storage_key_or_relative_path="private/opaque.png",
        file_size=1,
        storage_provider="s3",
        owner_subscriber_id=None,
        is_deleted=False,
    )
    db_session.add(file)
    db_session.commit()
    monkeypatch.setattr(
        file_uploads, "resolve_user_owner_subscriber", lambda *args: None
    )
    monkeypatch.setattr(
        file_uploads,
        "stream_file",
        lambda *args: pytest.fail("Private identity streamed through generic endpoint"),
    )
    with pytest.raises(HTTPException) as exc:
        download_file(
            str(file.id), db=db_session, current_user={"subscriber_id": str(other)}
        )
    assert exc.value.status_code == 404


def test_upload_validation_runs_before_storage(monkeypatch):
    monkeypatch.setattr(
        file_uploads,
        "prepare_upload",
        lambda **kwargs: pytest.fail("Invalid private bytes uploaded"),
    )
    with pytest.raises(owner.FieldIdentityError):
        owner.prepare_identity_upload(
            subject_id=uuid4(),
            portrait=True,
            filename="image.png",
            content_type="image/png",
            data=b"not an image",
        )
    with pytest.raises(owner.FieldIdentityError):
        owner.prepare_identity_upload(
            subject_id=uuid4(),
            portrait=True,
            filename="evidence.pdf",
            content_type="application/pdf",
            data=b"%PDF-1.4",
        )


@pytest.mark.parametrize("authorization", ["", "Basic invalid", "Bearer "])
def test_cookie_mutation_requires_csrf(authorization):
    from starlette.requests import Request

    from app.api.field.identity import _browser_csrf

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "headers": [
                (b"authorization", authorization.encode()),
                (b"cookie", b"session_token=synthetic"),
            ],
        }
    )
    with pytest.raises(HTTPException) as exc:
        _browser_csrf(request)
    assert exc.value.status_code == 403


def test_cookie_csrf_and_mobile_bearer_admission():
    from starlette.requests import Request

    from app.api.field.identity import _browser_csrf

    for headers in [
        [(b"cookie", b"csrf_token=synthetic"), (b"x-csrf-token", b"synthetic")],
        [(b"authorization", b"Bearer synthetic")],
    ]:
        _browser_csrf(Request({"type": "http", "method": "POST", "headers": headers}))
