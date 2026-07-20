"""Portal payload shape tests — the §2.5 read-surface compatibility contract.

The native ``build_portal_project_payload`` must serve byte-for-byte the shape
the ``project_mirror`` cached and mobile parses (``ProjectItem`` in
``app/schemas/portal.py`` — untouched by Phase 3). ``id`` is the project UUID
(the value the mirror exposed as ``crm_project_id``), ``progress_pct`` is an
int and stage ``status ∈ pending|in_progress|done``.
"""

import uuid
from datetime import UTC, datetime

from app.models.project import Project, ProjectTask
from app.schemas.portal import MyProjectsResponse, ProjectItem
from app.services.projects import (
    FIBER_INSTALLATION_STAGE_ORDER,
    build_portal_project_payload,
    projects,
)

# The exact item keys the mirror read served (projects_mirror.read_for_subscriber).
MIRROR_ITEM_KEYS = [
    "id",
    "name",
    "status",
    "project_type",
    "progress_pct",
    "current_stage",
    "stages",
    "customer_address",
    "region",
    "start_at",
    "due_at",
    "completed_at",
    "created_at",
]

STAGE_KEYS = ["key", "title", "status", "completed_at"]


def _fiber_project(*, plan_done=False):
    """Transient fiber project (never persisted) so timezone-aware datetimes
    survive intact — the payload builder is a pure function of the objects."""
    project = Project(
        id=uuid.UUID("6a3ffe36-1f6c-4f5a-9e17-46e29d5c3f10"),
        name="Fiber install — Wuse II",
        project_type="fiber_optics_installation",
        status="active",
        customer_address="12 Aminu Kano Crescent",
        region="Abuja",
        start_at=datetime(2026, 7, 1, 8, 0, tzinfo=UTC),
        due_at=datetime(2026, 7, 15, 8, 0, tzinfo=UTC),
        created_at=datetime(2026, 6, 30, 9, 0, tzinfo=UTC),
    )
    project.tasks = [
        ProjectTask(
            project_id=project.id,
            title=stage_key.replace("_", " ").title(),
            status="done" if plan_done and stage_key == "project_plan" else "todo",
            completed_at=datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
            if plan_done and stage_key == "project_plan"
            else None,
            created_at=datetime(2026, 6, 30, 9, index, tzinfo=UTC),
            metadata_={"fiber_stage_key": stage_key},
            is_active=True,
        )
        for index, stage_key in enumerate(FIBER_INSTALLATION_STAGE_ORDER)
    ]
    return project


