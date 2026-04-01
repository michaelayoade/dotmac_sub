import logging
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.tr069 import (
    Tr069AcsServer,
    Tr069CpeDevice,
    Tr069Job,
    Tr069JobStatus,
    Tr069Parameter,
    Tr069Session,
)
from app.schemas.tr069 import (
    Tr069AcsServerCreate,
    Tr069AcsServerUpdate,
    Tr069CpeDeviceCreate,
    Tr069CpeDeviceUpdate,
    Tr069JobCreate,
    Tr069JobUpdate,
    Tr069ParameterCreate,
    Tr069ParameterUpdate,
    Tr069SessionCreate,
    Tr069SessionUpdate,
)
from app.services.common import (
    apply_ordering,
    apply_pagination,
    validate_enum,
)
from app.services.credential_crypto import encrypt_credential
from app.services.genieacs import GenieACSClient, GenieACSError, normalize_tr069_serial
from app.services.response import ListResponseMixin

_ACS_CREDENTIAL_FIELDS = ("cwmp_password", "connection_request_password")

logger = logging.getLogger(__name__)


def _normalized_serial_expr(column):  # type: ignore[no-untyped-def]
    """Build a SQL expression that strips common serial formatting."""
    expr = func.upper(column)
    for token in ("-", " ", ":", ".", "_", "/"):
        expr = func.replace(expr, token, "")
    return expr


