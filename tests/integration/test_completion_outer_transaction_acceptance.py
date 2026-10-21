"""Real migration677/681 parent creation across PostgreSQL owner savepoints."""

from collections.abc import Callable
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.models.field_completion_review import (
    CompletionCriteriaRevision,
    CompletionReviewDecision,
    CompletionSubmission,
)
from app.services.field import completion_review as owner
from app.services.field.completion_review_contracts import (
    WRITE,
    CompletionReviewOutcome,
    IssuedCompletionCriterion,
    PublishCompletionCriteria,
    ReviewState,
)
from scripts.migrations.outer_transaction_provenance import CreationParentTable
from tests.integration.test_field_completion_review_acceptance import (
    context,
    prepare,
    review,
)
from tests.integration.test_project_engineering_plan_acceptance import NativeScope
from tests.integration.test_project_engineering_plan_acceptance import (
    native_engine as engineering_native_engine,
)
from tests.integration.test_project_engineering_plan_acceptance import (
    scope as engineering_scope,
)

native_engine = engineering_native_engine
scope = engineering_scope


def external_command(
    engine: Engine,
    *,
    callback: Callable[[Session], CompletionReviewOutcome],
    parent: CreationParentTable,
    immediate: bool,
) -> CompletionReviewOutcome:
    """Physical outer commit after the executor releases its actual savepoints."""
    with engine.connect() as connection:
        outer = connection.begin()
        with Session(connection, join_transaction_mode="create_savepoint") as db:
            result = callback(db)
        parent_id = {
            CreationParentTable.criteria_revision: result.criteria_revision_id,
            CreationParentTable.submission: result.submission_id,
            CreationParentTable.review_decision: result.decision_id,
        }[parent]
        assert parent_id is not None
        assert (
            connection.scalar(
                text(
                    f'SELECT creation_outer_xid=pg_current_xact_id() FROM "{parent.value}" WHERE id=:id'  # noqa: S608 - fixed migration-owned enum only
                ),
                {"id": parent_id},
            )
            is True
        )
        if immediate:
            connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        outer.commit()
    return result


@pytest.mark.parametrize("immediate", [False, True])
def test_enroll_publish_submit_review_commit_through_external_savepoints(
    native_engine: Engine, scope: NativeScope, monkeypatch, immediate
):
    original_enroll = owner.enroll

    def enroll_in_external_connection(_db, *, command):
        return external_command(
            native_engine,
            callback=lambda db: original_enroll(db, command=command),
            parent=CreationParentTable.criteria_revision,
            immediate=immediate,
        )

    monkeypatch.setattr(owner, "enroll", enroll_in_external_connection)
    prepared = prepare(native_engine, scope, native=True)
    published = external_command(
        native_engine,
        callback=lambda db: owner.publish_criteria(
            db,
            command=PublishCompletionCriteria(
                context=context(prepared.reviewer, WRITE),
                access=prepared.access,
                enrollment_id=prepared.submission.enrollment_id,
                expected_generation=1,
                criteria=tuple(
                    IssuedCompletionCriterion(
                        id=observation.criterion_id,
                        order=position,
                        statement="Current native installation criterion",
                        verification_method="Inspect exact installed equipment",
                    )
                    for position, observation in enumerate(
                        prepared.submission.observations
                    )
                ),
            ),
        ),
        parent=CreationParentTable.criteria_revision,
        immediate=immediate,
    )
    assert published.criteria_revision_id is not None
    prepared = replace(
        prepared,
        submission=replace(
            prepared.submission,
            criteria_revision_id=published.criteria_revision_id,
        ),
    )
    submitted = external_command(
        native_engine,
        callback=lambda db: owner.submit_evidence(db, command=prepared.submission),
        parent=CreationParentTable.submission,
        immediate=immediate,
    )
    decided = external_command(
        native_engine,
        callback=lambda db: owner.decide(db, command=review(prepared, submitted)),
        parent=CreationParentTable.review_decision,
        immediate=immediate,
    )
    assert decided.state == ReviewState.accepted
    with Session(native_engine) as db:
        assert db.get(CompletionReviewDecision, decided.decision_id).state == "accepted"
        assert (
            db.get(CompletionSubmission, submitted.submission_id).creation_outer_xid
            is not None
        )
        assert (
            db.get(
                CompletionCriteriaRevision, published.criteria_revision_id
            ).creation_outer_xid
            is not None
        )


