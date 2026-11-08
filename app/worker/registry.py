from collections.abc import Callable
from datetime import timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models.worker import WorkerRecord

WorkerSessionFactory = Callable[[], Session]
HeartbeatStatus = Literal["active", "draining"]
_LIVE_STATUSES = ("starting", "active", "draining")


def register_worker(
    session_factory: WorkerSessionFactory,
    *,
    worker_id: str,
    hostname: str,
    pid: int,
) -> None:
    with session_factory() as session:
        session.add(
            WorkerRecord(
                worker_id=worker_id,
                hostname=hostname,
                pid=pid,
                status="starting",
            )
        )
        session.commit()


def update_worker_heartbeat(
    session_factory: WorkerSessionFactory,
    *,
    worker_id: str,
    current_job_id: UUID | None,
    status: HeartbeatStatus,
    timeout_seconds: float,
) -> bool:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    with session_factory() as session:
        if session.get_bind().dialect.name == "sqlite":
            cutoff = func.datetime(
                func.now(),
                f"-{timeout_seconds:g} seconds",
            )
        else:
            cutoff = func.now() - timedelta(seconds=timeout_seconds)
        updated_worker_id = session.scalar(
            update(WorkerRecord)
            .where(
                WorkerRecord.worker_id == worker_id,
                WorkerRecord.status.in_(_LIVE_STATUSES),
                WorkerRecord.last_heartbeat_at >= cutoff,
            )
            .values(
                status=status,
                last_heartbeat_at=func.now(),
                current_job_id=current_job_id,
            )
            .returning(WorkerRecord.worker_id)
        )
        session.commit()
        return updated_worker_id is not None


def mark_stale_workers(
    session_factory: WorkerSessionFactory,
    *,
    timeout_seconds: float,
) -> list[str]:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    with session_factory() as session:
        database_now = session.scalar(select(func.now()))
        if database_now is None:
            raise RuntimeError("The database did not return its current time")
        cutoff = database_now - timedelta(seconds=timeout_seconds)
        stale_worker_ids = list(
            session.scalars(
                update(WorkerRecord)
                .where(
                    WorkerRecord.status.in_(_LIVE_STATUSES),
                    WorkerRecord.last_heartbeat_at < cutoff,
                )
                .values(
                    status="dead",
                    stopped_at=func.now(),
                )
                .returning(WorkerRecord.worker_id)
            )
        )
        session.commit()
        return stale_worker_ids


def stop_worker(session_factory: WorkerSessionFactory, *, worker_id: str) -> bool:
    with session_factory() as session:
        stopped_worker_id = session.scalar(
            update(WorkerRecord)
            .where(
                WorkerRecord.worker_id == worker_id,
                WorkerRecord.status.in_(_LIVE_STATUSES),
            )
            .values(
                status="stopped",
                stopped_at=func.now(),
                last_heartbeat_at=func.now(),
                current_job_id=None,
            )
            .returning(WorkerRecord.worker_id)
        )
        session.commit()
        return stopped_worker_id is not None
