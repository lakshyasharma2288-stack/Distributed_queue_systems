import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from redis.exceptions import RedisError

from app.core.config import settings
from app.queue.redis_client import close_redis_client, get_redis_client
from app.queue.queue_manager import QueueManager, QueueMessage
from week4_safety import (
    DatabaseSafetyError,
    unavailable_integration,
    validate_test_redis_url,
)


@pytest.mark.integration
def test_real_redis_default_client_queue_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_url = os.environ.get("REDIS_TEST_URL")
    if not redis_url:
        unavailable_integration(
            "REDIS_TEST_URL is not set; Redis integration test unverified"
        )
    try:
        validate_test_redis_url(redis_url, settings.redis_url)
    except DatabaseSafetyError as exc:
        pytest.fail(str(exc))
    db_index = urlsplit(redis_url).path.strip("/")
    if db_index != "15":
        pytest.fail("REDIS_TEST_URL must use the dedicated Redis database index 15")

    close_redis_client()
    monkeypatch.setattr(settings, "redis_url", redis_url)
    client = get_redis_client()
    try:
        client.ping()
    except RedisError as exc:
        close_redis_client()
        unavailable_integration(
            f"REDIS_TEST_URL is unreachable ({type(exc).__name__}); "
            "Redis integration test unverified"
        )

    key = f"djq:test:{uuid4().hex}"
    queue = QueueManager(key=key)
    try:
        created_at = datetime(2026, 1, 1, 12, 0, 0, 100_000, tzinfo=timezone.utc)
        first = QueueMessage(job_id=uuid4(), task_type="first", priority=5)
        second = QueueMessage(job_id=uuid4(), task_type="second", priority=5)
        high_priority = QueueMessage(
            job_id=uuid4(),
            task_type="high",
            priority=8,
        )

        assert queue.enqueue(first, created_at)
        assert queue.enqueue(second, created_at + timedelta(microseconds=100))
        assert queue.enqueue(high_priority, created_at + timedelta(seconds=1))
        assert queue.peek(10) == [high_priority, first, second]
        assert queue.length() == 3
    finally:
        try:
            client.delete(key)
        finally:
            close_redis_client()
