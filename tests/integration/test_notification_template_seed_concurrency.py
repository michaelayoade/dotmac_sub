"""PostgreSQL concurrency proof for application-startup template seeding."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models.notification import NotificationChannel, NotificationTemplate
from app.services.settings_seed import seed_notification_templates


def test_concurrent_startup_seeders_create_each_default_once(
    cloned_database,
) -> None:
    database_url = cloned_database("heads")
    engine = create_engine(database_url)
    session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )
    ready = Barrier(2, timeout=10)

    def seed() -> None:
        with session_factory() as session:
            ready.wait()
            seed_notification_templates(session)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(seed) for _index in range(2)]
            for future in futures:
                future.result(timeout=30)

        with Session(engine) as check:
            count = check.scalar(
                select(func.count(NotificationTemplate.id)).where(
                    NotificationTemplate.code == "subscription_paused",
                    NotificationTemplate.channel == NotificationChannel.email,
                )
            )
            assert count == 1
    finally:
        engine.dispose()
