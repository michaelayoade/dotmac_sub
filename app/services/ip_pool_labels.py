"""IP pool labels (Phase A): purpose, site and NAS bindings.

Read side only; the binding write lives with the pool form owner in
``web_network_ip``. Labels only. Nothing here changes allocation, assignment or RADIUS; those still
read ``ip_pools.nas_device_id`` and NAS ``radius_pool:`` tags. The pool <-> NAS
link is stored in ``nas_pool_bindings`` (network/access side), not on the pool.

A pool is *unlabelled* when any of these holds:
- ``purpose`` is NULL;
- ``site_ref`` is NULL;
- the pool is active, its purpose is ``customer`` / ``customer_nat`` and it has
  no NAS binding.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.catalog import NasDevice, NasPoolBinding
from app.models.network import IpPool
from app.models.network_monitoring import PopSite
from app.services.nas._helpers import radius_pool_ids_from_tags

POOL_PURPOSES: tuple[tuple[str, str], ...] = (
    ("customer", "Customer"),
    ("customer_nat", "Customer (NAT / CGNAT)"),
    ("infrastructure", "Infrastructure"),
    ("management", "Management"),
)
POOL_PURPOSE_VALUES = frozenset(value for value, _ in POOL_PURPOSES)
POOL_PURPOSE_LABELS = dict(POOL_PURPOSES)
NAS_BOUND_PURPOSES = frozenset({"customer", "customer_nat"})


def pool_is_unlabelled(pool: IpPool, nas_binding_count: int) -> bool:
    if not pool.purpose or pool.site_ref is None:
        return True
    return bool(
        pool.is_active and pool.purpose in NAS_BOUND_PURPOSES and nas_binding_count == 0
    )


def nas_binding_counts(db: Session) -> dict[UUID, int]:
    rows = db.execute(
        select(NasPoolBinding.pool_id, func.count()).group_by(NasPoolBinding.pool_id)
    ).all()
    return {pool_id: int(count) for pool_id, count in rows}


@dataclass(frozen=True)
class PoolLabelRow:
    purpose: str | None
    purpose_label: str | None
    site_name: str | None
    nas_count: int
    unlabelled: bool


def build_pool_label_rows(
    db: Session, pools: Sequence[IpPool]
) -> dict[str, PoolLabelRow]:
    counts = nas_binding_counts(db)
    site_names = {str(site.id): site.name for site in list_pop_sites(db)}
    rows: dict[str, PoolLabelRow] = {}
    for pool in pools:
        count = counts.get(pool.id, 0)
        rows[str(pool.id)] = PoolLabelRow(
            purpose=pool.purpose,
            purpose_label=POOL_PURPOSE_LABELS.get(pool.purpose or ""),
            site_name=site_names.get(str(pool.site_ref)) if pool.site_ref else None,
            nas_count=count,
            unlabelled=pool_is_unlabelled(pool, count),
        )
    return rows


def list_pop_sites(db: Session) -> list[PopSite]:
    return list(
        db.execute(
            select(PopSite).where(PopSite.is_active.is_(True)).order_by(PopSite.name)
        )
        .scalars()
        .all()
    )


def saved_binding_nas_ids(db: Session, pool_id: UUID) -> list[str]:
    ids = db.execute(
        select(NasPoolBinding.nas_device_id).where(NasPoolBinding.pool_id == pool_id)
    ).scalars()
    return sorted(str(i) for i in ids)


def suggested_nas_ids(db: Session, pool: IpPool) -> list[str]:
    """Active NAS linked to the pool today: ``nas_device_id`` FK union tags."""
    tag_pool_id = str(pool.id)
    result: list[str] = []
    devices = (
        db.execute(
            select(NasDevice)
            .where(NasDevice.is_active.is_(True))
            .order_by(NasDevice.name.asc())
        )
        .scalars()
        .all()
    )
    for device in devices:
        if device.id == pool.nas_device_id or tag_pool_id in radius_pool_ids_from_tags(
            device.tags
        ):
            result.append(str(device.id))
    return result


@dataclass(frozen=True)
class PoolLabelFormState:
    selected_nas_ids: list[str]
    nas_prefilled: bool
    nas_options: list[NasDevice]
    sites: list[PopSite]


def pool_label_form_state(
    db: Session,
    *,
    pool_id: str | None,
    submitted_nas_ids: Sequence[str] | None = None,
) -> PoolLabelFormState:
    """Form state for the three label fields.

    A submitted selection (re-render after an error) wins. Otherwise saved
    bindings are shown; a pool with none shows today's FK/tag NAS as a
    suggestion the user must save. Nothing is written here.
    """
    pool = db.get(IpPool, UUID(str(pool_id))) if pool_id else None
    saved: list[str] = []
    prefilled = False
    if submitted_nas_ids is not None:
        selected = list(submitted_nas_ids)
    elif pool is not None:
        saved = saved_binding_nas_ids(db, pool.id)
        selected = saved
        if not saved:
            selected = suggested_nas_ids(db, pool)
            prefilled = bool(selected)
    else:
        selected = []
    options = list(
        db.execute(
            select(NasDevice)
            .where(
                NasDevice.is_active.is_(True)
                | NasDevice.id.in_([UUID(i) for i in selected])
            )
            .order_by(NasDevice.name.asc())
        )
        .scalars()
        .all()
    )
    return PoolLabelFormState(
        selected_nas_ids=selected,
        nas_prefilled=prefilled,
        nas_options=options,
        sites=list_pop_sites(db),
    )
