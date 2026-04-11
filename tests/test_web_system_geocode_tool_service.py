from app.services import web_system_geocode_tool as geocode_tool_service


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
