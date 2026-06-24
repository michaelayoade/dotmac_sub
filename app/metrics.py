from prometheus_client import REGISTRY, Counter, Histogram
from prometheus_client.registry import Collector

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

# Connectivity state-machine observability (CONNECTIVITY_STATE_MACHINE.md step 2c).
# Direct (legacy) writes of connectivity-derived state, labelled by field and
# the write source. ``source=reconciler`` is the legitimate single writer;
# any other source is a legacy direct write we want to drive to zero before
# absorbing/deleting that writer. The reconciler marks its own writes via
# ``connectivity_reconciler.reconciler_write_scope`` so they are NOT counted as
# legacy.
CONNECTIVITY_DIRECT_WRITE = Counter(
    "connectivity_direct_write_total",
    "Writes of reconciler-owned connectivity state, by field and source",
    ["field", "source"],
)
# Shadow-mode disagreement between the desired connectivity state (derived) and
# the actual stored state, by dimension. Non-zero while shadowing means the
# grown reconciler would change something — inspect before flipping apply=True.
CONNECTIVITY_SHADOW_DIFF = Counter(
    "connectivity_shadow_diff_total",
    "Desired-vs-actual connectivity disagreements observed in shadow, by dimension",
    ["dimension"],
)


class _SuspensionAuditCollector(Collector):
    """Exports the latest suspension-audit result at scrape time.

    The audit runs in a Celery worker; a Gauge set there is invisible to the
    web process that serves /metrics (no multiprocess mode, workers recycle).
    The task stores its result in Redis and this collector reads it back on
    each scrape — one cached-client GET, fail-soft.

    Non-zero radius_suspension_audit_leaks means a fully-blocked subscriber
    can still reach the network unrestricted (kind=open_access /
    in_active_group / open_session) or per-service suspension is structurally
    defeated by a shared credential (kind=mixed_status_subscribers).
    """

    def collect(self):  # noqa: ANN201 - prometheus collector protocol
        from prometheus_client.core import GaugeMetricFamily

        try:
            from app.services.radius_reconciliation import load_latest_audit

            data = load_latest_audit()
        except Exception:
            return
        if not data:
            return
        leaks = GaugeMetricFamily(
            "radius_suspension_audit_leaks",
            "Suspension-enforcement audit leak count by class",
            labels=["kind"],
        )
        for kind, count in (data.get("counts") or {}).items():
            leaks.add_metric([kind], float(count or 0))
        leaks.add_metric(
            ["mixed_status_subscribers"],
            float(data.get("mixed_status_subscribers") or 0),
        )
        yield leaks

        ran_at = data.get("ran_at")
        if ran_at:
            from datetime import UTC, datetime

            try:
                age = (
                    datetime.now(UTC) - datetime.fromisoformat(ran_at)
                ).total_seconds()
            except ValueError:
                return
            age_metric = GaugeMetricFamily(
                "radius_suspension_audit_age_seconds",
                "Seconds since the last completed suspension audit",
            )
            age_metric.add_metric([], max(age, 0))
            yield age_metric


REGISTRY.register(_SuspensionAuditCollector())


class _IpConsistencyAuditCollector(Collector):
    """Exports the latest IPv4-consistency audit result at scrape time.

    Same Redis-backed, worker-runs/web-scrapes pattern as the suspension
    audit. Non-zero radius_ip_consistency_drift means an active subscriber's
    IPv4 disagrees across its three sources (column / IPAM / radreply) — the
    structural risk behind silent partial desync. kind=assignment_missing is
    the one to watch: the address is backed only by the subscription column.
    """

    def collect(self):  # noqa: ANN201 - prometheus collector protocol
        from prometheus_client.core import GaugeMetricFamily

        try:
            from app.services.ip_consistency_audit import load_latest_ip_audit

            data = load_latest_ip_audit()
        except Exception:
            return
        if not data:
            return
        drift = GaugeMetricFamily(
            "radius_ip_consistency_drift",
            "Active-subscriber IPv4 drift count by class",
            labels=["kind"],
        )
        for kind, count in (data.get("counts") or {}).items():
            drift.add_metric([kind], float(count or 0))
        yield drift

        population = GaugeMetricFamily(
            "radius_ip_consistency_population",
            "Active subscriptions expected to carry a pinned IPv4",
        )
        population.add_metric([], float(data.get("population") or 0))
        yield population

        ran_at = data.get("ran_at")
        if ran_at:
            from datetime import UTC, datetime

            try:
                age = (
                    datetime.now(UTC) - datetime.fromisoformat(ran_at)
                ).total_seconds()
            except ValueError:
                return
            age_metric = GaugeMetricFamily(
                "radius_ip_consistency_audit_age_seconds",
                "Seconds since the last completed IP consistency audit",
            )
            age_metric.add_metric([], max(age, 0))
            yield age_metric


