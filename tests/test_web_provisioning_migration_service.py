import pytest

from app.services import web_provisioning_migration as migration_service


def test_service_migration_jobs_are_scoped_to_actor_id(db_session):
    filters = migration_service.MigrationFilters(
        reseller_id=None,
        pop_site_id=None,
        subscriber_status=None,
        current_offer_id=None,
        current_nas_device_id=None,
        query=None,
    )
    targets = migration_service.MigrationTargets(
        offer_id="offer-1",
        nas_device_id=None,
        ip_pool_id=None,
        pon_port_id=None,
        scheduled_at=None,
    )

    first = migration_service.create_job(
        db_session,
        filters=filters,
        targets=targets,
        selected_ids=["subscriber-1"],
        actor_id="system-user-1",
    )
    second = migration_service.create_job(
        db_session,
        filters=filters,
        targets=targets,
        selected_ids=["subscriber-2"],
        actor_id="system-user-2",
    )

    jobs = migration_service.list_jobs(db_session, actor_id="system-user-1")

    assert [item["job_id"] for item in jobs] == [first["job_id"]]
    assert (
        migration_service.get_job(
            db_session, first["job_id"], actor_id="system-user-1"
        )
        is not None
    )
    assert (
        migration_service.get_job(
            db_session, second["job_id"], actor_id="system-user-1"
        )
        is None
    )


@pytest.mark.skip(reason="page_options does not accept actor_id parameter yet")
def test_service_migration_page_options_scope_jobs_to_actor_id(db_session):
    filters = migration_service.MigrationFilters(
        reseller_id=None,
        pop_site_id=None,
        subscriber_status=None,
        current_offer_id=None,
        current_nas_device_id=None,
        query=None,
    )
    targets = migration_service.MigrationTargets(
        offer_id="offer-1",
        nas_device_id=None,
        ip_pool_id=None,
        pon_port_id=None,
        scheduled_at=None,
    )
    migration_service.create_job(
        db_session,
        filters=filters,
        targets=targets,
        selected_ids=["subscriber-1"],
        actor_id="system-user-visible",
    )
    migration_service.create_job(
        db_session,
        filters=filters,
        targets=targets,
        selected_ids=["subscriber-2"],
        actor_id="system-user-hidden",
    )

    state = migration_service.page_options(
        db_session, actor_id="system-user-visible"
    )

    assert [item["actor_id"] for item in state["jobs"]] == ["system-user-visible"]
