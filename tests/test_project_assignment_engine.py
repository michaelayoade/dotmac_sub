"""Project auto-assignment engine tests (Phase 3 §2.1 — CRM engine parity)."""

import uuid

from app.models.project import Project, ProjectTask
from app.models.service_team import ServiceTeam
from app.models.ticket_workflow import TicketAssignmentRule
from app.services.ticket_assignment import (
    auto_assign_project,
    find_authoritative_project_creation_rule,
)


def _project(db_session, subscriber, **overrides):
    project = Project(
        name=overrides.pop("name", "Fiber install"),
        project_type=overrides.pop("project_type", "fiber_optics_installation"),
        status="open",
        subscriber_id=subscriber.id,
        **overrides,
    )
    db_session.add(project)
    db_session.commit()
    return project


def _rule(db_session, *, name="Rule", match_config=None, team_id=None, priority=0):
    rule = TicketAssignmentRule(
        name=name,
        priority=priority,
        is_active=True,
        match_config=match_config or {},
        team_id=team_id,
    )
    db_session.add(rule)
    db_session.commit()
    return rule


def _team(db_session):
    team = ServiceTeam(name=f"Team {uuid.uuid4().hex[:6]}", team_type="field")
    db_session.add(team)
    db_session.commit()
    return team


class TestAuthoritativeCreationRule:
    def test_project_type_scoped_rule_matches(self, db_session, subscriber):
        team = _team(db_session)
        rule = _rule(
            db_session,
            name="Fiber installs",
            match_config={
                "entity_types": ["project"],
                "project_types": ["fiber_optics_installation"],
            },
            team_id=team.id,
        )
        project = _project(db_session, subscriber)
        found = find_authoritative_project_creation_rule(db_session, project)
        assert found is not None
        assert found.id == rule.id

    def test_project_type_mismatch_does_not_match(self, db_session, subscriber):
        _rule(
            db_session,
            match_config={
                "entity_types": ["project"],
                "project_types": ["cable_rerun"],
            },
        )
        project = _project(db_session, subscriber)
        assert find_authoritative_project_creation_rule(db_session, project) is None

    def test_ticket_scoped_rule_does_not_match_projects(self, db_session, subscriber):
        _rule(
            db_session,
            match_config={"entity_types": ["ticket"], "ticket_types": ["repair"]},
        )
        project = _project(db_session, subscriber)
        assert find_authoritative_project_creation_rule(db_session, project) is None

    def test_direct_assignee_rule_is_not_authoritative(self, db_session, subscriber):
        _rule(
            db_session,
            match_config={
                "entity_types": ["project"],
                "assignee_person_id": str(uuid.uuid4()),
            },
        )
        project = _project(db_session, subscriber)
        assert find_authoritative_project_creation_rule(db_session, project) is None


class TestAutoAssignProject:
    def test_authoritative_rule_assigns_team_and_clears_roles(
        self, db_session, subscriber
    ):
        team = _team(db_session)
        _rule(
            db_session,
            match_config={
                "entity_types": ["project"],
                "project_types": ["fiber_optics_installation"],
            },
            team_id=team.id,
        )
        stale_manager = uuid.uuid4()
        project = _project(
            db_session,
            subscriber,
            manager_person_id=stale_manager,
            project_manager_person_id=stale_manager,
        )

        results = auto_assign_project(db_session, str(project.id))
        db_session.commit()
        db_session.refresh(project)

        assert any(r.assigned for r in results)
        assert results[0].project_id == str(project.id)
        assert results[0].reason == "group_assigned"
        assert project.service_team_id == team.id
        assert project.manager_person_id is None
        assert project.project_manager_person_id is None

    def test_direct_technical_supervisor_assignment(self, db_session, subscriber):
        assignee = uuid.uuid4()
        _rule(
            db_session,
            match_config={
                "entity_types": ["project"],
                "assignee_person_id": str(assignee),
                "assignment_target": "technical_supervisor",
            },
        )
        project = _project(db_session, subscriber)

        results = auto_assign_project(db_session, str(project.id))
        db_session.commit()
        db_session.refresh(project)

        assert results[0].assigned is True
        assert project.manager_person_id == assignee
        assert project.project_manager_person_id == assignee

    def test_direct_technician_assignment_fans_out_to_tasks(
        self, db_session, subscriber
    ):
        assignee = uuid.uuid4()
        _rule(
            db_session,
            match_config={
                "entity_types": ["project"],
                "assignee_person_id": str(assignee),
                "assignment_target": "technician",
            },
        )
        project = _project(db_session, subscriber)
        task = ProjectTask(project_id=project.id, title="Survey", status="todo")
        db_session.add(task)
        db_session.commit()

        results = auto_assign_project(db_session, str(project.id))
        db_session.commit()
        db_session.refresh(task)

        assert results[0].assigned is True
        assert task.assigned_to_person_id == assignee
        assert {a.person_id for a in task.assignees} == {assignee}

    def test_no_matching_rule(self, db_session, subscriber):
        project = _project(db_session, subscriber)
        results = auto_assign_project(db_session, str(project.id))
        assert len(results) == 1
        assert results[0].assigned is False
        assert results[0].reason == "no_matching_rule"

    def test_missing_project(self, db_session):
        results = auto_assign_project(db_session, str(uuid.uuid4()))
        assert results[0].reason == "project_not_found_or_inactive"
