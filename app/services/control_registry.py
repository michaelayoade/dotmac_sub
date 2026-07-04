"""Single control-plane resolver: modules, features, and safety in one place.

The app historically grew FIVE blended kinds of setting — product modules,
capability features, scheduler/task toggles, safety gates, and tuning knobs —
resolved in different places with different fail directions. That blend is what
let billing automation be silently half-off.

This module is the single source of truth and the single read path:

  * Layer 1 — MODULE   : "does this product area exist?"  (modules.* domain)
  * Layer 2 — FEATURE  : "is this capability on?" (composes with its module)
  * Layer 3 — SAFETY   : guardrails / kill switches — resolved SEPARATELY at
                         action time (see billing_enforcement_guards /
                         check_billing_switch), never folded into is_enabled().

``is_enabled(db, "billing.autopay")`` is the one call sites should use. A
feature is enabled only if BOTH its module and its own flag are on. Resolution
order per control: explicit env override → explicit DB row (canonical, then any
legacy alias) → registry default, with a per-control fail direction
(``on_missing``). Legacy-alias reads are logged so a later stale-key report can
prove which old keys are still live before they're deleted.

The module/feature substrate is :mod:`app.services.module_manager` (already
fail-open + cached); this layer adds the capability features that have parallel
scheduler/task keys (billing.invoicing/autopay/collections/overdue, …) and the
one composed resolver.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum

from sqlalchemy.orm import Session

from app.models.domain_settings import DomainSetting, SettingDomain
from app.services import module_manager

logger = logging.getLogger(__name__)


class Layer(str, Enum):
    module = "module"
    feature = "feature"
    safety = "safety"


@dataclass(frozen=True)
class LegacyAlias:
    domain: SettingDomain
    key: str
    env: str | None = None


@dataclass(frozen=True)
class Control:
    """One control-plane setting with a single, declared meaning."""

    key: str  # canonical dotted key, e.g. "billing.autopay"
    layer: Layer
    default: bool
    # Fail direction when NO value is found anywhere. Revenue/feature controls
    # fail OPEN (absent = on); dangerous capabilities and safety gates fail
    # CLOSED. This is the property whose absence caused the billing outage.
    on_missing: bool
    owner_module: str | None = None  # required for features
    legacy: tuple[LegacyAlias, ...] = field(default_factory=tuple)
    description: str = ""


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


# ---------------------------------------------------------------------------
# Registry. Modules come from module_manager.MODULE_KEY_MAP (Layer 1). Here we
# add the capability features that own scheduler/task behavior, each aliased to
# the legacy key(s) it currently reads so this is behavior-neutral on rollout.
# ---------------------------------------------------------------------------

_B = SettingDomain.billing
_C = SettingDomain.collections
_CAT = SettingDomain.catalog
_N = SettingDomain.notification
_P = SettingDomain.provisioning
_NET = SettingDomain.network
_SCH = SettingDomain.scheduler
_G = SettingDomain.gis


_FEATURE_CONTROLS: tuple[Control, ...] = (
    Control(
        key="billing.invoicing",
        layer=Layer.feature,
        owner_module="billing",
        default=True,
        on_missing=True,
        legacy=(LegacyAlias(_B, "billing_enabled", "BILLING_ENABLED"),),
        description="Recurring invoice generation (the billing runner).",
    ),
    Control(
        key="billing.autopay",
        layer=Layer.feature,
        owner_module="billing",
        default=True,
        on_missing=True,
        legacy=(LegacyAlias(_B, "autopay_enabled", "BILLING_AUTOPAY_ENABLED"),),
        description="Auto-charge saved cards for due invoices.",
    ),
    Control(
        key="billing.collections",
        layer=Layer.feature,
        owner_module="billing",
        default=True,
        on_missing=True,
        legacy=(LegacyAlias(_C, "dunning_enabled", "DUNNING_ENABLED"),),
        description="Dunning / collections workflow.",
    ),
    Control(
        key="billing.overdue_marking",
        layer=Layer.feature,
        owner_module="billing",
        default=True,
        on_missing=True,
        legacy=(
            LegacyAlias(_B, "overdue_check_enabled", "BILLING_OVERDUE_CHECK_ENABLED"),
        ),
        description="Mark past-due invoices overdue.",
    ),
    Control(
        key="billing.arrangements",
        layer=Layer.feature,
        owner_module="billing",
        default=True,
        on_missing=True,
        legacy=(
            LegacyAlias(_C, "arrangement_check_enabled", "ARRANGEMENT_CHECK_ENABLED"),
        ),
        description="Payment-arrangement due checks.",
    ),
    Control(
        key="billing.topup_reconciliation",
        layer=Layer.feature,
        owner_module="billing",
        default=True,
        on_missing=True,
        legacy=(
            LegacyAlias(
                _B,
                "topup_reconciliation_enabled",
                "BILLING_TOPUP_RECONCILIATION_ENABLED",
            ),
        ),
        description="Reconcile pending gateway top-ups.",
    ),
    Control(
        # Cadence runner for billing notifications. DEFAULT OFF historically —
        # fail-closed so this stays a deliberate opt-in.
        key="billing.notifications_hourly",
        layer=Layer.feature,
        owner_module="billing",
        default=False,
        on_missing=False,
        legacy=(
            LegacyAlias(
                _C,
                "billing_notifications_hourly_enabled",
                "BILLING_NOTIFICATIONS_HOURLY_ENABLED",
            ),
        ),
        description="Hourly billing-notification send-window runner.",
    ),
    Control(
        # Balance/expiry-based prepaid enforcement sweep. DEFAULT OFF —
        # fail-closed so arming this customer-suspending sweep is a deliberate
        # opt-in (Item 2 of the prepaid/invoice/deposit alignment).
        key="collections.prepaid_balance_enforcement",
        layer=Layer.feature,
        owner_module="billing",
        default=False,
        on_missing=False,
        legacy=(
            LegacyAlias(
                _C,
                "prepaid_balance_enforcement_enabled",
                "PREPAID_BALANCE_ENFORCEMENT_ENABLED",
            ),
        ),
        description="Prepaid balance/expiry suspension sweep.",
    ),
    Control(
        key="catalog.subscription_expiration",
        layer=Layer.feature,
        owner_module="catalog",
        default=True,
        on_missing=True,
        legacy=(
            LegacyAlias(
                _CAT,
                "subscription_expiration_enabled",
                "SUBSCRIPTION_EXPIRATION_ENABLED",
            ),
        ),
        description="Expire subscriptions at end of term.",
    ),
    Control(
        key="catalog.vacation_hold_resume",
        layer=Layer.feature,
        owner_module="catalog",
        default=True,
        on_missing=True,
        legacy=(
            LegacyAlias(
                _CAT,
                "vacation_hold_auto_resume_enabled",
                "VACATION_HOLD_AUTO_RESUME_ENABLED",
            ),
        ),
        description="Auto-resume expired vacation holds.",
    ),
    # NOTE: nas_backup_retention intentionally NOT registered — it is network
    # infrastructure housekeeping, not a catalog (product) capability. Leaving it
    # unregistered keeps it on the pure legacy path with no accidental module
    # coupling (disabling a module must not silently stop NAS backup cleanup).
    Control(
        key="notifications.queue",
        layer=Layer.feature,
        owner_module="notifications",
        default=True,
        on_missing=True,
        legacy=(
            LegacyAlias(_N, "notification_queue_enabled", "NOTIFICATION_QUEUE_ENABLED"),
        ),
        description="Notification delivery queue runner.",
    ),
    Control(
        key="provisioning.compensation_retry",
        layer=Layer.feature,
        owner_module="provisioning",
        default=True,
        on_missing=True,
        legacy=(
            LegacyAlias(_P, "compensation_retry_enabled", "COMPENSATION_RETRY_ENABLED"),
        ),
        description="Retry pending provisioning compensations.",
    ),
    Control(
        key="network.olt_profile_sync",
        layer=Layer.feature,
        owner_module="network",
        default=False,
        on_missing=False,
        legacy=(
            LegacyAlias(
                _NET,
                "olt_profile_sync_worker_enabled",
                "OLT_PROFILE_SYNC_WORKER_ENABLED",
            ),
        ),
        description="OLT profile sync worker.",
    ),
    Control(
        key="network.tr069_sync",
        layer=Layer.feature,
        owner_module="network",
        default=True,
        on_missing=True,
        legacy=(LegacyAlias(_NET, "tr069_sync_enabled", "TR069_SYNC_ENABLED"),),
        description="TR-069 inventory sync.",
    ),
    Control(
        key="network.tr069_job_execution",
        layer=Layer.feature,
        owner_module="network",
        default=True,
        on_missing=True,
        legacy=(
            LegacyAlias(
                _NET, "tr069_job_execution_enabled", "TR069_JOB_EXECUTION_ENABLED"
            ),
        ),
        description="TR-069 job execution.",
    ),
    Control(
        key="network.tr069_health_check",
        layer=Layer.feature,
        owner_module="network",
        default=True,
        on_missing=True,
        legacy=(
            LegacyAlias(
                _NET, "tr069_health_check_enabled", "TR069_HEALTH_CHECK_ENABLED"
            ),
        ),
        description="TR-069 health check.",
    ),
    Control(
        key="network.tr069_cleanup",
        layer=Layer.feature,
        owner_module="network",
        default=True,
        on_missing=True,
        legacy=(LegacyAlias(_NET, "tr069_cleanup_enabled", "TR069_CLEANUP_ENABLED"),),
        description="TR-069 cleanup.",
    ),
    Control(
        key="network.tr069_genieacs_stale_cleanup",
        layer=Layer.feature,
        owner_module="network",
        default=True,
        on_missing=True,
        legacy=(
            LegacyAlias(
                _NET,
                "tr069_genieacs_stale_cleanup_enabled",
                "TR069_GENIEACS_STALE_CLEANUP_ENABLED",
            ),
        ),
        description="GenieACS stale-device cleanup.",
    ),
    Control(
        key="network.tr069_metrics_scrape",
        layer=Layer.feature,
        owner_module="network",
        default=True,
        on_missing=True,
        legacy=(
            LegacyAlias(
                _NET, "tr069_metrics_scrape_enabled", "TR069_METRICS_SCRAPE_ENABLED"
            ),
        ),
        description="TR-069 metrics scrape.",
    ),
    Control(
        key="network.tr069_ont_runtime_refresh",
        layer=Layer.feature,
        owner_module="network",
        default=True,
        on_missing=True,
        legacy=(
            LegacyAlias(
                _NET,
                "tr069_ont_runtime_refresh_enabled",
                "TR069_ONT_RUNTIME_REFRESH_ENABLED",
            ),
        ),
        description="TR-069 ONT runtime refresh.",
    ),
    Control(
        key="vpn.log_cleanup",
        layer=Layer.feature,
        owner_module="vpn",
        default=True,
        on_missing=True,
        legacy=(
            LegacyAlias(
                _NET, "wireguard_log_cleanup_enabled", "WIREGUARD_LOG_CLEANUP_ENABLED"
            ),
        ),
        description="WireGuard log cleanup.",
    ),
    Control(
        key="vpn.token_cleanup",
        layer=Layer.feature,
        owner_module="vpn",
        default=True,
        on_missing=True,
        legacy=(
            LegacyAlias(
                _NET,
                "wireguard_token_cleanup_enabled",
                "WIREGUARD_TOKEN_CLEANUP_ENABLED",
            ),
        ),
        description="WireGuard token cleanup.",
    ),
    Control(
        key="crm.ticket_pull",
        layer=Layer.feature,
        owner_module="crm",
        default=False,
        on_missing=False,
        legacy=(
            LegacyAlias(_SCH, "crm_ticket_pull_enabled", "CRM_TICKET_PULL_ENABLED"),
        ),
        description="Pull tickets from CRM.",
    ),
    Control(
        key="crm.billing_push",
        layer=Layer.feature,
        owner_module="crm",
        default=False,
        on_missing=False,
        legacy=(
            LegacyAlias(_SCH, "crm_billing_push_enabled", "CRM_BILLING_PUSH_ENABLED"),
        ),
        description="Push billing snapshots to CRM.",
    ),
    Control(
        key="gis.sync",
        layer=Layer.feature,
        owner_module="gis",
        default=True,
        on_missing=True,
        legacy=(LegacyAlias(_G, "sync_enabled", "GIS_SYNC_ENABLED"),),
        description="GIS sync.",
    ),
)


_CONTROLS: dict[str, Control] = {}
# Layer 1: every product module from the existing map, fail-open.
for _m in module_manager.MODULE_KEY_MAP:
    _CONTROLS[_m] = Control(
        key=_m,
        layer=Layer.module,
        default=True,
        on_missing=True,
        description=module_manager.MODULE_LABELS.get(_m, _m.title()),
    )
# Layer 2: capability features with scheduler/task ownership.
for _c in _FEATURE_CONTROLS:
    _CONTROLS[_c.key] = _c

# Reverse index: legacy (domain, key) -> canonical control key. Lets the
# scheduler chokepoint (_effective_bool) and task bodies delegate by their
# existing keys without touching every call site.
_LEGACY_INDEX: dict[tuple[SettingDomain, str], str] = {}
for _c in _FEATURE_CONTROLS:
    for _a in _c.legacy:
        _LEGACY_INDEX[(_a.domain, _a.key)] = _c.key


def control_for_legacy(domain: SettingDomain, key: str) -> str | None:
    """Canonical control key for a legacy (domain, key), or None if unmapped."""
    return _LEGACY_INDEX.get((domain, key))


def owner_module_for(canonical_key: str) -> str | None:
    """Owning module of a (feature) control, or None."""
    control = _CONTROLS.get(canonical_key)
    return control.owner_module if control else None


def all_controls() -> Iterable[Control]:
    return _CONTROLS.values()


def _db_value(db: Session, domain: SettingDomain, key: str) -> object | None:
    # query(...).filter(...).filter(...).filter(...).first() — the shape the
    # scheduler tests mock; returns None for "no row".
    setting = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == domain)
        .filter(DomainSetting.key == key)
        .filter(DomainSetting.is_active.is_(True))
        .first()
    )
    if setting is None:
        return None
    return setting.value_json if setting.value_json is not None else setting.value_text


def _resolve_own_flag(db: Session, control: Control) -> bool:
    """Resolve a control's OWN value (ignoring module composition).

    Precedence (highest first): env override → canonical DB row
    (``modules.<feature>``) → legacy alias DB row → ``on_missing`` default. Env
    is the emergency override, so it wins over any stored row. Logs which legacy
    alias supplied the value.
    """
    # 1) Env override (any alias env) — emergency lever, beats stored rows.
    for alias in control.legacy:
        if alias.env:
            env_val = os.getenv(alias.env)
            if env_val is not None:
                return _truthy(env_val)
    # 2) Canonical row (modules.<feature>) — what the admin page will write.
    value = _db_value(db, SettingDomain.modules, control.key.replace(".", "_"))
    if value is not None:
        return _truthy(value)
    # 3) Legacy alias rows — back-compat with pre-registry keys.
    for alias in control.legacy:
        row = _db_value(db, alias.domain, alias.key)
        if row is not None:
            logger.debug(
                "control_registry: %s resolved from legacy alias %s.%s",
                control.key,
                alias.domain.value,
                alias.key,
            )
            return _truthy(row)
    return control.on_missing


def is_enabled(db: Session, key: str) -> bool:
    """The one resolver. ``key`` is a module ("billing") or a dotted feature
    ("billing.autopay"). A feature is enabled only if its module is too."""
    control = _CONTROLS.get(key)
    if control is None:
        # Unknown key: be conservative-but-non-breaking — treat a bare module
        # name via module_manager, else default on (callers should register).
        if "." not in key:
            return module_manager.is_module_enabled(db, key)
        logger.warning("control_registry: unknown control %r; defaulting on", key)
        return True

    if control.layer is Layer.module:
        return module_manager.is_module_enabled(db, control.key)

    # Feature: module gate AND the feature's own flag.
    if control.owner_module and not module_manager.is_module_enabled(
        db, control.owner_module
    ):
        return False
    return _resolve_own_flag(db, control)