REGISTRY.register(_IpConsistencyAuditCollector())


class _BillingHealthCollector(Collector):
    """Exports billing liveness/anomaly signals at scrape time.

    Cheap, indexed aggregate queries computed on scrape (no worker needed).
    Wrapped so a transient DB hiccup yields no metrics rather than breaking the
    whole /metrics endpoint. See app/services/billing_health.py. Alert on:
    billing_paid_invoices_with_balance > 0; billing_invoice_scan_ratio low;
    billing_payment_volume_collapsed == 1.
    """

    def collect(self):  # noqa: ANN201 - prometheus collector protocol
        from prometheus_client.core import GaugeMetricFamily

        try:
            from app.services.billing_health import billing_health_snapshot
            from app.services.db_session_adapter import db_session_adapter

            with db_session_adapter.session() as db:
                snap = billing_health_snapshot(db)
        except Exception:
            return

        def gauge(name: str, help_text: str, value: float):
            g = GaugeMetricFamily(name, help_text)
            g.add_metric([], float(value))
            return g

        yield gauge(
            "billing_paid_invoices_with_balance",
            "Invoices status=paid with non-zero balance_due (AR-integrity defect)",
            snap.paid_with_balance_count,
        )
        yield gauge(
            "billing_invoice_last_scanned",
            "subscriptions_scanned of the most recent billing run",
            snap.last_scanned or 0,
        )
        yield gauge(
            "billing_active_subscriptions",
            "Active subscriptions (invoice-cycle eligibility denominator)",
            snap.eligible_active_subs,
        )
        if snap.scan_ratio is not None:
            yield gauge(
                "billing_invoice_scan_ratio",
                "last_scanned / active_subscriptions (low = cohort silently skipped)",
                snap.scan_ratio,
            )
        yield gauge(
            "billing_payments_succeeded_24h",
            "Succeeded payments in the last 24h",
            snap.payments_24h,
        )
        yield gauge(
            "billing_payments_succeeded_7d_daily_avg",
            "Trailing 7-day daily average of succeeded payments (baseline)",
            snap.payments_7d_daily_avg,
        )
        if snap.payment_volume_ratio is not None:
            yield gauge(
                "billing_payment_volume_ratio",
                "last-24h payments / 7-day daily average (collapse = intake broke)",
                snap.payment_volume_ratio,
            )
        yield gauge(
            "billing_payment_volume_collapsed",
            "1 if last-24h payment volume collapsed vs the 7-day baseline",
            1.0 if snap.payment_volume_collapsed else 0.0,
        )
        yield gauge(
            "billing_enforcement_covered_but_locked",
            "Accounts under a billing lock whose ledger balance is >= 0 "
            "(wrongful-suspension drift; should be 0)",
            snap.covered_but_locked,
        )

        # §6.3 per-runner heartbeat freshness (label = task).
        stale = GaugeMetricFamily(
            "billing_runner_heartbeat_stale",
            "1 if an enabled critical runner has no fresh success heartbeat",
            labels=["task"],
        )
        age = GaugeMetricFamily(
            "billing_runner_heartbeat_age_seconds",
            "Seconds since a critical runner last succeeded",
            labels=["task"],
        )
        for r in snap.runners:
            if not r.enabled:
                continue
            stale.add_metric([r.task_name], 1.0 if r.stale else 0.0)
            if r.age_seconds is not None:
                age.add_metric([r.task_name], max(r.age_seconds, 0.0))
        yield stale
        yield age


REGISTRY.register(_BillingHealthCollector())

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
