import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.router_management import Router, RouterStatus

logger = logging.getLogger(__name__)


class RouterMonitoringService:
    @staticmethod
    def get_dashboard_summary(db: Session) -> dict:
        counts = {}
        for status in RouterStatus:
            count = db.execute(
                select(func.count(Router.id)).where(
                    Router.is_active.is_(True),
                    Router.status == status,
                )
            ).scalar_one()
            counts[status.value] = count

        total = sum(counts.values())
        return {"total": total, **counts}

    @staticmethod
    def parse_health_response(data: dict) -> dict:
        return {
            "cpu_load": int(data.get("cpu-load", 0)),
            "free_memory": int(data.get("free-memory", 0)),
            "total_memory": int(data.get("total-memory", 0)),
            "uptime": data.get("uptime", "unknown"),
            "free_hdd_space": int(data.get("free-hdd-space", 0)),
            "total_hdd_space": int(data.get("total-hdd-space", 0)),
            "architecture_name": data.get("architecture-name", "unknown"),
            "board_name": data.get("board-name", "unknown"),
            "version": data.get("version", "unknown"),
        }
