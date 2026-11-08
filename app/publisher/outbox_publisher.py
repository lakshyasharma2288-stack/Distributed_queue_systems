import logging
from datetime import datetime, timezone
from threading import Event

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.outbox_event import OutboxEvent
from app.queue.exceptions import QueueConfigurationError, QueueUnavailableError
from app.queue.queue_manager import InvalidQueueMessageError, QueueManager, QueueMessage
from app.queue.redis_client import masked_redis_error

logger = logging.getLogger(__name__)


def _message_from_event(event: OutboxEvent) -> QueueMessage:
    if event.event_type != "job_created":
        raise InvalidQueueMessageError("Unsupported outbox event type")

    try:
        message = QueueMessage.model_validate(event.payload)
    except ValidationError as exc:
        raise InvalidQueueMessageError("Invalid job_created event payload") from exc

    if message.job_id != event.job_id:
        raise InvalidQueueMessageError("Outbox payload job_id does not match the event")
    return message


def _commit_event(session: Session) -> None:
    try:
        session.commit()
    except SQLAlchemyError as exc:
        session.rollback()
        logger.error(
            "Outbox database commit failed error_type=%s",
            type(exc).__name__,
        )
        raise


def publish_pending_events(
    session: Session,
    queue: QueueManager,
    batch_size: int,
    *,
    stop_event: Event | None = None,
) -> dict[str, int]:
    if batch_size < 1:
        raise ValueError("batch_size must be positive")

    fetched = 0
    published = 0
    failed = 0

    for _ in range(batch_size):
        if stop_event is not None and stop_event.is_set():
            break
        try:
            event = session.scalar(
                select(OutboxEvent)
                .where(OutboxEvent.status == "pending")
                .order_by(OutboxEvent.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        except SQLAlchemyError as exc:
            session.rollback()
            logger.error(
                "Outbox event fetch failed error_type=%s",
                type(exc).__name__,
            )
            raise

        if event is None:
            break
        fetched += 1

        try:
            message = _message_from_event(event)
        except InvalidQueueMessageError as exc:
            event.status = "failed"
            event.attempt_count += 1
            event.last_error = str(exc)[:500]
            _commit_event(session)
            failed += 1
            logger.warning(
                "Outbox event marked failed event_id=%s error=%s",
                event.id,
                str(exc),
            )
            continue

        try:
            queue.enqueue(message, event.created_at)
        except QueueUnavailableError as exc:
            event.attempt_count += 1
            event.last_error = "Redis unavailable"
            _commit_event(session)
            logger.error(
                "Outbox publication paused event_id=%s error=%s",
                event.id,
                masked_redis_error(exc),
            )
            break
        except QueueConfigurationError as exc:
            event.attempt_count += 1
            event.last_error = "Redis configuration is invalid"
            _commit_event(session)
            logger.error(
                "Outbox publication paused event_id=%s error=%s",
                event.id,
                masked_redis_error(exc),
            )
            break

        event.status = "published"
        event.published_at = datetime.now(timezone.utc)
        event.attempt_count += 1
        event.last_error = None
        _commit_event(session)
        published += 1
        logger.info("Outbox event published event_id=%s", event.id)

    try:
        pending_count = session.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.status == "pending")
        )
    except SQLAlchemyError as exc:
        session.rollback()
        logger.error(
            "Outbox pending count failed error_type=%s",
            type(exc).__name__,
        )
        raise

    return {
        "fetched": fetched,
        "published": published,
        "failed": failed,
        "still_pending": int(pending_count or 0),
    }
