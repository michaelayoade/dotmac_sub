from app.services import infrastructure_health


def test_expected_celery_queues_uses_default_when_unset(monkeypatch):
    monkeypatch.delenv("CELERY_EXPECTED_QUEUES", raising=False)

    queues = infrastructure_health._expected_celery_queues()

    assert queues == [
        "celery",
        "nin",
        "tr069",
        "acs",
        "bandwidth",
        "ingestion",
        "crm",
        "billing",
    ]


def test_expected_celery_queues_parses_env(monkeypatch):
    monkeypatch.setenv("CELERY_EXPECTED_QUEUES", "celery, billing, tr069")

    queues = infrastructure_health._expected_celery_queues()

    assert queues == ["celery", "billing", "tr069"]


def test_celery_queue_restart_targets_default_mapping(monkeypatch):
    monkeypatch.delenv("CELERY_QUEUE_RESTART_TARGETS", raising=False)

    targets = infrastructure_health._celery_queue_restart_targets()

    assert targets["billing"] == "celery-worker-billing"
    assert targets["tr069"] == "celery-worker-tr069"
    assert targets["ingestion"] == "celery-worker-ingestion"


def test_celery_queue_restart_targets_parses_env(monkeypatch):
    monkeypatch.setenv(
        "CELERY_QUEUE_RESTART_TARGETS",
        "celery=worker-default,billing=worker-billing",
    )

    targets = infrastructure_health._celery_queue_restart_targets()

    assert targets == {
        "celery": "worker-default",
        "billing": "worker-billing",
    }


def test_celery_unavailable_details_marks_expected_queues_missing(monkeypatch):
    monkeypatch.setenv("CELERY_EXPECTED_QUEUES", "celery,billing")
    monkeypatch.setenv(
        "CELERY_QUEUE_RESTART_TARGETS",
        "celery=worker-default,billing=worker-billing",
    )

    details = infrastructure_health._celery_unavailable_details("No workers")

    assert details["error"] == "No workers"
    assert details["expected_queues"] == ["celery", "billing"]
    assert details["missing_queues"] == ["celery", "billing"]
    assert details["queue_restart_targets"] == {
        "celery": "worker-default",
        "billing": "worker-billing",
    }