class AcsServers(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: Tr069AcsServerCreate):
        data = payload.model_dump()
        for field in _ACS_CREDENTIAL_FIELDS:
            if data.get(field):
                data[field] = encrypt_credential(data[field])
        server = Tr069AcsServer(**data)
        db.add(server)
        db.commit()
        db.refresh(server)
        return server

    @staticmethod
    def get(db: Session, server_id: str):
        server = db.get(Tr069AcsServer, server_id)
        if not server:
            raise HTTPException(status_code=404, detail="ACS server not found")
        return server

    @staticmethod
    def list(
        db: Session,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(Tr069AcsServer)
        if is_active is None:
            query = query.filter(Tr069AcsServer.is_active.is_(True))
        else:
            query = query.filter(Tr069AcsServer.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": Tr069AcsServer.created_at, "name": Tr069AcsServer.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, server_id: str, payload: Tr069AcsServerUpdate):
        server = db.get(Tr069AcsServer, server_id)
        if not server:
            raise HTTPException(status_code=404, detail="ACS server not found")
        data = payload.model_dump(exclude_unset=True)
        for field in _ACS_CREDENTIAL_FIELDS:
            if field in data and data[field]:
                data[field] = encrypt_credential(data[field])
        for key, value in data.items():
            setattr(server, key, value)
        db.commit()
        db.refresh(server)
        return server

    @staticmethod
    def delete(db: Session, server_id: str):
        server = db.get(Tr069AcsServer, server_id)
        if not server:
            raise HTTPException(status_code=404, detail="ACS server not found")
        server.is_active = False
        db.commit()


class CpeDevices(ListResponseMixin):
    @staticmethod
    def _clip_text(value: object | None, max_len: int) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        return text[:max_len]

    @staticmethod
    def _extract_identity(
        client: GenieACSClient, device_data: dict
    ) -> tuple[str | None, str | None, str | None]:
        device_id = str(device_data.get("_id") or "").strip()
        parsed_oui: str | None = None
        parsed_product_class: str | None = None
        parsed_serial: str | None = None

        if device_id:
            try:
                parsed_oui, parsed_product_class, parsed_serial = (
                    client.parse_device_id(device_id)
                )
            except ValueError:
                logger.warning("Invalid device ID format: %s", device_id)

        raw_device_id = device_data.get("_deviceId")
        fallback_oui = fallback_product_class = fallback_serial = None
        if isinstance(raw_device_id, dict):
            fallback_oui = raw_device_id.get("_OUI") or raw_device_id.get("OUI")
            fallback_product_class = raw_device_id.get(
                "_ProductClass"
            ) or raw_device_id.get("ProductClass")
            fallback_serial = raw_device_id.get("_SerialNumber") or raw_device_id.get(
                "SerialNumber"
            )

        param_serial = client.extract_parameter_value(
            device_data, "Device.DeviceInfo.SerialNumber"
        ) or client.extract_parameter_value(
            device_data, "InternetGatewayDevice.DeviceInfo.SerialNumber"
        )
        param_product_class = client.extract_parameter_value(
            device_data, "Device.DeviceInfo.ProductClass"
        ) or client.extract_parameter_value(
            device_data, "InternetGatewayDevice.DeviceInfo.ProductClass"
        )

        # Prefer structured GenieACS identity fields over parsed `_id` parts.
        oui = CpeDevices._clip_text(fallback_oui, 8) or CpeDevices._clip_text(
            parsed_oui, 8
        )
        product_class = (
            CpeDevices._clip_text(param_product_class, 120)
            or CpeDevices._clip_text(fallback_product_class, 120)
            or CpeDevices._clip_text(parsed_product_class, 120)
        )
        serial_number = (
            CpeDevices._clip_text(param_serial, 120)
            or CpeDevices._clip_text(fallback_serial, 120)
            or CpeDevices._clip_text(parsed_serial, 120)
        )
        return oui, product_class, serial_number

    @staticmethod
    def create(db: Session, payload: Tr069CpeDeviceCreate):
        device = Tr069CpeDevice(**payload.model_dump())
        db.add(device)
        db.commit()
        db.refresh(device)
        return device

    @staticmethod
    def get(db: Session, device_id: str):
        device = db.get(Tr069CpeDevice, device_id)
        if not device:
            raise HTTPException(status_code=404, detail="TR-069 CPE device not found")
        return device

    @staticmethod
    def list(
        db: Session,
        acs_server_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(Tr069CpeDevice)
        if acs_server_id:
            query = query.filter(Tr069CpeDevice.acs_server_id == acs_server_id)
        if is_active is None:
            query = query.filter(Tr069CpeDevice.is_active.is_(True))
        else:
            query = query.filter(Tr069CpeDevice.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": Tr069CpeDevice.created_at,
                "serial_number": Tr069CpeDevice.serial_number,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, device_id: str, payload: Tr069CpeDeviceUpdate):
        device = db.get(Tr069CpeDevice, device_id)
        if not device:
            raise HTTPException(status_code=404, detail="TR-069 CPE device not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(device, key, value)
        db.commit()
        db.refresh(device)
        return device

    @staticmethod
    def delete(db: Session, device_id: str):
        device = db.get(Tr069CpeDevice, device_id)
        if not device:
            raise HTTPException(status_code=404, detail="TR-069 CPE device not found")
        device.is_active = False
        db.commit()

    @staticmethod
    def sync_from_genieacs(db: Session, acs_server_id: str) -> dict:
        """Sync devices from GenieACS to local database.

        Args:
            db: Database session
            acs_server_id: ACS server ID to sync from

        Returns:
            Dict with created and updated counts
        """
        server = db.get(Tr069AcsServer, acs_server_id)
        if not server:
            raise HTTPException(status_code=404, detail="ACS server not found")

        try:
            client = GenieACSClient(server.base_url)
            devices = client.list_devices()
        except GenieACSError as e:
            raise HTTPException(status_code=502, detail=f"GenieACS error: {e}")

        created, updated = 0, 0
        now = datetime.now(UTC)

        for device_data in devices:
            oui, product_class, serial_number = CpeDevices._extract_identity(
                client, device_data
            )
            if not serial_number:
                logger.warning(
                    "Skipping GenieACS device without serial number: %s",
                    device_data.get("_id"),
                )
                continue

            # Skip GenieACS discovery service probes — these are not real devices
            if oui == "DISCOVERYSERVICE" or product_class == "DISCOVERYSERVICE":
                continue

            # Extract connection request URL if available
            connection_url = client.extract_parameter_value(
                device_data, "Device.ManagementServer.ConnectionRequestURL"
            ) or client.extract_parameter_value(
                device_data,
                "InternetGatewayDevice.ManagementServer.ConnectionRequestURL",
            )
            connection_url = CpeDevices._clip_text(connection_url, 255)

            normalized_serial = normalize_tr069_serial(serial_number)

            # Look for existing device by exact serial number and ACS server first.
            existing = (
                db.query(Tr069CpeDevice)
                .filter(Tr069CpeDevice.acs_server_id == acs_server_id)
                .filter(Tr069CpeDevice.serial_number == serial_number)
                .first()
            )
            # Fallback: match using normalized serials to tolerate vendor formatting differences.
            if not existing and normalized_serial:
                existing = (
                    db.query(Tr069CpeDevice)
                    .filter(Tr069CpeDevice.acs_server_id == acs_server_id)
                    .filter(
                        _normalized_serial_expr(Tr069CpeDevice.serial_number)
                        == normalized_serial
                    )
                    .first()
                )
            # Fallback: update legacy/mis-parsed records by stable connection URL.
            if not existing and connection_url:
                existing = (
                    db.query(Tr069CpeDevice)
                    .filter(Tr069CpeDevice.acs_server_id == acs_server_id)
                    .filter(Tr069CpeDevice.connection_request_url == connection_url)
                    .first()
                )

            # Extract last inform time
            last_inform = device_data.get("_lastInform")
            last_inform_at = None
            if last_inform:
                try:
                    last_inform_at = datetime.fromisoformat(
                        last_inform.replace("Z", "+00:00")
                    )
                except (ValueError, AttributeError):
                    pass

            if existing:
                existing.oui = oui
                existing.product_class = product_class
                existing.connection_request_url = connection_url
                existing.last_inform_at = last_inform_at
                existing.is_active = True
                updated += 1
            else:
                new_device = Tr069CpeDevice(
                    acs_server_id=server.id,
                    serial_number=serial_number,
                    oui=oui,
                    product_class=product_class,
                    connection_request_url=connection_url,
                    last_inform_at=last_inform_at,
                    is_active=True,
                )
                db.add(new_device)
                created += 1

        db.commit()

        # Auto-link to ONTs by serial number
        auto_linked = 0
        explicit_links = 0
        serial_updated = 0
        try:
            from app.models.network import OntUnit

            unlinked_devices = (
                db.query(Tr069CpeDevice)
                .filter(
                    Tr069CpeDevice.acs_server_id == acs_server_id,
                    Tr069CpeDevice.is_active.is_(True),
                )
                .all()
            )
            for cpe_dev in unlinked_devices:
                if not cpe_dev.serial_number:
                    continue
                cpe_serial = str(cpe_dev.serial_number).strip()
                normalized_cpe_serial = normalize_tr069_serial(cpe_serial)

                # Strategy 1: Direct normalized serial match
                ont = (
                    db.query(OntUnit)
                    .filter(
                        _normalized_serial_expr(OntUnit.serial_number)
                        == normalized_cpe_serial
                    )
                    .filter(OntUnit.is_active.is_(True))
                    .first()
                )

                # Strategy 2: Decode Huawei hex serial to display format
                # e.g., 485754437D4733C3 → first 8 hex chars decode to ASCII
                # vendor prefix "HWTC" or "HWTT", used in some ONT serial fields
                if not ont and len(cpe_serial) == 16:
                    try:
                        vendor_ascii = bytes.fromhex(cpe_serial[:8]).decode("ascii")
                        display_serial = vendor_ascii + cpe_serial[8:]
                        normalized_display = normalize_tr069_serial(display_serial)
                        ont = (
                            db.query(OntUnit)
                            .filter(
                                _normalized_serial_expr(OntUnit.serial_number)
                                == normalized_display
                            )
                            .filter(OntUnit.is_active.is_(True))
                            .first()
                        )
                    except (ValueError, UnicodeDecodeError):
                        pass

                if ont:
                    if cpe_dev.ont_unit_id != ont.id:
                        cpe_dev.ont_unit_id = ont.id
                        explicit_links += 1
                    if not ont.tr069_acs_server_id:
                        ont.tr069_acs_server_id = server.id
                        auto_linked += 1
                    # Update synthetic serials with real GenieACS serial
                    current = str(ont.serial_number or "")
                    if current.startswith("HW-") and cpe_serial != current:
                        ont.serial_number = cpe_serial[:120]
                        serial_updated += 1

            if auto_linked or explicit_links or serial_updated:
                db.commit()
                logger.info(
                    "Auto-link: %d ONTs linked to ACS %s, %d explicit TR-069 links, %d serials updated",
                    auto_linked,
                    server.name,
                    explicit_links,
                    serial_updated,
                )
        except Exception as e:
            logger.warning("Auto-link ONTs after sync failed: %s", e)
            db.rollback()

        logger.info(
            "GenieACS sync: created=%d, updated=%d, auto_linked=%d",
            created,
            updated,
            auto_linked,
        )
        return {
            "created": created,
            "updated": updated,
            "total": len(devices),
            "auto_linked": auto_linked,
        }


class Sessions(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: Tr069SessionCreate):
        session = Tr069Session(**payload.model_dump())
        db.add(session)
        db.commit()
        db.refresh(session)
        return session

    @staticmethod
    def get(db: Session, session_id: str):
        session = db.get(Tr069Session, session_id)
        if not session:
            raise HTTPException(status_code=404, detail="TR-069 session not found")
        return session

    @staticmethod
    def list(
        db: Session,
        device_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(Tr069Session)
        if device_id:
            query = query.filter(Tr069Session.device_id == device_id)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": Tr069Session.created_at,
                "started_at": Tr069Session.started_at,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, session_id: str, payload: Tr069SessionUpdate):
        session = db.get(Tr069Session, session_id)
        if not session:
            raise HTTPException(status_code=404, detail="TR-069 session not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(session, key, value)
        db.commit()
        db.refresh(session)
        return session

    @staticmethod
    def delete(db: Session, session_id: str):
        session = db.get(Tr069Session, session_id)
        if not session:
            raise HTTPException(status_code=404, detail="TR-069 session not found")
        db.delete(session)
        db.commit()


class Parameters(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: Tr069ParameterCreate):
        parameter = Tr069Parameter(**payload.model_dump())
        db.add(parameter)
        db.commit()
        db.refresh(parameter)
        return parameter

    @staticmethod
    def get(db: Session, parameter_id: str):
        parameter = db.get(Tr069Parameter, parameter_id)
        if not parameter:
            raise HTTPException(status_code=404, detail="TR-069 parameter not found")
        return parameter

    @staticmethod
    def list(
        db: Session,
        device_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(Tr069Parameter)
        if device_id:
            query = query.filter(Tr069Parameter.device_id == device_id)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "name": Tr069Parameter.name,
                "updated_at": Tr069Parameter.updated_at,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, parameter_id: str, payload: Tr069ParameterUpdate):
        parameter = db.get(Tr069Parameter, parameter_id)
        if not parameter:
            raise HTTPException(status_code=404, detail="TR-069 parameter not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(parameter, key, value)
        db.commit()
        db.refresh(parameter)
        return parameter

    @staticmethod
    def delete(db: Session, parameter_id: str):
        parameter = db.get(Tr069Parameter, parameter_id)
        if not parameter:
            raise HTTPException(status_code=404, detail="TR-069 parameter not found")
        db.delete(parameter)
        db.commit()


class Jobs(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: Tr069JobCreate):
        job = Tr069Job(**payload.model_dump())
        db.add(job)
        db.commit()
        db.refresh(job)
        return job

    @staticmethod
    def get(db: Session, job_id: str):
        job = db.get(Tr069Job, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="TR-069 job not found")
        return job

    @staticmethod
    def list(
        db: Session,
        device_id: str | None,
        status: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(Tr069Job)
        if device_id:
            query = query.filter(Tr069Job.device_id == device_id)
        if status:
            query = query.filter(
                Tr069Job.status == validate_enum(status, Tr069JobStatus, "status")
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": Tr069Job.created_at, "status": Tr069Job.status},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, job_id: str, payload: Tr069JobUpdate):
        job = db.get(Tr069Job, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="TR-069 job not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(job, key, value)
        db.commit()
        db.refresh(job)
        return job

    @staticmethod
    def delete(db: Session, job_id: str):
        job = db.get(Tr069Job, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="TR-069 job not found")
        db.delete(job)
        db.commit()

    @staticmethod
    def execute(db: Session, job_id: str) -> Tr069Job:
        """Execute a job via GenieACS API.

        Args:
            db: Database session
            job_id: Job ID to execute

        Returns:
            Updated job object
        """
        job = db.get(Tr069Job, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        if job.status not in (Tr069JobStatus.queued, Tr069JobStatus.failed):
            raise HTTPException(
                status_code=400,
                detail=f"Job cannot be executed in {job.status.value} status",
            )

        device = db.get(Tr069CpeDevice, job.device_id)
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")

        server = db.get(Tr069AcsServer, device.acs_server_id)
        if not server:
            raise HTTPException(status_code=404, detail="ACS server not found")

        # Mark job as running
        job.status = Tr069JobStatus.running
        job.started_at = datetime.now(UTC)
        job.error = None
        db.commit()

        try:
            client = GenieACSClient(server.base_url)

            # Build GenieACS device ID
            genieacs_device_id = client.build_device_id(
                device.oui or "", device.product_class or "", device.serial_number or ""
            )

            # Build task based on command
            task = {"name": job.command}
            if job.payload:
                task.update(job.payload)

            # Execute task via GenieACS
            result = client.create_task(genieacs_device_id, task)

            job.status = Tr069JobStatus.succeeded
            logger.info(f"Job {job_id} executed successfully: {result}")

        except GenieACSError as e:
            job.status = Tr069JobStatus.failed
            job.error = str(e)
            logger.error(f"Job {job_id} failed: {e}")

        except Exception as e:
            job.status = Tr069JobStatus.failed
            job.error = str(e)
            logger.exception(f"Job {job_id} failed with unexpected error")

        job.completed_at = datetime.now(UTC)
        db.commit()
        db.refresh(job)
        return job

    @staticmethod
    def cancel(db: Session, job_id: str) -> Tr069Job:
        """Cancel a queued job.

        Args:
            db: Database session
            job_id: Job ID to cancel

        Returns:
            Updated job object
        """
        job = db.get(Tr069Job, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        if job.status != Tr069JobStatus.queued:
            raise HTTPException(
                status_code=400,
                detail=f"Only queued jobs can be canceled, current status: {job.status.value}",
            )

        job.status = Tr069JobStatus.canceled
        job.completed_at = datetime.now(UTC)
        db.commit()
        db.refresh(job)
        return job


def receive_inform(
    db: Session,
    *,
    serial_number: str | None,
    device_id_raw: str | None,
    event: str,
) -> dict:
    """Process a GenieACS inform webhook callback.

    Looks up the CPE device by serial number, updates its last_inform_at
    timestamp, and creates a session record.
    """
    from app.models.tr069 import Tr069Event

    serial = (serial_number or "").strip()
    device_id_str = (device_id_raw or "").strip()
    event_str = (event or "periodic").strip().lower()

    if not serial and device_id_str:
        parts = device_id_str.split("-", 2)
        if len(parts) == 3:
            serial = parts[2]

    if not serial:
        return {"status": "ignored", "reason": "no serial number"}

    from sqlalchemy import select

    device = db.scalars(
        select(Tr069CpeDevice)
        .where(
            Tr069CpeDevice.serial_number == serial,
            Tr069CpeDevice.is_active.is_(True),
        )
        .limit(1)
    ).first()

    if not device:
        logger.debug("Inform received for unknown serial: %s", serial)
        return {"status": "ignored", "reason": "unknown device"}

    now = datetime.now(UTC)
    device.last_inform_at = now

    event_map = {
        "boot": Tr069Event.boot,
        "bootstrap": Tr069Event.bootstrap,
        "periodic": Tr069Event.periodic,
        "value_change": Tr069Event.value_change,
        "connection_request": Tr069Event.connection_request,
        "transfer_complete": Tr069Event.transfer_complete,
        "diagnostics_complete": Tr069Event.diagnostics_complete,
    }
    event_type = event_map.get(event_str, Tr069Event.periodic)

    session = Tr069Session(
        device_id=device.id,
        event_type=event_type,
        started_at=now,
        ended_at=now,
        inform_payload={
            "serial_number": serial_number,
            "device_id": device_id_raw,
            "event": event,
        },
    )
    db.add(session)
    db.commit()

    logger.info(
        "Inform received: serial=%s event=%s device_id=%s",
        serial,
        event_str,
        device.id,
    )
    return {"status": "ok", "device_id": str(device.id), "event": event_str}


# -----------------------------------------------------------------------------
# ACS Enforcement Preset Management
# -----------------------------------------------------------------------------

PROVISION_NAME_PREFIX = "dotmac-enforce-acs"
PRESET_NAME_PREFIX = "dotmac-enforce-acs"


def _build_acs_provision_script(
    cwmp_url: str,
    cwmp_username: str | None = None,
    cwmp_password: str | None = None,
    periodic_inform_interval: int = 300,  # 5 minutes for timely NOC diagnostics
) -> str:
    """Build GenieACS provision script that enforces ACS URL on every inform.

    This provision uses GenieACS's declare() function to set the ManagementServer
    parameters. It handles both TR-181 (Device.*) and TR-098 (InternetGatewayDevice.*)
    data models.

    Args:
        cwmp_url: The ACS CWMP URL to enforce
        cwmp_username: Optional CWMP username
        cwmp_password: Optional CWMP password
        periodic_inform_interval: Inform interval in seconds (default 3600)

    Returns:
        JavaScript provision script
    """
    # Escape strings for JavaScript
    def js_string(s: str | None) -> str:
        if s is None:
            return "null"
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

    script_lines = [
        "// DotMac ACS URL Enforcement Provision",
        "// Automatically generated - do not edit manually",
        f"// Target ACS: {cwmp_url}",
        "",
        "const now = Date.now();",
        "",
        "// Detect data model by checking which root exists",
        'let root = "Device";',
        'try {',
        '  const dm = declare("Device.DeviceInfo.Manufacturer", {value: 1});',
        '  if (!dm.value || dm.value[0] === undefined) {',
        '    root = "InternetGatewayDevice";',
        "  }",
        "} catch (e) {",
        '  root = "InternetGatewayDevice";',
        "}",
        "",
        "// Set ManagementServer parameters",
        f'declare(root + ".ManagementServer.URL", {{value: now}}, {{value: {js_string(cwmp_url)}}});',
        f'declare(root + ".ManagementServer.PeriodicInformEnable", {{value: now}}, {{value: "true"}});',
        f'declare(root + ".ManagementServer.PeriodicInformInterval", {{value: now}}, {{value: "{periodic_inform_interval}"}});',
    ]

    if cwmp_username:
        script_lines.append(
            f'declare(root + ".ManagementServer.Username", {{value: now}}, {{value: {js_string(cwmp_username)}}});'
        )

    if cwmp_password:
        script_lines.append(
            f'declare(root + ".ManagementServer.Password", {{value: now}}, {{value: {js_string(cwmp_password)}}});'
        )

    return "\n".join(script_lines)


def _build_acs_preset(
    preset_id: str,
    provision_name: str,
    *,
    on_bootstrap: bool = True,
    on_boot: bool = True,
    on_periodic: bool = True,
    precondition: str = "",
    weight: int = 100,
) -> dict:
    """Build GenieACS preset definition.

    Args:
        preset_id: Unique preset ID
        provision_name: Name of the provision script to run
        on_bootstrap: Run on bootstrap event (device first contact)
        on_boot: Run on boot event
        on_periodic: Run on periodic inform
        precondition: Optional MongoDB-style filter to limit which devices
        weight: Preset priority (higher = runs later, default 100)

    Returns:
        Preset definition dict
    """
    events = {}
    if on_bootstrap:
        events["0 BOOTSTRAP"] = True
    if on_boot:
        events["1 BOOT"] = True
    if on_periodic:
        events["2 PERIODIC"] = True

    return {
        "_id": preset_id,
        "channel": "default",
        "weight": weight,
        "schedule": "",
        "events": events,
        "precondition": precondition,
        "configurations": [{"type": "provision", "name": provision_name}],
    }


def push_acs_enforcement_preset(
    db: Session,
    acs_server_id: str,
    *,
    on_bootstrap: bool = True,
    on_boot: bool = True,
    on_periodic: bool = True,
    precondition: str = "",
) -> dict:
    """Push ACS enforcement provision and preset to GenieACS.

    Creates a provision script that sets ManagementServer.URL to this ACS server's
    CWMP URL, and a preset that runs it on specified events. This ensures all
    devices will use this ACS regardless of any competing ACS configurations.

    Args:
        db: Database session
        acs_server_id: The ACS server to enforce
        on_bootstrap: Run on device bootstrap (first contact)
        on_boot: Run on device boot
        on_periodic: Run on periodic inform
        precondition: MongoDB-style filter to limit affected devices

    Returns:
        Dict with provision_id, preset_id, and status
    """
    from app.services.credential_crypto import decrypt_credential

    server = db.get(Tr069AcsServer, acs_server_id)
    if not server:
        raise HTTPException(status_code=404, detail="ACS server not found")

    if not server.cwmp_url:
        raise HTTPException(
            status_code=400, detail="ACS server has no CWMP URL configured"
        )

    if not server.base_url:
        raise HTTPException(
            status_code=400, detail="ACS server has no GenieACS base URL configured"
        )

    # Build unique IDs based on server
    server_slug = str(server.id).replace("-", "")[:12]
    provision_name = f"{PROVISION_NAME_PREFIX}-{server_slug}"
    preset_id = f"{PRESET_NAME_PREFIX}-{server_slug}"

    # Decrypt password if set
    cwmp_password = None
    if server.cwmp_password:
        cwmp_password = decrypt_credential(server.cwmp_password)

    # Build provision script using server's configured interval
    provision_script = _build_acs_provision_script(
        cwmp_url=server.cwmp_url,
        cwmp_username=server.cwmp_username,
        cwmp_password=cwmp_password,
        periodic_inform_interval=server.periodic_inform_interval or 300,
    )

    # Build preset
    preset = _build_acs_preset(
        preset_id=preset_id,
        provision_name=provision_name,
        on_bootstrap=on_bootstrap,
        on_boot=on_boot,
        on_periodic=on_periodic,
        precondition=precondition,
        weight=100,  # High weight to run after other presets
    )

    # Push to GenieACS
    client = GenieACSClient(server.base_url)

    try:
        # Create provision first
        client.create_provision(provision_name, provision_script)
        logger.info("Created ACS enforcement provision: %s", provision_name)
    except GenieACSError as exc:
        logger.error("Failed to create provision %s: %s", provision_name, exc)
        raise HTTPException(
            status_code=500, detail=f"Failed to create provision: {exc}"
        ) from exc

    try:
        # Create preset
        client.create_preset(preset)
        logger.info("Created ACS enforcement preset: %s", preset_id)
    except GenieACSError as exc:
        logger.error("Failed to create preset %s: %s", preset_id, exc)
        raise HTTPException(
            status_code=500, detail=f"Failed to create preset: {exc}"
        ) from exc

    return {
        "provision_id": provision_name,
        "preset_id": preset_id,
        "cwmp_url": server.cwmp_url,
        "events": {
            "bootstrap": on_bootstrap,
            "boot": on_boot,
            "periodic": on_periodic,
        },
        "status": "created",
    }


def remove_acs_enforcement_preset(db: Session, acs_server_id: str) -> dict:
    """Remove ACS enforcement provision and preset from GenieACS.

    Args:
        db: Database session
        acs_server_id: The ACS server whose enforcement to remove

    Returns:
        Dict with removal status
    """
    server = db.get(Tr069AcsServer, acs_server_id)
    if not server:
        raise HTTPException(status_code=404, detail="ACS server not found")

    if not server.base_url:
        raise HTTPException(
            status_code=400, detail="ACS server has no GenieACS base URL configured"
        )

    server_slug = str(server.id).replace("-", "")[:12]
    provision_name = f"{PROVISION_NAME_PREFIX}-{server_slug}"
    preset_id = f"{PRESET_NAME_PREFIX}-{server_slug}"

    client = GenieACSClient(server.base_url)
    removed = {"provision": False, "preset": False}

    try:
        client.delete_preset(preset_id)
        removed["preset"] = True
        logger.info("Removed ACS enforcement preset: %s", preset_id)
    except GenieACSError as exc:
        logger.warning("Failed to remove preset %s: %s", preset_id, exc)

    try:
        client.delete_provision(provision_name)
        removed["provision"] = True
        logger.info("Removed ACS enforcement provision: %s", provision_name)
    except GenieACSError as exc:
        logger.warning("Failed to remove provision %s: %s", provision_name, exc)

    return {
        "provision_id": provision_name,
        "preset_id": preset_id,
        "removed": removed,
        "status": "removed" if any(removed.values()) else "not_found",
    }


def get_acs_enforcement_status(db: Session, acs_server_id: str) -> dict:
    """Check if ACS enforcement preset exists in GenieACS.

    Args:
        db: Database session
        acs_server_id: The ACS server to check

    Returns:
        Dict with existence status and details
    """
    server = db.get(Tr069AcsServer, acs_server_id)
    if not server:
        raise HTTPException(status_code=404, detail="ACS server not found")

    if not server.base_url:
        return {
            "exists": False,
            "error": "ACS server has no GenieACS base URL configured",
        }

    server_slug = str(server.id).replace("-", "")[:12]
    provision_name = f"{PROVISION_NAME_PREFIX}-{server_slug}"
    preset_id = f"{PRESET_NAME_PREFIX}-{server_slug}"

    client = GenieACSClient(server.base_url)
    status = {
        "provision_id": provision_name,
        "preset_id": preset_id,
        "provision_exists": False,
        "preset_exists": False,
        "preset_details": None,
    }

    try:
        provisions = client.list_provisions()
        status["provision_exists"] = any(
            p.get("_id") == provision_name for p in provisions
        )
    except GenieACSError as exc:
        logger.warning("Failed to list provisions: %s", exc)

    try:
        presets = client.list_presets()
        for preset in presets:
            if preset.get("_id") == preset_id:
                status["preset_exists"] = True
                status["preset_details"] = {
                    "events": preset.get("events", {}),
                    "precondition": preset.get("precondition", ""),
                    "weight": preset.get("weight", 0),
                }
                break
    except GenieACSError as exc:
        logger.warning("Failed to list presets: %s", exc)

    status["exists"] = status["provision_exists"] and status["preset_exists"]
    return status


acs_servers = AcsServers()
cpe_devices = CpeDevices()
sessions = Sessions()
parameters = Parameters()
jobs = Jobs()


# -----------------------------------------------------------------------------
# Runtime Data Collection Provision
# -----------------------------------------------------------------------------

RUNTIME_PROVISION_NAME = "dotmac-runtime-collect"
RUNTIME_PRESET_NAME = "dotmac-runtime-collect"


def _build_runtime_collection_provision() -> str:
    """Build GenieACS provision script that collects runtime parameters.

    This provision uses GenieACS's declare() function with {value: 1} to
    request the device report these parameters. It handles both TR-181
    (Device.*) and TR-098 (InternetGatewayDevice.*) data models.

    Returns:
        JavaScript provision script
    """
    return '''// DotMac Runtime Data Collection Provision
// Collects operational parameters for dashboard display

const now = Date.now();

// Detect data model by checking which root exists
let root = "Device";
try {
  const dm = declare("Device.DeviceInfo.Manufacturer", {value: 1});
  if (!dm.value || dm.value[0] === undefined) {
    root = "InternetGatewayDevice";
  }
} catch (e) {
  root = "InternetGatewayDevice";
}

// System info
declare(root + ".DeviceInfo.SerialNumber", {value: 1});
declare(root + ".DeviceInfo.SoftwareVersion", {value: 1});
declare(root + ".DeviceInfo.UpTime", {value: 1});
declare(root + ".DeviceInfo.MemoryStatus.Total", {value: 1});
declare(root + ".DeviceInfo.MemoryStatus.Free", {value: 1});

// WAN connection status - PPPoE
declare(root + ".WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.ConnectionStatus", {value: 1});
declare(root + ".WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.ConnectionType", {value: 1});
declare(root + ".WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.ExternalIPAddress", {value: 1});
declare(root + ".WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.Username", {value: 1});
declare(root + ".WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.MACAddress", {value: 1});

// WAN connection status - DHCP/Static IP
declare(root + ".WANDevice.1.WANConnectionDevice.1.WANIPConnection.1.ConnectionStatus", {value: 1});
declare(root + ".WANDevice.1.WANConnectionDevice.1.WANIPConnection.1.ConnectionType", {value: 1});
declare(root + ".WANDevice.1.WANConnectionDevice.1.WANIPConnection.1.ExternalIPAddress", {value: 1});
declare(root + ".WANDevice.1.WANConnectionDevice.1.WANIPConnection.1.MACAddress", {value: 1});

// LAN configuration
declare(root + ".LANDevice.1.LANHostConfigManagement.DHCPServerEnable", {value: 1});
declare(root + ".LANDevice.1.LANHostConfigManagement.MinAddress", {value: 1});
declare(root + ".LANDevice.1.LANHostConfigManagement.MaxAddress", {value: 1});
declare(root + ".LANDevice.1.Hosts.HostNumberOfEntries", {value: 1});

// WiFi configuration and clients
declare(root + ".LANDevice.1.WLANConfiguration.1.Enable", {value: 1});
declare(root + ".LANDevice.1.WLANConfiguration.1.SSID", {value: 1});
declare(root + ".LANDevice.1.WLANConfiguration.1.Channel", {value: 1});
declare(root + ".LANDevice.1.WLANConfiguration.1.TotalAssociations", {value: 1});
declare(root + ".LANDevice.1.WLANConfiguration.1.Standard", {value: 1});
declare(root + ".LANDevice.1.WLANConfiguration.1.BeaconType", {value: 1});

// Ethernet port status
declare(root + ".LANDevice.1.LANEthernetInterfaceConfig.1.Status", {value: 1});
declare(root + ".LANDevice.1.LANEthernetInterfaceConfig.1.MACAddress", {value: 1});
declare(root + ".LANDevice.1.LANEthernetInterfaceConfig.2.Status", {value: 1});
declare(root + ".LANDevice.1.LANEthernetInterfaceConfig.3.Status", {value: 1});
declare(root + ".LANDevice.1.LANEthernetInterfaceConfig.4.Status", {value: 1});
'''


def _build_runtime_preset(
    *,
    on_bootstrap: bool = True,
    on_boot: bool = True,
    on_periodic: bool = True,
    weight: int = 50,
) -> dict:
    """Build GenieACS preset for runtime data collection.

    Args:
        on_bootstrap: Run on bootstrap event
        on_boot: Run on boot event
        on_periodic: Run on periodic inform
        weight: Preset priority (lower = runs earlier)

    Returns:
        Preset definition dict
    """
    events = {}
    if on_bootstrap:
        events["0 BOOTSTRAP"] = True
    if on_boot:
        events["1 BOOT"] = True
    if on_periodic:
        events["2 PERIODIC"] = True

    return {
        "_id": RUNTIME_PRESET_NAME,
        "channel": "default",
        "weight": weight,
        "schedule": "",
        "events": events,
        "precondition": "",
        "configurations": [{"type": "provision", "name": RUNTIME_PROVISION_NAME}],
    }


def push_runtime_collection_preset(
    db: Session,
    acs_server_id: str,
    *,
    on_bootstrap: bool = True,
    on_boot: bool = True,
    on_periodic: bool = True,
) -> dict:
    """Push runtime data collection provision and preset to GenieACS.

    Creates a provision that collects operational parameters (WiFi clients,
    WAN status, LAN mode, etc.) and a preset that runs it on specified events.

    Args:
        db: Database session
        acs_server_id: The ACS server to configure
        on_bootstrap: Run on device bootstrap
        on_boot: Run on device boot
        on_periodic: Run on periodic inform

    Returns:
        Dict with provision_id, preset_id, and status
    """
    server = db.get(Tr069AcsServer, acs_server_id)
    if not server:
        raise HTTPException(status_code=404, detail="ACS server not found")

    if not server.base_url:
        raise HTTPException(
            status_code=400, detail="ACS server has no GenieACS base URL configured"
        )

    provision_script = _build_runtime_collection_provision()
    preset = _build_runtime_preset(
        on_bootstrap=on_bootstrap,
        on_boot=on_boot,
        on_periodic=on_periodic,
    )

    client = GenieACSClient(server.base_url)

    try:
        client.create_provision(RUNTIME_PROVISION_NAME, provision_script)
        logger.info("Created runtime collection provision: %s", RUNTIME_PROVISION_NAME)
    except GenieACSError as exc:
        logger.error(
            "Failed to create provision %s: %s", RUNTIME_PROVISION_NAME, exc
        )
        raise HTTPException(
            status_code=500, detail=f"Failed to create provision: {exc}"
        ) from exc

    try:
        client.create_preset(preset)
        logger.info("Created runtime collection preset: %s", RUNTIME_PRESET_NAME)
    except GenieACSError as exc:
        logger.error("Failed to create preset %s: %s", RUNTIME_PRESET_NAME, exc)
        raise HTTPException(
            status_code=500, detail=f"Failed to create preset: {exc}"
        ) from exc

    return {
        "provision_id": RUNTIME_PROVISION_NAME,
        "preset_id": RUNTIME_PRESET_NAME,
        "events": {
            "bootstrap": on_bootstrap,
            "boot": on_boot,
            "periodic": on_periodic,
        },
        "status": "created",
    }


def get_runtime_collection_status(db: Session, acs_server_id: str) -> dict:
    """Check if runtime collection preset exists in GenieACS.

    Args:
        db: Database session
        acs_server_id: The ACS server to check

    Returns:
        Dict with existence status and details
    """
    server = db.get(Tr069AcsServer, acs_server_id)
    if not server:
        raise HTTPException(status_code=404, detail="ACS server not found")

    if not server.base_url:
        return {
            "exists": False,
            "error": "ACS server has no GenieACS base URL configured",
        }

    client = GenieACSClient(server.base_url)
    status = {
        "provision_id": RUNTIME_PROVISION_NAME,
        "preset_id": RUNTIME_PRESET_NAME,
        "provision_exists": False,
        "preset_exists": False,
    }

    try:
        provisions = client.list_provisions()
        status["provision_exists"] = any(
            p.get("_id") == RUNTIME_PROVISION_NAME for p in provisions
        )
    except GenieACSError as exc:
        logger.warning("Failed to list provisions: %s", exc)

    try:
        presets = client.list_presets()
        for preset in presets:
            if preset.get("_id") == RUNTIME_PRESET_NAME:
                status["preset_exists"] = True
                status["preset_details"] = {
                    "events": preset.get("events", {}),
                    "weight": preset.get("weight", 0),
                }
                break
    except GenieACSError as exc:
        logger.warning("Failed to list presets: %s", exc)

    status["exists"] = status["provision_exists"] and status["preset_exists"]
    return status
