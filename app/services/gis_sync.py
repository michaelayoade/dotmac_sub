from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import BackgroundTasks
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.gis import GeoLocation, GeoLocationType
from app.models.network_monitoring import PopSite
from app.models.subscriber import Address
from app.services.response import ListResponseMixin


@dataclass
class SyncResult(ListResponseMixin):
    created: int = 0
    updated: int = 0
    skipped: int = 0


def _address_display_name(address: Address) -> str:
    if address.label:
        return address.label
    parts = [address.address_line1]
    if address.city:
        parts.append(address.city)
    if address.region:
        parts.append(address.region)
    if address.postal_code:
        parts.append(address.postal_code)
    return ", ".join([part for part in parts if part])


class GeoSync(ListResponseMixin):
    @staticmethod
    def sync_sources(
        db: Session,
        background_tasks: BackgroundTasks,
        sync_pops: bool,
        sync_addresses: bool,
        deactivate_missing: bool,
        background: bool,
    ) -> dict[str, object]:
        if background:
            return GeoSync.queue_sync(
                background_tasks, sync_pops, sync_addresses, deactivate_missing
            )
        return GeoSync.run_sync(db, sync_pops, sync_addresses, deactivate_missing)

    @staticmethod
    def run_sync(
        db: Session,
        sync_pops: bool,
        sync_addresses: bool,
        deactivate_missing: bool,
    ) -> dict[str, object]:
        results: dict[str, object] = {}
        if sync_pops:
            result = GeoSync.sync_pop_sites(db, deactivate_missing=deactivate_missing)
            results["pop_sites"] = {
                "created": result.created,
                "updated": result.updated,
                "skipped": result.skipped,
            }
        if sync_addresses:
            result = GeoSync.sync_addresses(db, deactivate_missing=deactivate_missing)
            results["addresses"] = {
                "created": result.created,
                "updated": result.updated,
                "skipped": result.skipped,
            }
        return results

    @staticmethod
    def queue_sync(
        background_tasks: BackgroundTasks,
        sync_pops: bool,
        sync_addresses: bool,
        deactivate_missing: bool,
    ) -> dict[str, object]:
        def _run_sync() -> None:
            session = SessionLocal()
            try:
                GeoSync.run_sync(
                    session,
                    sync_pops=sync_pops,
                    sync_addresses=sync_addresses,
                    deactivate_missing=deactivate_missing,
                )
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

        background_tasks.add_task(_run_sync)
        return {"status": "queued"}

    @staticmethod
    def sync_pop_sites(db: Session, deactivate_missing: bool = False) -> SyncResult:
        result = SyncResult()
        pops = db.query(PopSite).all()
        seen_ids: set[uuid.UUID] = set()
        for pop in pops:
            if pop.latitude is None or pop.longitude is None:
                result.skipped += 1
                continue
            seen_ids.add(pop.id)
            existing = (
                db.query(GeoLocation)
                .filter(GeoLocation.pop_site_id == pop.id)
                .first()
            )
            if existing:
                existing.name = pop.name
                existing.location_type = GeoLocationType.pop
                existing.latitude = pop.latitude
                existing.longitude = pop.longitude
                existing.is_active = pop.is_active
                result.updated += 1
            else:
                db.add(
                    GeoLocation(
                        name=pop.name,
                        location_type=GeoLocationType.pop,
                        latitude=pop.latitude,
                        longitude=pop.longitude,
                        pop_site_id=pop.id,
                        is_active=pop.is_active,
                    )
                )
                result.created += 1
        if deactivate_missing:
            missing_query = db.query(GeoLocation).filter(
                GeoLocation.pop_site_id.isnot(None)
            )
            if seen_ids:
                missing_query = missing_query.filter(
                    GeoLocation.pop_site_id.notin_(seen_ids)
                )
            missing_query.update({"is_active": False}, synchronize_session=False)
        db.commit()
        return result

    @staticmethod
    def sync_addresses(db: Session, deactivate_missing: bool = False) -> SyncResult:
        result = SyncResult()
        addresses = db.query(Address).all()
        seen_ids: set[uuid.UUID] = set()
        for address in addresses:
            if address.latitude is None or address.longitude is None:
                result.skipped += 1
                continue
            seen_ids.add(address.id)
            existing = (
                db.query(GeoLocation)
                .filter(GeoLocation.address_id == address.id)
                .first()
            )
            name = _address_display_name(address)
            if existing:
                existing.name = name
                existing.location_type = GeoLocationType.address
                existing.latitude = address.latitude
                existing.longitude = address.longitude
                existing.is_active = True
                result.updated += 1
            else:
                db.add(
                    GeoLocation(
                        name=name,
                        location_type=GeoLocationType.address,
                        latitude=address.latitude,
                        longitude=address.longitude,
                        address_id=address.id,
                        is_active=True,
                    )
                )
                result.created += 1
        if deactivate_missing:
            missing_query = db.query(GeoLocation).filter(
                GeoLocation.address_id.isnot(None)
            )
            if seen_ids:
                missing_query = missing_query.filter(
                    GeoLocation.address_id.notin_(seen_ids)
                )
            missing_query.update({"is_active": False}, synchronize_session=False)
        db.commit()
        return result


geo_sync = GeoSync()
