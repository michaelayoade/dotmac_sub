import time

from celery.beat import Scheduler

from app.services.scheduler_config import build_beat_schedule


class DbScheduler(Scheduler):
    def __init__(self, *args, **kwargs):
        self._last_refresh_at = 0.0
        super().__init__(*args, **kwargs)

    def setup_schedule(self):
        self._refresh_schedule()

    def tick(self):
        self._refresh_schedule()
        return super().tick()

    def _refresh_schedule(self):
        refresh_seconds = int(self.app.conf.get("beat_refresh_seconds", 30))
        now = time.monotonic()
        if now - self._last_refresh_at < max(refresh_seconds, 1):
            return
        schedule = build_beat_schedule()
        if schedule:
            missing = set(self.schedule.keys()) - set(schedule.keys())
            for key in missing:
                self.schedule.pop(key, None)
            self.merge_inplace(schedule)
        self._last_refresh_at = now
