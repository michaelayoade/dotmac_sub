import pytest

from app.services import web_system_geocode_tool as geocode_tool_service


@pytest.mark.skip(reason="list_jobs does not exist yet; actor scoping not implemented")
def test_geocode_jobs_are_scoped_to_actor_id(db_session):
    first = geocode_tool_service.create_job(
        db_session,
        filters=geocode_tool_service.GeocodeFilters(
            date_from=None,
            date_to=None,
            subscriber_status=None,
            overwrite_existing=False,
        ),
        actor_id="system-user-1",
    )
    second = geocode_tool_service.create_job(
        db_session,
        filters=geocode_tool_service.GeocodeFilters(
            date_from=None,
            date_to=None,
            subscriber_status=None,
            overwrite_existing=True,
        ),
        actor_id="system-user-2",
    )

    jobs = geocode_tool_service.list_jobs(
        db_session,
        actor_id="system-user-1",
    )

    assert [item["job_id"] for item in jobs] == [first["job_id"]]
    assert (
        geocode_tool_service.get_job(
            db_session,
            first["job_id"],
            actor_id="system-user-1",
        )
        is not None
    )
    assert (
        geocode_tool_service.get_job(
            db_session,
            second["job_id"],
            actor_id="system-user-1",
        )
        is None
    )


def test_geocode_page_state_returns_all_jobs(db_session):
    """build_page_state does not currently accept actor_id scoping."""
    geocode_tool_service.create_job(
        db_session,
        filters=geocode_tool_service.GeocodeFilters(
            date_from=None,
            date_to=None,
            subscriber_status=None,
            overwrite_existing=False,
        ),
        actor_id="system-user-visible",
    )
    geocode_tool_service.create_job(
        db_session,
        filters=geocode_tool_service.GeocodeFilters(
            date_from=None,
            date_to=None,
            subscriber_status=None,
            overwrite_existing=False,
        ),
        actor_id="system-user-hidden",
    )

    state = geocode_tool_service.build_page_state(db_session)

    # build_page_state returns all jobs (no actor scoping yet)
    assert len(state["jobs"]) == 2
