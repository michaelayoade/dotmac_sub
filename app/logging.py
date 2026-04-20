import json
import logging
import logging.config
from datetime import UTC, datetime

_BASE_LOG_RECORD_FIELDS = set(logging.makeLogRecord({}).__dict__.keys())


def _json_safe(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in (
            "request_id",
            "actor_id",
            "path",
            "method",
            "status",
            "duration_ms",
        ):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        for key, value in record.__dict__.items():
            if key in payload or key in _BASE_LOG_RECORD_FIELDS or key.startswith("_"):
                continue
            payload[key] = _json_safe(value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging() -> None:
    logging_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "json": {
                "()": JsonLogFormatter,
            }
        },
        "handlers": {
            "default": {
                "class": "logging.StreamHandler",
                "formatter": "json",
            }
        },
        "root": {"handlers": ["default"], "level": "INFO"},
        "loggers": {
            # Paramiko logs expected network/banner failures from remote devices
            # as ERROR tracebacks before application code can handle them. Keep
            # those details in our OLT SSH result messages instead of flooding
            # app logs with transport internals.
            "paramiko.transport": {"level": "CRITICAL", "propagate": False},
        },
    }
    logging.config.dictConfig(logging_config)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
