import logging

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.job import Job
from app.queue.exceptions import QueueConfigurationError, QueueUnavailableError
from app.queue.queue_manager import QueueManager, QueueMessage
from app.queue.redis_client import masked_redis_error
from app.worker.state_machine import transition_job

logger = logging.getLogger(__name__)


def publish_due_retries(
    session: Session,
    queue: QueueManager,
    batch_size: int,
) -> dict[str, int]:
    if batch_size < 1:
        raise ValueError("batch_size must be positive")

    published = 0
    for _ in range(batch_size):
        try:
            job = session.scalar(
                select(Job)
                .where(
                    Job.status == "retry_scheduled",
                    Job.next_retry_at <= func.now(),
                )
                .order_by(Job.next_retry_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        except SQLAlchemyError as exc:
            session.rollback()
            logger.error(
                "Retry scheduler query failed error_type=%s",
                type(exc).__name__,
            )
            raise

        if job is None:
            break

        transition = transition_job(
            job.id,
            expected_status="retry_scheduled",
            new_status="pending",
            values={"next_retry_at": None},
        ).returning(Job.id)
        try:
            transitioned_id = session.scalar(transition)
            if transitioned_id is None:
                session.rollback()
                continue
            queue.enqueue(
                QueueMessage(
                    job_id=job.id,
                    task_type=job.task_type,
                    priority=job.priority,
                ),
                job.created_at,
            )
            session.commit()
        except (QueueUnavailableError, QueueConfigurationError) as exc:
            session.rollback()
            logger.error(
                "Retry publication paused error=%s",
                masked_redis_error(exc),
            )
            break
        except SQLAlchemyError as exc:
            session.rollback()
            logger.error(
                "Retry publication transaction failed error_type=%s",
                type(exc).__name__,
            )
            raise

        published += 1
        logger.info(
            "Retry scheduled job published job_id=%s attempt=%s",
            job.id,
            job.attempts,
        )

    try:
        still_due = session.scalar(
            select(func.count())
            .select_from(Job)
            .where(
                Job.status == "retry_scheduled",
                Job.next_retry_at <= func.now(),
            )
        )
    except SQLAlchemyError as exc:
        session.rollback()
        logger.error(
            "Retry scheduler count failed error_type=%s",
            type(exc).__name__,
        )
        raise

    return {
        "published": published,
        "still_due": int(still_due or 0),
    }
