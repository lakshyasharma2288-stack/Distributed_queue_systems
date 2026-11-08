import logging
from collections.abc import Callable
from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.database import SessionLocal
from app.models.attempt import JobAttempt
from app.models.dead_letter import DeadLetter
from app.models.job import Job
from app.models.worker import WorkerRecord
from app.queue.exceptions import QueueConfigurationError, QueueUnavailableError
from app.queue.queue_manager import QueueManager, QueueMessage
from app.queue.redis_client import masked_redis_error
from app.worker.retry_policy import compute_retry_delay_seconds
from app.worker.state_machine import transition_job

logger = logging.getLogger(__name__)
SessionFactory = Callable[[], Session]
_LIVE_WORKER_STATUSES = ("starting", "active", "draining")


def recover_interrupted_jobs(
    batch_size: int,
    *,
    session_factory: SessionFactory = SessionLocal,
) -> dict[str, int]:
    if batch_size < 1:
        raise ValueError("batch_size must be positive")

    recovered = 0
    retried = 0
    dead_lettered = 0
    live_owner = (
        select(WorkerRecord.worker_id)
        .where(
            WorkerRecord.worker_id == Job.lease_owner_id,
            WorkerRecord.status.in_(_LIVE_WORKER_STATUSES),
        )
        .exists()
    )

    for _ in range(batch_size):
        try:
            with session_factory() as session:
                database_now = session.scalar(select(func.now()))
                if database_now is None:
                    raise RuntimeError("The database did not return its current time")
                job = session.scalar(
                    select(Job)
                    .where(
                        Job.status == "processing",
                        or_(
                            Job.lease_expires_at.is_(None),
                            Job.lease_expires_at <= database_now,
                            ~live_owner,
                        ),
                    )
                    .order_by(Job.started_at, Job.created_at)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                if job is None:
                    break

                attempt_number = max(job.attempts, 1)
                if job.attempts < attempt_number:
                    job.attempts = attempt_number
                retryable = attempt_number < job.max_attempts
                if retryable:
                    new_status = "retry_scheduled"
                    delay = compute_retry_delay_seconds(
                        attempt_number,
                        base_delay_seconds=settings.retry_base_delay_seconds,
                        max_delay_seconds=settings.retry_max_delay_seconds,
                        jitter_ratio=settings.retry_jitter_ratio,
                    )
                    next_retry_at = database_now + timedelta(seconds=delay)
                    completed_at = None
                else:
                    new_status = "failed"
                    next_retry_at = None
                    completed_at = database_now

                error_code = "WORKER_LOST"
                error_message = (
                    "Worker lease expired or the worker heartbeat became stale."
                )
                owner_condition = (
                    Job.lease_owner_id.is_(None)
                    if job.lease_owner_id is None
                    else Job.lease_owner_id == job.lease_owner_id
                )
                transition = transition_job(
                    job.id,
                    expected_status="processing",
                    new_status=new_status,
                    conditions=(
                        Job.lease_version == job.lease_version,
                        owner_condition,
                    ),
                    values={
                        "attempts": attempt_number,
                        "completed_at": completed_at,
                        "error_message": error_message,
                        "result": None,
                        "next_retry_at": next_retry_at,
                        "lease_owner_id": None,
                        "lease_expires_at": None,
                    },
                )
                transitioned_id = session.scalar(transition.returning(Job.id))
                if transitioned_id is None:
                    session.rollback()
                    continue

                attempt = session.get(JobAttempt, (job.id, attempt_number))
                if attempt is None:
                    session.add(
                        JobAttempt(
                            job_id=job.id,
                            attempt_number=attempt_number,
                            worker_id=job.lease_owner_id or "legacy-unknown",
                            started_at=job.started_at or job.created_at,
                            ended_at=database_now,
                            outcome=(
                                "retry_scheduled" if retryable else "dead"
                            ),
                            error_code=error_code,
                            error_message=error_message,
                        )
                    )
                elif attempt.outcome == "running":
                    attempt.ended_at = database_now
                    attempt.outcome = "retry_scheduled" if retryable else "dead"
                    attempt.error_code = error_code
                    attempt.error_message = error_message
                else:
                    raise RuntimeError(
                        "Processing job attempt is not in the running state"
                    )

                if not retryable:
                    session.add(
                        DeadLetter(
                            job_id=job.id,
                            final_error_code=error_code,
                            final_error_message=error_message,
                        )
                    )
                session.commit()
                recovered += 1
                if retryable:
                    retried += 1
                else:
                    dead_lettered += 1
                logger.warning(
                    "Recovered interrupted job job_id=%s attempt=%s state=%s",
                    job.id,
                    attempt_number,
                    new_status,
                )
        except SQLAlchemyError as exc:
            logger.error(
                "Job recovery failed error_type=%s",
                type(exc).__name__,
            )
            raise

    return {
        "recovered": recovered,
        "retried": retried,
        "dead_lettered": dead_lettered,
    }


def reconcile_pending_jobs(
    session: Session,
    queue: QueueManager,
    batch_size: int,
) -> dict[str, int]:
    if batch_size < 1:
        raise ValueError("batch_size must be positive")

    enqueued = 0
    processed_ids: list[UUID] = []
    for _ in range(batch_size):
        try:
            job = session.scalar(
                select(Job)
                .where(
                    Job.status == "pending",
                    Job.id.not_in(processed_ids),
                )
                .order_by(Job.priority.desc(), Job.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if job is None:
                break
            added = queue.enqueue(
                QueueMessage(
                    job_id=job.id,
                    task_type=job.task_type,
                    priority=job.priority,
                ),
                job.created_at,
            )
            processed_ids.append(job.id)
            session.commit()
            enqueued += int(added)
        except (QueueUnavailableError, QueueConfigurationError) as exc:
            session.rollback()
            logger.error(
                "Pending job reconciliation paused error=%s",
                masked_redis_error(exc),
            )
            break
        except SQLAlchemyError as exc:
            session.rollback()
            logger.error(
                "Pending job reconciliation failed error_type=%s",
                type(exc).__name__,
            )
            raise

    return {"enqueued": enqueued}
