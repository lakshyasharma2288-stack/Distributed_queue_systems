import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.core.config import settings
from app.queue.exceptions import QueueConfigurationError, QueueUnavailableError
from app.queue.queue_manager import QueueManager
from app.queue.redis_client import masked_redis_error

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/queue", tags=["queue"])


class QueueJobItem(BaseModel):
    job_id: uuid.UUID
    task_type: str
    priority: int


class QueueInspectionResponse(BaseModel):
    queue: list[QueueJobItem]
    total_queued: int
    limit: int


def get_queue_manager() -> QueueManager:
    return QueueManager()


@router.get("/jobs", response_model=QueueInspectionResponse)
def inspect_queue(
    limit: int = Query(default=50, ge=1, le=200),
    queue: QueueManager = Depends(get_queue_manager),
) -> QueueInspectionResponse:
    # Local-development only: this unauthenticated endpoint exposes queue contents.
    if not settings.enable_queue_inspection:
        raise HTTPException(status_code=404, detail="Not found")

    try:
        messages = queue.peek(limit)
        total_queued = queue.length()
    except (QueueUnavailableError, QueueConfigurationError) as exc:
        logger.error(
            "Queue inspection unavailable error=%s",
            masked_redis_error(exc),
        )
        raise HTTPException(
            status_code=503,
            detail="Queue unavailable",
        ) from exc

    return QueueInspectionResponse(
        queue=[
            QueueJobItem(
                job_id=message.job_id,
                task_type=message.task_type,
                priority=message.priority,
            )
            for message in messages
        ],
        total_queued=total_queued,
        limit=limit,
    )
