"""Context-builder tests for the admin projects web service
(``app/services/web_projects.py``, Phase 3 PR 10) — web_support_tickets test
style: exercise the builders against the native managers on db_session."""

import uuid

import pytest
from fastapi import HTTPException

from app.models.project import (
    ProjectStatus,
    ProjectTemplateTask,
    ProjectTemplateTaskDependency,
    ProjectType,
)
from app.schemas.project import ProjectCreate, ProjectTaskCreate
from app.services import web_projects
from app.services.project_filters import (
    serialize_project_filter_schema,
    serialize_project_task_filter_schema,
)
from app.services.projects import (
    FIBER_INSTALLATION_STAGE_ORDER,
    project_tasks,
    projects,
)


def _create_project(db_session, subscriber, **overrides):
    payload = ProjectCreate(
        name=overrides.pop("name", "Backbone build"),
        subscriber_id=subscriber.id,
        **overrides,
    )
    return projects.create(db_session, payload)


class TestListContext:
    def test_list_context_shape(self, db_session, subscriber):
        project = _create_project(db_session, subscriber, region="Abuja")
        context = web_projects.build_projects_list_context(
            db_session,
            search=None,
            status=None,
            project_type=None,
            priority=None,
            region=None,
            filters=None,
            order_by="created_at",
            order_dir="desc",
            page=1,
            per_page=25,
        )
        assert [str(row.id) for row in context["projects"]] == [str(project.id)]
        assert context["has_next_page"] is False
        assert context["page"] == 1
        assert {card["value"] for card in context["status_summary_cards"]} == {
            item.value for item in ProjectStatus
        }
        open_card = next(
            card for card in context["status_summary_cards"] if card["value"] == "open"
        )
        assert open_card["count"] == 1
        assert "Abuja" in context["region_options"]
        schema_fields = {item["field"] for item in context["project_filter_schema"]}
        assert "status" in schema_fields and "region" in schema_fields

    def test_list_context_filters_by_status(self, db_session, subscriber):
        _create_project(db_session, subscriber)
        context = web_projects.build_projects_list_context(
            db_session,
            search=None,
            status="completed",
            project_type=None,
            priority=None,
            region=None,
            filters=None,
            order_by="created_at",
            order_dir="desc",
            page=1,
            per_page=25,
        )
        assert context["projects"] == []

    def test_list_context_dynamic_filters(self, db_session, subscriber):
        _create_project(db_session, subscriber, region="Lagos")
        _create_project(db_session, subscriber, name="Other", region="Abuja")
        filters = '{"and": [["Project", "region", "=", "Lagos"]]}'
        context = web_projects.build_projects_list_context(
            db_session,
            search=None,
            status=None,
            project_type=None,
            priority=None,
            region=None,
            filters=filters,
            order_by="created_at",
            order_dir="desc",
            page=1,
            per_page=25,
        )
        assert [row.region for row in context["projects"]] == ["Lagos"]

    def test_invalid_filters_payload_is_http_400(self, db_session):
        with pytest.raises(HTTPException) as exc_info:
            web_projects.build_projects_list_context(
                db_session,
                search=None,
                status=None,
                project_type=None,
                priority=None,
                region=None,
                filters='{"and": [["Project", "not_a_field", "=", "x"]]}',
                order_by="created_at",
                order_dir="desc",
                page=1,
                per_page=25,
            )
        assert exc_info.value.status_code == 400

    def test_csv_export_includes_default_columns(self, db_session, subscriber):
        _create_project(db_session, subscriber, name="CSV project")
        content = web_projects.render_projects_csv(
            db_session,
            search=None,
            status=None,
            project_type=None,
            priority=None,
            region=None,
            filters=None,
            order_by="created_at",
            order_dir="desc",
            columns=None,
        )
        header, row = content.strip().splitlines()[:2]
        assert header.startswith("Project,Customer,Status,Priority,Created")
        assert "CSV project" in row


