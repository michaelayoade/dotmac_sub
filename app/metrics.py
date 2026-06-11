from prometheus_client import Counter, Gauge, Histogram

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

# Set by app.tasks.radius.audit_suspension_enforcement. Non-zero means a
# fully-blocked subscriber can still reach the network (kind=usable_password /
# in_active_group / open_session) or per-service suspension is being defeated
# by a shared credential (kind=mixed_status_subscribers).
RADIUS_SUSPENSION_AUDIT_LEAKS = Gauge(
    "radius_suspension_audit_leaks",
    "Suspension-enforcement audit leak count by class",
    ["kind"],
)

GENIEACS_IDENTITY_RECOVERY_EVENTS = Counter(
    "genieacs_identity_recovery_events_total",
    "Total GenieACS identity recovery events",
    ["event", "result"],
)

APP_CACHE_LOOKUPS = Counter(
    "app_cache_lookups_total",
    "Application cache lookups",
    ["cache", "result"],
)

APP_CACHE_REFRESH_DURATION = Histogram(
    "app_cache_refresh_duration_seconds",
    "Application cache refresh duration",
    ["cache", "status"],
)

APP_CACHE_FALLBACKS = Counter(
    "app_cache_fallbacks_total",
    "Application cache fallbacks to synchronous computation or live fetch",
    ["cache", "reason"],
)

CUSTOMER_IDENTITY_RESOLUTION_TOTAL = Counter(
    "customer_identity_resolution_total",
    "Inbound customer identity resolution outcomes",
    ["result", "identity_type", "match_source", "confidence", "inbound_channel"],
)


def observe_job(task_name: str, status: str, duration: float) -> None:
    JOB_DURATION.labels(task=task_name, status=status).observe(duration)


def record_cache_lookup(cache_name: str, result: str) -> None:
    APP_CACHE_LOOKUPS.labels(cache=cache_name, result=result).inc()


def observe_cache_refresh(cache_name: str, status: str, duration: float) -> None:
    APP_CACHE_REFRESH_DURATION.labels(cache=cache_name, status=status).observe(duration)


def record_cache_fallback(cache_name: str, reason: str) -> None:
    APP_CACHE_FALLBACKS.labels(cache=cache_name, reason=reason).inc()


def record_customer_identity_resolution(
    *,
    result: str | None,
    identity_type: str | None,
    match_source: str | None,
    confidence: str | None,
    inbound_channel: str | None,
) -> None:
    CUSTOMER_IDENTITY_RESOLUTION_TOTAL.labels(
        result=str(result or "unknown"),
        identity_type=str(identity_type or "unknown"),
        match_source=str(match_source or "none"),
        confidence=str(confidence or "NONE"),
        inbound_channel=str(inbound_channel or "unknown"),
    ).inc()
