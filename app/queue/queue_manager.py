import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import redis
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from redis.exceptions import RedisError

from app.core.config import settings
from app.queue.exceptions import QueueUnavailableError
from app.queue.redis_client import get_redis_client

logger = logging.getLogger(__name__)
PRIORITY_SCORE_RANGE = 10**13


class InvalidQueueMessageError(ValueError):
    pass


class QueueMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: UUID
    task_type: str = Field(min_length=1, max_length=100)
    priority: int = Field(default=0, ge=0, le=9, strict=True)

    @field_validator("task_type")
    @classmethod
    def reject_blank_task_type(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("task_type must not be blank")
        return value


class QueueManager:
    def __init__(
        self,
        client: redis.Redis | None = None,
        key: str | None = None,
    ) -> None:
        self._client = client
        self.key = key or settings.queue_key

    @staticmethod
    def serialize(message: QueueMessage) -> str:
        return json.dumps(
            message.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def parse(member: str) -> QueueMessage:
        try:
            return QueueMessage.model_validate_json(member)
        except (ValidationError, ValueError, TypeError) as exc:
            raise InvalidQueueMessageError("Queue member is invalid") from exc

    @staticmethod
    def _score(message: QueueMessage, created_at: datetime) -> float:
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        created_at_epoch_ms = created_at.timestamp() * 1000
        return (9 - message.priority) * PRIORITY_SCORE_RANGE + created_at_epoch_ms

    def _get_client(self) -> redis.Redis:
        return self._client if self._client is not None else get_redis_client()

    def enqueue(self, message: QueueMessage, created_at: datetime) -> bool:
        member = self.serialize(message)
        score = self._score(message, created_at)
        try:
            added = self._get_client().zadd(self.key, {member: score}, nx=True)
        except (RedisError, OSError) as exc:
            raise QueueUnavailableError("Redis queue operation failed") from exc
        return bool(added)

    def pop_job_id(self) -> UUID | None:
        while True:
            try:
                popped = self._get_client().zpopmin(self.key, 1)
            except (RedisError, OSError) as exc:
                raise QueueUnavailableError("Redis queue operation failed") from exc

            if not popped:
                return None

            member = popped[0][0]
            if isinstance(member, bytes):
                try:
                    member = member.decode("utf-8")
                except UnicodeDecodeError:
                    logger.warning(
                        "Skipping invalid queue member job_id=unknown member_length=%d",
                        len(member),
                    )
                    continue
            if not isinstance(member, str):
                logger.warning(
                    "Skipping invalid queue member job_id=unknown member_length=0"
                )
                continue

            try:
                return self.parse(member).job_id
            except InvalidQueueMessageError:
                logger.warning(
                    "Skipping invalid queue member job_id=%s member_length=%d",
                    self._safe_job_id(member),
                    len(member),
                )

    def peek(self, limit: int) -> list[QueueMessage]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        try:
            members = self._get_client().zrange(self.key, 0, limit - 1)
        except (RedisError, OSError) as exc:
            raise QueueUnavailableError("Redis queue operation failed") from exc

        messages: list[QueueMessage] = []
        for member in members:
            try:
                messages.append(self.parse(member))
            except InvalidQueueMessageError:
                logger.warning(
                    "Skipping invalid queue member job_id=%s member_length=%d",
                    self._safe_job_id(member),
                    len(member),
                )
        return messages

    @staticmethod
    def _safe_job_id(member: str) -> str:
        try:
            data: Any = json.loads(member)
            if not isinstance(data, dict):
                return "unknown"
            return str(UUID(str(data.get("job_id", ""))))
        except (ValueError, TypeError, json.JSONDecodeError):
            return "unknown"

    def length(self) -> int:
        try:
            return int(self._get_client().zcard(self.key))
        except (RedisError, OSError) as exc:
            raise QueueUnavailableError("Redis queue operation failed") from exc
