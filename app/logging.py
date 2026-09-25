import json
import logging
import logging.config
import re
import sys
from datetime import UTC, datetime
from typing import Any

_BASE_LOG_RECORD_FIELDS = set(logging.makeLogRecord({}).__dict__.keys())
_SENSITIVE_QUERY_VALUE = re.compile(
    r"([?&](?:token|access_token|refresh_token|visitor_token)=)[^&\s\"']+",
    re.IGNORECASE,
)
_ROUTEROS_CREDENTIAL_VALUE = re.compile(
    r"=(password|secret)=.*?(?="
    r" \.tag="  # next RouterOS API word: the trailing .tag=<n>
    r"| =[A-Za-z0-9_-]+="  # next RouterOS API word: =<name>=<value>
    r"|['\"](?:[,)\]]|$)"  # closing bytes-repr/tuple-repr quote, then , ) ] or end
    r"|$"  # end of string — never leave an unterminated tail unredacted
    r")",
    re.DOTALL,
)
_TRACEBACK_FORMATTER = logging.Formatter()


def redact_sensitive_query_values(value: str) -> str:
    """Redact URL query credentials before a record reaches any formatter."""

    return _SENSITIVE_QUERY_VALUE.sub(r"\1<redacted>", value)


def redact_routeros_credentials(text: str) -> str:
    """Redact RouterOS API word-syntax credentials from exception/log text.

    ``routeros_api`` embeds the raw ``/login`` API word it sent — including
    the cleartext ``=password=...``/``=secret=...`` value — in the exception
    text it raises on failure, both as a plain string and inside a Python
    ``bytes`` repr (e.g. ``b'/login =name=x =password=secret .tag=1'``). A
    password can itself contain a space or a quote character, so this must
    NOT stop at the first bare space or embedded quote (doing so leaves the
    rest of the password readable after the match) — it stops only at a
    recognized RouterOS word boundary (`` .tag=`` or `` =<word>=``), a
    closing repr quote that is actually followed by `` , `` / `` ) `` /
    `` ] `` / end of string, or end of string itself. Falling through to
    end-of-string when no boundary is recognized means an unfamiliar tail
    shape is over-redacted rather than leaked.

    This is a boundary heuristic over the observed ``routeros_api`` text
    shapes, not a structural parse of the RouterOS API word protocol: a
    password that itself contains the literal substring `` .tag=`` or
    `` =<word>=`` can still make the match end inside the password, leaving
    the remainder unredacted. On a chained/grouped traceback the end-of-string
    fallback above also applies per matched occurrence, so an unrelated tail
    that happens to follow a `=password=`/`=secret=` match elsewhere in the
    same text can be swept into that match's redaction — an intentional
    over-redaction, not a bug, because failing safe (redact too much) is the
    correct failure mode for a credential leak, not failing open.
    """

    return _ROUTEROS_CREDENTIAL_VALUE.sub(r"=\1=<redacted>", text)


def sanitize_exception(exc: BaseException) -> str:
    """Return ``str(exc)`` with any RouterOS API credential redacted.

    Falls back to the exception's type name when the sanitized message is
    empty, so a caller always gets a non-empty, log-safe string.
    """

    message = redact_routeros_credentials(str(exc))
    return message or type(exc).__name__


def _redact_log_value(value: Any) -> Any:
    if isinstance(value, BaseException):
        return sanitize_exception(value)
    if isinstance(value, str):
        return redact_routeros_credentials(redact_sensitive_query_values(value))
    if isinstance(value, tuple):
        return tuple(_redact_log_value(item) for item in value)
    if isinstance(value, dict):
        return {key: _redact_log_value(item) for key, item in value.items()}
    return value


