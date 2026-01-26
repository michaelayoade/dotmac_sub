from celery import Celery

from app.services.scheduler_config import build_beat_schedule, get_celery_config

celery_app = Celery("dotmac_sm")
celery_app.conf.update(get_celery_config())
celery_app.conf.beat_schedule = build_beat_schedule()
celery_app.conf.beat_scheduler = "app.celery_scheduler.DbScheduler"
celery_app.autodiscover_tasks(["app.tasks"])