class TestPortalPayloadShape:
    def test_item_keys_match_mirror_read_exactly(self):
        payload = build_portal_project_payload(_fiber_project())
        assert list(payload.keys()) == MIRROR_ITEM_KEYS
        for stage in payload["stages"]:
            assert list(stage.keys()) == STAGE_KEYS

    def test_payload_parses_with_untouched_portal_schema(self):
        """`app/schemas/portal.py` doubles as the mobile contract (§2.5/§2.4):
        the native payload must deserialize with it unchanged."""
        project = _fiber_project(plan_done=True)
        item = ProjectItem(**build_portal_project_payload(project))
        assert item.id == str(project.id)
        assert isinstance(item.progress_pct, int)
        assert all(s.status in {"pending", "in_progress", "done"} for s in item.stages)

    def test_golden_fixture_fiber_project(self):
        """Representative golden payload (the shape the mirror served)."""
        project = _fiber_project(plan_done=True)
        payload = build_portal_project_payload(project)
        assert payload == {
            "id": "6a3ffe36-1f6c-4f5a-9e17-46e29d5c3f10",
            "name": "Fiber install — Wuse II",
            "status": "active",
            "project_type": "fiber_optics_installation",
            "progress_pct": 17,
            "current_stage": "Project Survey",
            "stages": [
                {
                    "key": "project_plan",
                    "title": "Project Plan",
                    "status": "done",
                    "completed_at": "2026-07-01T12:00:00+00:00",
                },
                {
                    "key": "project_survey",
                    "title": "Project Survey",
                    "status": "pending",
                    "completed_at": None,
                },
                {
                    "key": "drop_cable_installation",
                    "title": "Drop Cable Installation",
                    "status": "pending",
                    "completed_at": None,
                },
                {
                    "key": "survey_approval_po_issuance",
                    "title": "Survey Approval & PO Issuance",
                    "status": "pending",
                    "completed_at": None,
                },
                {
                    "key": "last_mile_installation",
                    "title": "Last Mile Installation",
                    "status": "pending",
                    "completed_at": None,
                },
                {
                    "key": "power_splicing_activation",
                    "title": ("Power Direction, Splicing & Customer Activation"),
                    "status": "pending",
                    "completed_at": None,
                },
            ],
            "customer_address": "12 Aminu Kano Crescent",
            "region": "Abuja",
            "start_at": "2026-07-01T08:00:00+00:00",
            "due_at": "2026-07-15T08:00:00+00:00",
            "completed_at": None,
            "created_at": "2026-06-30T09:00:00+00:00",
        }

    def test_completed_project_is_100pct_with_no_current_stage(self):
        project = _fiber_project()
        project.status = "completed"
        project.completed_at = datetime(2026, 7, 10, 10, 0, tzinfo=UTC)
        payload = build_portal_project_payload(project)
        assert payload["progress_pct"] == 100
        assert payload["current_stage"] is None
        assert payload["completed_at"] == "2026-07-10T10:00:00+00:00"

    def test_stage_status_vocabulary(self):
        project = _fiber_project()
        tasks = {t.metadata_["fiber_stage_key"]: t for t in project.tasks}
        tasks["project_plan"].status = "done"
        tasks["project_survey"].status = "in_progress"
        tasks["drop_cable_installation"].status = "blocked"
        tasks["survey_approval_po_issuance"].status = "backlog"

        statuses = {
            s["key"]: s["status"]
            for s in build_portal_project_payload(project)["stages"]
        }
        assert statuses["project_plan"] == "done"
        assert statuses["project_survey"] == "in_progress"
        assert statuses["drop_cable_installation"] == "in_progress"
        assert statuses["survey_approval_po_issuance"] == "pending"
        assert statuses["last_mile_installation"] == "pending"

    def test_non_fiber_project_uses_generic_task_timeline(self):
        project = Project(
            id=uuid.uuid4(),
            name="Cross connect",
            project_type="cross_connect",
            status="open",
            created_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
        project.tasks = [
            ProjectTask(
                project_id=project.id,
                title="Patch fibre",
                status="done",
                is_active=True,
            )
        ]

        payload = build_portal_project_payload(project)
        assert payload["stages"][0]["key"] is None
        assert payload["stages"][0]["title"] == "Patch fibre"
        assert payload["progress_pct"] == 100


def _persisted_project(db_session, subscriber_id, **overrides):
    project = Project(
        name=overrides.pop("name", "Fiber install"),
        project_type="fiber_optics_installation",
        status="open",
        subscriber_id=subscriber_id,
        **overrides,
    )
    db_session.add(project)
    db_session.commit()
    return project


class TestPortalList:
    def test_scoped_to_subscribers_and_parses_as_response(self, db_session, subscriber):
        from app.models.subscriber import Subscriber

        other = Subscriber(
            first_name="Other",
            last_name="Customer",
            email=f"other-{uuid.uuid4().hex}@example.com",
        )
        db_session.add(other)
        db_session.commit()

        mine = _persisted_project(db_session, subscriber.id)
        _persisted_project(db_session, other.id, name="Other install")

        items = projects.portal_list(db_session, str(subscriber.id))
        assert [item["id"] for item in items] == [str(mine.id)]

        # The `{projects, total, active}` shell (PR8 repoints /me/projects
        # onto this) still validates against the untouched portal schema.
        active = sum(1 for i in items if i["status"] not in ("completed", "canceled"))
        response = MyProjectsResponse(projects=items, total=len(items), active=active)
        assert response.total == 1
        assert response.active == 1

    def test_inactive_projects_excluded(self, db_session, subscriber):
        project = _persisted_project(db_session, subscriber.id)
        project.is_active = False
        db_session.commit()
        assert projects.portal_list(db_session, str(subscriber.id)) == []