class SensitiveQueryFilter(logging.Filter):
    """Protect logs that contain query-string or RouterOS API credentials.

    Covers ``record.msg``/``record.args`` (including an exception object
    passed as a ``%s`` argument), a rendered ``exc_info`` traceback (which
    otherwise bypass the msg/args redaction above), and any non-standard
    ``extra=`` attribute a caller attached to the record (e.g. Sentry's
    logging integration copies these into the event's ``extra``; Celery's
    ``extra={"error": str(exception)}`` is exactly this shape).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = _redact_log_value(record.msg)
            record.args = _redact_log_value(record.args)
            if record.exc_info:
                text = getattr(record, "exc_text", None)
                if not text:
                    text = _TRACEBACK_FORMATTER.formatException(record.exc_info)
                record.exc_text = redact_routeros_credentials(text)
            for key, value in list(record.__dict__.items()):
                if key in _BASE_LOG_RECORD_FIELDS or key.startswith("_"):
                    continue
                setattr(record, key, _redact_log_value(value))
        except Exception:
            # A broken __str__/__repr__ anywhere above must never propagate
            # out of a logging call into business code, and must never let
            # unredacted text through by falling back to the original
            # record — replace the record's content instead of re-raising.
            record.msg = "<log record redaction failed>"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
        return True


class StderrStreamHandler(logging.StreamHandler):
    """A StreamHandler that resolves ``sys.stderr`` lazily on every emit.

    The default ``logging.StreamHandler`` captures whatever ``sys.stderr``
    points at when the handler is *constructed* and holds that reference for
    life. ``configure_logging()`` runs at import time of ``app.main``; under
    pytest the first test that imports the app binds the root handler to that
    test's captured stderr. When pytest later tears that capture down (closing
    the stream), the still-bound handler raises
    ``ValueError: I/O operation on closed file`` for every subsequent test that
    logs through the root logger — a broad, ordering-dependent test-isolation
    cascade ("--- Logging error ---" in CI). Resolving ``sys.stderr`` on each
    access keeps the handler bound to the live stream, fixing the leak at its
    source (and making the handler robust to any stderr swap at runtime).
    """

    def __init__(self) -> None:
        super().__init__(stream=sys.stderr)

    @property
    def stream(self):
        return sys.stderr

    @stream.setter
    def stream(self, value) -> None:
        # logging.StreamHandler.__init__ assigns to self.stream; ignore the
        # snapshot it tries to store so stream resolution stays dynamic.
        pass


def _json_safe(value):
    if isinstance(value, BaseException):
        return sanitize_exception(value)
    if isinstance(value, str):
        return redact_routeros_credentials(redact_sensitive_query_values(value))
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return redact_routeros_credentials(redact_sensitive_query_values(str(value)))


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
            # Prefer the filter-sanitized cache: recomputing here would
            # re-render the raw traceback (and any RouterOS credential in
            # it) straight from ``record.exc_info``, undoing the filter.
            payload["exception"] = getattr(
                record, "exc_text", None
            ) or self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging() -> None:
    logging_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "sensitive_query": {
                "()": SensitiveQueryFilter,
            }
        },
        "formatters": {
            "json": {
                "()": JsonLogFormatter,
            }
        },
        "handlers": {
            "default": {
                # Lazily-resolving stderr handler (see StderrStreamHandler):
                # avoids binding to a stale/closed stream snapshot, which under
                # pytest caused an "I/O operation on closed file" cascade.
                "()": StderrStreamHandler,
                "formatter": "json",
                "filters": ["sensitive_query"],
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
    # uvicorn configures "uvicorn"/"uvicorn.error"/"uvicorn.access" with their
    # own handlers and propagate=False, so the "root" handlers/filters above
    # never see their records — attach the same redaction directly.
    for uvicorn_logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        install_log_redaction(logging.getLogger(uvicorn_logger_name))


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def install_log_redaction(logger: logging.Logger | None = None) -> None:
    """Attach :class:`SensitiveQueryFilter` to every handler on ``logger``.

    For a process that configures logging outside :func:`configure_logging`
    (e.g. a standalone poller using ``logging.basicConfig`` with a deliberately
    plain-text format that Loki queries depend on) this adds the same
    credential/query redaction without touching the format string. Idempotent:
    calling it again does not attach a second filter to a handler that already
    has one.
    """

    target = logger if logger is not None else logging.getLogger()
    for handler in target.handlers:
        if not any(isinstance(f, SensitiveQueryFilter) for f in handler.filters):
            handler.addFilter(SensitiveQueryFilter())