@pytest.mark.parametrize("child", ["criterion", "observation", "result"])
def test_committed_completion_parent_refuses_late_children(
    native_engine: Engine, scope: NativeScope, child
):
    prepared = prepare(native_engine, scope, native=True)
    with Session(native_engine) as db:
        submitted = owner.submit_evidence(db, command=prepared.submission)
    with Session(native_engine) as db:
        decided = owner.decide(db, command=review(prepared, submitted))
    criterion = prepared.submission.observations[0].criterion_id
    with native_engine.begin() as connection:
        with pytest.raises(
            DBAPIError,
            match="only be created with revision|atomic submission creation|atomic decision creation",
        ):
            if child == "criterion":
                connection.execute(
                    text(
                        "INSERT INTO completion_criteria(revision_id,id,\"order\",statement,verification_method) VALUES (:revision,:id,99,'Late criterion','Inspect')"
                    ),
                    {
                        "revision": prepared.submission.criteria_revision_id,
                        "id": uuid4(),
                    },
                )
            elif child == "observation":
                connection.execute(
                    text(
                        "INSERT INTO completion_observations(submission_id,criterion_id,criteria_revision_id,observation) VALUES (:submission,:criterion,:revision,'Late observation')"
                    ),
                    {
                        "submission": submitted.submission_id,
                        "criterion": criterion,
                        "revision": prepared.submission.criteria_revision_id,
                    },
                )
            else:
                connection.execute(
                    text(
                        "INSERT INTO completion_criterion_results(decision_id,criterion_id,submission_id,verdict,reason) VALUES (:decision,:criterion,:submission,'passed','Late result')"
                    ),
                    {
                        "decision": decided.decision_id,
                        "criterion": criterion,
                        "submission": submitted.submission_id,
                    },
                )
        connection.rollback()


@pytest.mark.parametrize(
    "model",
    [CompletionCriteriaRevision, CompletionSubmission, CompletionReviewDecision],
)
@pytest.mark.parametrize("supplied", [None, "1", "18446744073709551615"])
def test_completion_parent_marker_is_server_stamped_and_immutable(
    native_engine: Engine, scope: NativeScope, model, supplied
):
    prepared = prepare(native_engine, scope, native=True)
    with Session(native_engine) as db:
        submitted = owner.submit_evidence(db, command=prepared.submission)
    with Session(native_engine) as db:
        decided = owner.decide(db, command=review(prepared, submitted))
    identifier = {
        CompletionCriteriaRevision: prepared.submission.criteria_revision_id,
        CompletionSubmission: submitted.submission_id,
        CompletionReviewDecision: decided.decision_id,
    }[model]
    with native_engine.connect() as connection:
        outer = connection.begin()
        original = (
            connection.execute(select(model.__table__).where(model.id == identifier))
            .mappings()
            .one()
        )
        values = dict(original)
        values.update(id=uuid4(), creation_outer_xid=supplied)
        if model is CompletionCriteriaRevision:
            values["revision"] += 1
        connection.execute(model.__table__.insert().values(**values))
        assert connection.scalar(
            select(model.creation_outer_xid).where(model.id == values["id"])
        ) == connection.scalar(text("SELECT pg_current_xact_id()::text"))
        outer.rollback()  # Marker canary deliberately creates no child set.
    with native_engine.begin() as connection:
        with pytest.raises(DBAPIError, match="immutable"):
            connection.execute(
                model.__table__.update()
                .where(model.id == identifier)
                .values(creation_outer_xid=text("pg_current_xact_id()"))
            )
        connection.rollback()