class TestReferenceResolution:
    def test_resolves_by_number_then_uuid(self, db_session, subscriber):
        project = _create_project(db_session, subscriber)
        if project.number:
            found, should_redirect = web_projects.resolve_project_reference(
                db_session, project.number
            )
            assert found.id == project.id and should_redirect is False
        found, should_redirect = web_projects.resolve_project_reference(
            db_session, str(project.id)
        )
        assert found.id == project.id
        # Deep links with the UUID must resolve; canonical redirect only when
        # the project has a human number.
        assert should_redirect is bool(project.number)

    def test_unknown_reference_is_404(self, db_session):
        with pytest.raises(HTTPException) as exc_info:
            web_projects.resolve_project_reference(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404


class TestFormHandlers:
    def test_create_requires_name(self, db_session):
        with pytest.raises(ValueError):
            web_projects.create_project_from_form(
                db_session, request=None, actor_id=None, name="   "
            )

    def test_create_and_quick_status(self, db_session, subscriber):
        project = web_projects.create_project_from_form(
            db_session,
            request=None,
            actor_id=str(uuid.uuid4()),
            name="Form project",
            subscriber_id=str(subscriber.id),
            status="open",
            priority="high",
            region="Abuja",
        )
        assert project.name == "Form project"
        assert project.priority == "high"

        updated = web_projects.quick_update_project(
            db_session,
            request=None,
            project_id=str(project.id),
            actor_id=None,
            field="status",
            value="active",
        )
        assert updated.status == "active"

        with pytest.raises(HTTPException) as exc_info:
            web_projects.quick_update_project(
                db_session,
                request=None,
                project_id=str(project.id),
                actor_id=None,
                field="name",
                value="nope",
            )
        assert exc_info.value.status_code == 400

    def test_comment_edit_requires_author(self, db_session, subscriber):
        project = _create_project(db_session, subscriber)
        author_id = str(uuid.uuid4())
        comment = web_projects.add_project_comment_from_form(
            db_session,
            request=None,
            project_id=str(project.id),
            actor_id=author_id,
            body="First note",
        )
        with pytest.raises(HTTPException) as exc_info:
            web_projects.update_project_comment_from_form(
                db_session,
                request=None,
                project_id=str(project.id),
                comment_id=str(comment.id),
                actor_id=str(uuid.uuid4()),
                body="Hijack",
            )
        assert exc_info.value.status_code == 403
        updated = web_projects.update_project_comment_from_form(
            db_session,
            request=None,
            project_id=str(project.id),
            comment_id=str(comment.id),
            actor_id=author_id,
            body="Edited note",
        )
        assert updated.body == "Edited note"


class TestDetailContext:
    def test_fiber_project_detail_has_stage_timeline(self, db_session, subscriber):
        project = _create_project(
            db_session,
            subscriber,
            name="Fiber install",
            project_type=ProjectType.fiber_optics_installation,
        )
        context = web_projects.build_project_detail_context(db_session, project=project)
        stages = context["fiber_stages"]
        assert [stage["key"] for stage in stages] == list(
            FIBER_INSTALLATION_STAGE_ORDER
        )
        assert all(stage["status"] == "pending" for stage in stages)
        assert len(context["tasks"]) == len(FIBER_INSTALLATION_STAGE_ORDER)
        assert context["all_statuses"] == [item.value for item in ProjectStatus]

    def test_plain_project_detail_has_no_stage_timeline(self, db_session, subscriber):
        project = _create_project(db_session, subscriber)
        context = web_projects.build_project_detail_context(db_session, project=project)
        assert context["fiber_stages"] == []
        assert context["comments"] == []


class TestTasksContext:
    def test_tasks_list_and_quick_status(self, db_session, subscriber):
        project = _create_project(db_session, subscriber)
        task = project_tasks.create(
            db_session,
            ProjectTaskCreate(project_id=project.id, title="Splice closure"),
        )
        context = web_projects.build_tasks_list_context(
            db_session,
            project_id=str(project.id),
            status=None,
            priority=None,
            assigned_to_me=False,
            actor_id=None,
            filters=None,
            page=1,
            per_page=25,
        )
        assert [str(row.id) for row in context["tasks"]] == [str(task.id)]
        assert str(project.id) in context["project_map"]
        schema_fields = {item["field"] for item in context["task_filter_schema"]}
        assert "assigned_to_person_id" in schema_fields

        updated = web_projects.quick_update_task_status(
            db_session,
            request=None,
            task_id=str(task.id),
            actor_id=None,
            status="done",
        )
        assert updated.status == "done"
        assert updated.completed_at is not None

    def test_assigned_to_me_requires_actor(self, db_session):
        with pytest.raises(HTTPException) as exc_info:
            web_projects.build_tasks_list_context(
                db_session,
                project_id=None,
                status=None,
                priority=None,
                assigned_to_me=True,
                actor_id=None,
                filters=None,
                page=1,
                per_page=25,
            )
        assert exc_info.value.status_code == 400

    def test_task_detail_lists_dependencies(self, db_session, subscriber):
        template = web_projects.create_template_from_form(
            db_session, name="Dep template"
        )
        web_projects.save_template_tasks_from_editor(
            db_session,
            template_id=str(template.id),
            tasks_json=(
                '[{"client_id": "a", "title": "Survey", "description": "",'
                ' "effort_hours": 2, "dependencies": []},'
                ' {"client_id": "b", "title": "Install", "description": "",'
                ' "effort_hours": null, "dependencies": ["a"]}]'
            ),
        )
        project = _create_project(
            db_session, subscriber, project_template_id=template.id
        )
        rows = context_tasks = web_projects.build_project_detail_context(
            db_session, project=project
        )["tasks"]
        assert len(context_tasks) == 2
        install = next(row for row in rows if row.title == "Install")
        detail = web_projects.build_task_detail_context(db_session, task=install)
        blocked_by = detail["dependencies"]["blocked_by"]
        assert [row["title"] for row in blocked_by] == ["Survey"]
        survey = next(row for row in rows if row.title == "Survey")
        survey_detail = web_projects.build_task_detail_context(db_session, task=survey)
        assert [row["title"] for row in survey_detail["dependencies"]["blocks"]] == [
            "Install"
        ]


class TestTemplateEditor:
    def test_editor_save_upserts_and_soft_deletes(self, db_session):
        template = web_projects.create_template_from_form(
            db_session, name="Editor template"
        )
        web_projects.save_template_tasks_from_editor(
            db_session,
            template_id=str(template.id),
            tasks_json=(
                '[{"client_id": "one", "title": "First", "description": "",'
                ' "effort_hours": "", "dependencies": []},'
                ' {"client_id": "two", "title": "Second", "description": "",'
                ' "effort_hours": 4, "dependencies": ["one"]}]'
            ),
        )
        tasks = (
            db_session.query(ProjectTemplateTask)
            .filter(ProjectTemplateTask.template_id == template.id)
            .filter(ProjectTemplateTask.is_active.is_(True))
            .order_by(ProjectTemplateTask.sort_order)
            .all()
        )
        assert [task.title for task in tasks] == ["First", "Second"]
        links = (
            db_session.query(ProjectTemplateTaskDependency)
            .filter(
                ProjectTemplateTaskDependency.template_task_id.in_(
                    [task.id for task in tasks]
                )
            )
            .all()
        )
        assert len(links) == 1
        assert links[0].depends_on_template_task_id == tasks[0].id

        payload = web_projects.build_template_tasks_editor_payload(
            db_session, str(template.id)
        )
        assert payload[1]["dependencies"] == [str(tasks[0].id)]

        # Resave keeping only the first task (by its real id) — the second is
        # soft-deleted and the dependency rebuilt away.
        web_projects.save_template_tasks_from_editor(
            db_session,
            template_id=str(template.id),
            tasks_json=(
                f'[{{"client_id": "{tasks[0].id}", "title": "First renamed",'
                ' "description": "", "effort_hours": 1, "dependencies": []}]'
            ),
        )
        active = (
            db_session.query(ProjectTemplateTask)
            .filter(ProjectTemplateTask.template_id == template.id)
            .filter(ProjectTemplateTask.is_active.is_(True))
            .all()
        )
        assert [task.title for task in active] == ["First renamed"]

    def test_editor_rejects_bad_payload(self, db_session):
        template = web_projects.create_template_from_form(db_session, name="Bad")
        with pytest.raises(ValueError):
            web_projects.save_template_tasks_from_editor(
                db_session,
                template_id=str(template.id),
                tasks_json='[{"client_id": "x", "title": ""}]',
            )
        with pytest.raises(ValueError):
            web_projects.save_template_tasks_from_editor(
                db_session,
                template_id=str(template.id),
                tasks_json="not-json",
            )

    def test_template_task_cross_template_guard(self, db_session):
        template_a = web_projects.create_template_from_form(db_session, name="A")
        template_b = web_projects.create_template_from_form(db_session, name="B")
        task = web_projects.create_template_task_from_form(
            db_session, template_id=str(template_a.id), title="Only in A"
        )
        with pytest.raises(HTTPException) as exc_info:
            web_projects.get_template_task_checked(
                db_session, template_id=str(template_b.id), task_id=str(task.id)
            )
        assert exc_info.value.status_code == 404


class TestFilterSchemas:
    def test_project_schema_covers_specs_and_options(self):
        schema = serialize_project_filter_schema(
            status_options=["open", "active"],
            priority_options=["normal"],
            project_type_options=["fiber_optics_installation"],
            staff_options=[{"id": "abc", "label": "Tech"}],
            template_options=[{"id": "tpl", "label": "Fiber"}],
        )
        by_field = {item["field"]: item for item in schema}
        assert {"value": "open", "label": "Open"} in by_field["status"]["options"]
        assert by_field["manager_person_id"]["options"] == [
            {"value": "abc", "label": "Tech"}
        ]
        assert by_field["project_template_id"]["options"] == [
            {"value": "tpl", "label": "Fiber"}
        ]
        assert all(item["operators"] for item in schema)

    def test_task_schema_covers_assignee_options(self):
        schema = serialize_project_task_filter_schema(
            status_options=["todo"],
            priority_options=["normal"],
            staff_options=[{"id": "abc", "label": "Tech"}],
            project_options=[{"id": "p1", "label": "Project one"}],
        )
        by_field = {item["field"]: item for item in schema}
        assert by_field["assigned_to_person_id"]["options"] == [
            {"value": "abc", "label": "Tech"}
        ]
        assert by_field["project_id"]["options"] == [
            {"value": "p1", "label": "Project one"}
        ]


class TestTemplatesListContext:
    def test_counts_active_template_tasks(self, db_session):
        template = web_projects.create_template_from_form(db_session, name="Counted")
        web_projects.create_template_task_from_form(
            db_session, template_id=str(template.id), title="T1"
        )
        context = web_projects.build_templates_list_context(db_session)
        assert str(template.id) in {str(t.id) for t in context["templates"]}
        assert context["template_task_counts"][str(template.id)] == 1

    def test_template_detail_labels_dependencies(self, db_session):
        template = web_projects.create_template_from_form(db_session, name="Detail")
        web_projects.save_template_tasks_from_editor(
            db_session,
            template_id=str(template.id),
            tasks_json=(
                '[{"client_id": "a", "title": "Alpha", "description": "",'
                ' "effort_hours": null, "dependencies": []},'
                ' {"client_id": "b", "title": "Beta", "description": "",'
                ' "effort_hours": null, "dependencies": ["a"]}]'
            ),
        )
        context = web_projects.build_template_detail_context(
            db_session, template_id=str(template.id)
        )
        beta = next(t for t in context["template_tasks"] if t.title == "Beta")
        assert context["dependency_labels"][str(beta.id)] == ["Alpha"]
