from prometheus_client import Counter, Histogram

REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status"],
)
REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["method", "path", "status"],
)
REQUEST_ERRORS = Counter(
    "http_request_errors_total",
    "Total HTTP 5xx responses",
    ["method", "path", "status"],
)

JOB_DURATION = Histogram(
    "job_duration_seconds",
    "Background job duration",
    ["task", "status"],
)

VICTORIAMETRICS_WRITE_FAILURES = Counter(
    "victoriametrics_write_failures_total",
    "Total VictoriaMetrics write failures",
    ["adapter", "operation"],
)

GENIEACS_IDENTITY_RECOVERY_EVENTS = Counter(
    "genieacs_identity_recovery_events_total",
    "Total GenieACS identity recovery events",
    ["event", "result"],
)


def observe_job(task_name: str, status: str, duration: float) -> None:
    JOB_DURATION.labels(task=task_name, status=status).observe(duration)
