from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models.job import Job

LeaseSessionFactory = Callable[[], Session]


@dataclass(frozen=True)
class JobLease:
    job_id: UUID
    worker_id: str
    lease_version: int
    attempt_number: int


def renew_job_lease(
    session_factory: LeaseSessionFactory,
    lease: JobLease,
    *,
    duration_seconds: float,
) -> bool:
    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be positive")

    with session_factory() as session:
        if session.get_bind().dialect.name == "postgresql":
            database_now = func.clock_timestamp()
            lease_expiry = database_now + timedelta(seconds=duration_seconds)
        else:
            database_time = session.scalar(select(func.now()))
            if database_time is None:
                raise RuntimeError("The database did not return its current time")
            database_now = database_time
            lease_expiry = database_time + timedelta(seconds=duration_seconds)
        renewed_job_id = session.scalar(
            update(Job)
            .where(
                Job.id == lease.job_id,
                Job.status == "processing",
                Job.lease_owner_id == lease.worker_id,
                Job.lease_version == lease.lease_version,
                Job.lease_expires_at > database_now,
            )
            .values(
                lease_expires_at=lease_expiry,
            )
            .returning(Job.id)
        )
        session.commit()
        return renewed_job_id is not None
