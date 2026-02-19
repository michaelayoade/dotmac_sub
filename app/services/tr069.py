import logging
from datetime import UTC, datetime

from fastapi import HTTPException
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
from app.services.genieacs import GenieACSClient, GenieACSError
from app.services.response import ListResponseMixin

logger = logging.getLogger(__name__)


class AcsServers(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: Tr069AcsServerCreate):
        server = Tr069AcsServer(**payload.model_dump())
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
        for key, value in payload.model_dump(exclude_unset=True).items():
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
            {"created_at": Tr069CpeDevice.created_at, "serial_number": Tr069CpeDevice.serial_number},
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
            device_id = device_data.get("_id")
            if not device_id:
                continue

            try:
                oui, product_class, serial_number = client.parse_device_id(device_id)
            except ValueError:
                logger.warning(f"Invalid device ID format: {device_id}")
                continue

            # Look for existing device by serial number and ACS server
            existing = (
                db.query(Tr069CpeDevice)
                .filter(Tr069CpeDevice.acs_server_id == acs_server_id)
                .filter(Tr069CpeDevice.serial_number == serial_number)
                .first()
            )

            # Extract connection request URL if available
            connection_url = client.extract_parameter_value(
                device_data, "Device.ManagementServer.ConnectionRequestURL"
            ) or client.extract_parameter_value(
                device_data, "InternetGatewayDevice.ManagementServer.ConnectionRequestURL"
            )

            # Extract last inform time
            last_inform = device_data.get("_lastInform")
            last_inform_at = None
            if last_inform:
                try:
                    last_inform_at = datetime.fromisoformat(last_inform.replace("Z", "+00:00"))
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
        logger.info(f"GenieACS sync: created={created}, updated={updated}")
        return {"created": created, "updated": updated, "total": len(devices)}


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
            {"created_at": Tr069Session.created_at, "started_at": Tr069Session.started_at},
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
            {"updated_at": Tr069Parameter.updated_at, "name": Tr069Parameter.name},
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
                detail=f"Job cannot be executed in {job.status.value} status"
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
                device.oui or "",
                device.product_class or "",
                device.serial_number or ""
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
                detail=f"Only queued jobs can be canceled, current status: {job.status.value}"
            )

        job.status = Tr069JobStatus.canceled
        job.completed_at = datetime.now(UTC)
        db.commit()
        db.refresh(job)
        return job


acs_servers = AcsServers()
cpe_devices = CpeDevices()
sessions = Sessions()
parameters = Parameters()
jobs = Jobs()
