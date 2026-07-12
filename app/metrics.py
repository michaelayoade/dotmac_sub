from prometheus_client import REGISTRY, Counter, Histogram
from prometheus_client.registry import Collector


def _gauge_description(name: str, help_text: str, labels: list[str] | None = None):  # noqa: ANN202 - prometheus collector protocol
    from prometheus_client.core import GaugeMetricFamily

    return GaugeMetricFamily(name, help_text, labels=labels)


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
API_SYNC_PRESSURE_LIMITED = Counter(
    "api_sync_pressure_limited_total",
    "API sync requests rejected before they could acquire DB resources",
    ["bucket", "scope"],
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

    def describe(self):  # noqa: ANN201 - prometheus collector protocol
        yield _gauge_description(
            "radius_suspension_audit_leaks",
            "Suspension-enforcement audit leak count by class",
            labels=["kind"],
        )
        yield _gauge_description(
            "radius_suspension_audit_age_seconds",
            "Seconds since the last completed suspension audit",
        )

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


class _ObservabilityStateCollector(Collector):
    """Export Redis-backed domain state produced by worker-side services."""

    def describe(self):  # noqa: ANN201 - prometheus collector protocol
        yield _gauge_description(
            "observability_state",
            "Latest bounded domain state value",
            labels=["domain", "signal", "scope"],
        )
        yield _gauge_description(
            "observability_snapshot_age_seconds",
            "Seconds since the latest domain state snapshot",
            labels=["domain"],
        )
        yield _gauge_description(
            "observability_snapshot_status",
            "Latest domain state snapshot status",
            labels=["domain", "status"],
        )

    def collect(self):  # noqa: ANN201 - prometheus collector protocol
        from datetime import UTC, datetime

        from prometheus_client.core import GaugeMetricFamily

        try:
            from app.services.observability import (
                load_state_snapshot,
                state_snapshot_domains,
            )
        except Exception:
            return

        state = GaugeMetricFamily(
            "observability_state",
            "Latest bounded domain state value",
            labels=["domain", "signal", "scope"],
        )
        age = GaugeMetricFamily(
            "observability_snapshot_age_seconds",
            "Seconds since the latest domain state snapshot",
            labels=["domain"],
        )
        status_metric = GaugeMetricFamily(
            "observability_snapshot_status",
            "Latest domain state snapshot status",
            labels=["domain", "status"],
        )
        found = False
        for domain in state_snapshot_domains():
            try:
                snapshot = load_state_snapshot(domain)
            except Exception:
                continue
            if not snapshot:
                continue
            for observation in snapshot.get("observations") or []:
                if not isinstance(observation, dict):
                    continue
                try:
                    state.add_metric(
                        [
                            domain,
                            str(observation["signal"]),
                            str(observation["scope"]),
                        ],
                        float(observation["value"]),
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                found = True

            observed_at = snapshot.get("observed_at")
            if observed_at:
                try:
                    recorded = datetime.fromisoformat(str(observed_at))
                    if recorded.tzinfo is None:
                        recorded = recorded.replace(tzinfo=UTC)
                    snapshot_age = max(
                        0.0,
                        (datetime.now(UTC) - recorded).total_seconds(),
                    )
                    age.add_metric([domain], snapshot_age)
                    found = True
                except ValueError:
                    pass
            snapshot_status = str(snapshot.get("status") or "error")
            for status in ("ok", "degraded", "error"):
                status_metric.add_metric(
                    [domain, status],
                    1.0 if status == snapshot_status else 0.0,
                )
            found = True

        if found:
            yield state
            yield age
            yield status_metric


REGISTRY.register(_ObservabilityStateCollector())


class _DatabasePressureCollector(Collector):
    """Exports SQLAlchemy pool and PostgreSQL session pressure.

    This catches the failure mode behind the selfcare 504 incident: long-lived
    app transactions and pool saturation under API sync/admin traffic. The
    collector is fail-soft so /metrics remains available even if the DB is sick.
    """

    def describe(self):  # noqa: ANN201 - prometheus collector protocol
        yield _gauge_description(
            "sqlalchemy_pool_checked_out",
            "Connections currently checked out from the app SQLAlchemy pool",
        )
        yield _gauge_description(
            "sqlalchemy_pool_size",
            "Configured size of the app SQLAlchemy pool",
        )
        yield _gauge_description(
            "sqlalchemy_pool_overflow",
            "Current SQLAlchemy pool overflow connection count",
        )
        yield _gauge_description(
            "postgres_activity_connections",
            "PostgreSQL session counts by state for the application database",
            labels=["state"],
        )
        yield _gauge_description(
            "postgres_connection_utilization_pct",
            "PostgreSQL connection utilization percentage",
        )
        yield _gauge_description(
            "postgres_max_idle_in_transaction_seconds",
            "Oldest idle-in-transaction PostgreSQL session age in seconds",
        )
        yield _gauge_description(
            "postgres_waiting_on_lock",
            "PostgreSQL sessions waiting on locks",
        )

    def collect(self):  # noqa: ANN201 - prometheus collector protocol
        from prometheus_client.core import GaugeMetricFamily

        try:
            from app.db import _engine

            pool = _engine.pool
            for name, help_text, value in (
                (
                    "sqlalchemy_pool_checked_out",
                    "Connections currently checked out from the app SQLAlchemy pool",
                    getattr(pool, "checkedout", lambda: 0)(),
                ),
                (
                    "sqlalchemy_pool_size",
                    "Configured size of the app SQLAlchemy pool",
                    getattr(pool, "size", lambda: 0)(),
                ),
                (
                    "sqlalchemy_pool_overflow",
                    "Current SQLAlchemy pool overflow connection count",
                    getattr(pool, "overflow", lambda: 0)(),
                ),
            ):
                gauge = GaugeMetricFamily(name, help_text)
                gauge.add_metric([], float(value or 0))
                yield gauge
        except Exception:
            pass

        try:
            from app.services.db_session_adapter import db_session_adapter
            from app.services.infrastructure_health import _postgres_activity_snapshot

            with db_session_adapter.read_session() as db:
                activity = _postgres_activity_snapshot(db)
        except Exception:
            return

        connections = GaugeMetricFamily(
            "postgres_activity_connections",
            "PostgreSQL session counts by state for the application database",
            labels=["state"],
        )
        for key, label in (
            ("total_connections", "total"),
            ("active_connections", "active"),
            ("idle_connections", "idle"),
            ("idle_in_transaction", "idle_in_transaction"),
            ("idle_in_transaction_over_60s", "idle_in_transaction_over_60s"),
        ):
            connections.add_metric([label], float(activity.get(key) or 0))
        yield connections

        for name, help_text, key in (
            (
                "postgres_connection_utilization_pct",
                "PostgreSQL connection utilization percentage",
                "connection_utilization_pct",
            ),
            (
                "postgres_max_idle_in_transaction_seconds",
                "Oldest idle-in-transaction PostgreSQL session age in seconds",
                "max_idle_in_transaction_seconds",
            ),
            (
                "postgres_waiting_on_lock",
                "PostgreSQL sessions waiting on locks",
                "waiting_on_lock",
            ),
        ):
            gauge = GaugeMetricFamily(name, help_text)
            gauge.add_metric([], float(activity.get(key) or 0))
            yield gauge


REGISTRY.register(_DatabasePressureCollector())


class _IpConsistencyAuditCollector(Collector):
    """Exports the latest IPv4-consistency audit result at scrape time.

    Same Redis-backed, worker-runs/web-scrapes pattern as the suspension
    audit. Non-zero radius_ip_consistency_drift means an active subscriber's
    IPv4 disagrees across its three sources (column / IPAM / radreply) — the
    structural risk behind silent partial desync. kind=assignment_missing is
    the one to watch: the address is backed only by the subscription column.
    """

    def describe(self):  # noqa: ANN201 - prometheus collector protocol
        yield _gauge_description(
            "radius_ip_consistency_drift",
            "Active-subscriber IPv4 drift count by class",
            labels=["kind"],
        )
        yield _gauge_description(
            "radius_ip_consistency_population",
            "Active subscriptions expected to carry a pinned IPv4",
        )
        yield _gauge_description(
            "radius_ip_consistency_audit_age_seconds",
            "Seconds since the last completed IP consistency audit",
        )

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

    def describe(self):  # noqa: ANN201 - prometheus collector protocol
        yield _gauge_description(
            "billing_paid_invoices_with_balance",
            "Invoices status=paid with non-zero balance_due (AR-integrity defect)",
        )
        yield _gauge_description(
            "billing_invoice_last_scanned",
            "subscriptions_scanned of the most recent billing run",
        )
        yield _gauge_description(
            "billing_active_subscriptions",
            "Active subscriptions (invoice-cycle eligibility denominator)",
        )
        yield _gauge_description(
            "billing_invoice_scan_ratio",
            "last_scanned / active_subscriptions (low = cohort silently skipped)",
        )
        yield _gauge_description(
            "billing_payments_succeeded_24h",
            "Succeeded payments in the last 24h",
        )
        yield _gauge_description(
            "billing_payments_succeeded_7d_daily_avg",
            "Trailing 7-day daily average of succeeded payments (baseline)",
        )
        yield _gauge_description(
            "billing_payment_volume_ratio",
            "last-24h payments / 7-day daily average (collapse = intake broke)",
        )
        yield _gauge_description(
            "billing_payment_volume_collapsed",
            "1 if last-24h payment volume collapsed vs the 7-day baseline",
        )
        yield _gauge_description(
            "billing_enforcement_covered_but_locked",
            "Accounts under a billing lock whose ledger balance is >= 0 "
            "(wrongful-suspension drift; should be 0)",
        )
        yield _gauge_description(
            "billing_runner_heartbeat_stale",
            "1 if an enabled critical runner has no fresh success heartbeat",
            labels=["task"],
        )
        yield _gauge_description(
            "billing_runner_heartbeat_age_seconds",
            "Seconds since a critical runner last succeeded",
            labels=["task"],
        )
        yield _gauge_description(
            "billing_unbilled_active_subscriptions",
            "Active subscriptions that no enabled billing path covers",
            labels=["reason"],
        )
        yield _gauge_description(
            "billing_negative_prepaid_balance_accounts",
            "Active/collectible prepaid accounts whose wallet balance is below zero",
        )
        yield _gauge_description(
            "billing_negative_prepaid_balance_total",
            "Absolute total negative prepaid wallet exposure",
        )
        yield _gauge_description(
            "billing_negative_prepaid_sweep_disabled_accounts",
            "Negative prepaid accounts while the prepaid balance sweep is disabled",
        )

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
        yield gauge(
            "billing_negative_prepaid_balance_accounts",
            "Active/collectible prepaid accounts whose wallet balance is below zero",
            snap.negative_prepaid_balance_count,
        )
        yield gauge(
            "billing_negative_prepaid_balance_total",
            "Absolute total negative prepaid wallet exposure",
            snap.negative_prepaid_balance_total,
        )
        yield gauge(
            "billing_negative_prepaid_sweep_disabled_accounts",
            "Negative prepaid accounts while the prepaid balance sweep is disabled",
            snap.negative_prepaid_with_sweep_disabled_count,
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

        # §6.1 billing-path coverage: active subs no enabled path will bill.
        unbilled = GaugeMetricFamily(
            "billing_unbilled_active_subscriptions",
            "Active subscriptions that no enabled billing path covers",
            labels=["reason"],
        )
        unbilled.add_metric(["no_billing_path"], float(snap.unbilled_no_path))
        unbilled.add_metric(
            ["terminal_account"], float(snap.active_subs_on_terminal_account)
        )
        yield unbilled


REGISTRY.register(_BillingHealthCollector())


class _ConnectivityShadowCollector(Collector):
    """Exports the latest full-base connectivity shadow-audit result at scrape
    time (worker runs the sweep, stores Redis; web scrapes — same pattern as the
    suspension/IP audits). ``connectivity_shadow_drift{dimension}`` is the
    cutover-readiness gauge: a point-in-time count of subscribers whose
    connectivity dimension disagrees with the desired state. When every
    dimension reads ~0 the connectivity reconciler can be promoted from shadow
    to sole-writer.
    """

    def describe(self):  # noqa: ANN201 - prometheus collector protocol
        yield _gauge_description(
            "connectivity_shadow_drift",
            "Subscribers whose connectivity dimension disagrees with desired",
            labels=["dimension"],
        )
        yield _gauge_description(
            "connectivity_shadow_population",
            "Subscribers swept by the connectivity shadow audit",
        )
        yield _gauge_description(
            "connectivity_shadow_audit_age_seconds",
            "Seconds since the last completed connectivity shadow audit",
        )

    def collect(self):  # noqa: ANN201 - prometheus collector protocol
        from prometheus_client.core import GaugeMetricFamily

        try:
            from app.services.connectivity_reconciler import (
                load_connectivity_shadow_result,
            )

            data = load_connectivity_shadow_result()
        except Exception:
            return
        if not data:
            return

        drift = GaugeMetricFamily(
            "connectivity_shadow_drift",
            "Subscribers whose connectivity dimension disagrees with desired",
            labels=["dimension"],
        )
        for dim, count in (data.get("counts") or {}).items():
            drift.add_metric([dim], float(count or 0))
        yield drift

        population = GaugeMetricFamily(
            "connectivity_shadow_population",
            "Subscribers swept by the connectivity shadow audit",
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
                "connectivity_shadow_audit_age_seconds",
                "Seconds since the last completed connectivity shadow audit",
            )
            age_metric.add_metric([], max(age, 0))
            yield age_metric


REGISTRY.register(_ConnectivityShadowCollector())


class _PollerHealthCollector(Collector):
    """Exports bandwidth-poller health at scrape time.

    The poller is a separate process, so Prometheus metrics set there aren't
    visible here. It writes a Redis snapshot each cycle and this collector reads
    it on scrape — fail-soft. ``bandwidth_poller_last_cycle_age_seconds`` is the
    key liveness signal: if the poller dies it grows unbounded (alert on it);
    ``bandwidth_poller_devices_failing`` surfaces silently-broken routers.
    """

    def describe(self):  # noqa: ANN201 - prometheus collector protocol
        yield _gauge_description(
            "bandwidth_poller_devices_total",
            "MikroTik devices in the poller pool",
        )
        yield _gauge_description(
            "bandwidth_poller_devices_ok",
            "Devices polled without recent failures",
        )
        yield _gauge_description(
            "bandwidth_poller_devices_failing",
            "Devices in failure backoff (silently broken)",
        )
        yield _gauge_description(
            "bandwidth_poller_cycle_seconds",
            "Duration of the poller's last completed cycle",
        )
        yield _gauge_description(
            "bandwidth_poller_last_cycle_age_seconds",
            "Seconds since the poller's last completed cycle (liveness)",
        )

    def collect(self):  # noqa: ANN201 - prometheus collector protocol
        from prometheus_client.core import GaugeMetricFamily

        try:
            from app.services.poller_health import load_poller_health

            data = load_poller_health()
        except Exception:
            return
        if not data:
            return

        for key, help_text in (
            ("devices_total", "MikroTik devices in the poller pool"),
            ("devices_ok", "Devices polled without recent failures"),
            ("devices_failing", "Devices in failure backoff (silently broken)"),
        ):
            gauge = GaugeMetricFamily(f"bandwidth_poller_{key}", help_text)
            gauge.add_metric([], float(data.get(key) or 0))
            yield gauge

        cycle = data.get("cycle_seconds")
        if cycle is not None:
            gauge = GaugeMetricFamily(
                "bandwidth_poller_cycle_seconds",
                "Duration of the poller's last completed cycle",
            )
            gauge.add_metric([], float(cycle))
            yield gauge

        ts = data.get("ts")
        if ts:
            from datetime import UTC, datetime

            try:
                age = (datetime.now(UTC) - datetime.fromisoformat(ts)).total_seconds()
            except ValueError:
                return
            gauge = GaugeMetricFamily(
                "bandwidth_poller_last_cycle_age_seconds",
                "Seconds since the poller's last completed cycle (liveness)",
            )
            gauge.add_metric([], max(age, 0))
            yield gauge


REGISTRY.register(_PollerHealthCollector())

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

OBSERVABILITY_EVENTS_TOTAL = Counter(
    "observability_events_total",
    "Shared observability events recorded by domain and signal",
    ["domain", "signal", "status"],
)

NOTIFICATION_QUEUE_OUTCOMES_TOTAL = Counter(
    "notification_queue_outcomes_total",
    "Notification queue processing outcomes",
    ["outcome"],
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
