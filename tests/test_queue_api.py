from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import fakeredis
import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError

from app.api.routes.queue import get_queue_manager
from app.core.config import settings
from app.main import app
from app.queue.queue_manager import QueueManager, QueueMessage


@pytest.fixture()
def fake_queue() -> tuple[fakeredis.FakeRedis, QueueManager]:
    redis_client = fakeredis.FakeRedis(decode_responses=True)
    return redis_client, QueueManager(client=redis_client, key="test:inspection")


@pytest.fixture()
def queue_client(
    fake_queue: tuple[fakeredis.FakeRedis, QueueManager],
) -> Iterator[TestClient]:
    _, manager = fake_queue
    app.dependency_overrides[get_queue_manager] = lambda: manager
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_queue_manager, None)


def test_inspection_returns_only_safe_fields_and_full_count(
    queue_client: TestClient,
    fake_queue: tuple[fakeredis.FakeRedis, QueueManager],
) -> None:
    _, queue = fake_queue
    high_priority = QueueMessage(job_id=uuid4(), task_type="send_email", priority=8)
    low_priority = QueueMessage(job_id=uuid4(), task_type="resize_image", priority=1)
    created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    queue.enqueue(low_priority, created_at)
    queue.enqueue(high_priority, created_at + timedelta(milliseconds=1))

    response = queue_client.get("/queue/jobs")

    assert response.status_code == 200
    assert response.json() == {
        "queue": [
            {
                "job_id": str(high_priority.job_id),
                "task_type": "send_email",
                "priority": 8,
            },
            {
                "job_id": str(low_priority.job_id),
                "task_type": "resize_image",
                "priority": 1,
            },
        ],
        "total_queued": 2,
        "limit": 50,
    }


def test_limit_truncates_items_but_preserves_full_queue_length(
    queue_client: TestClient,
    fake_queue: tuple[fakeredis.FakeRedis, QueueManager],
) -> None:
    _, queue = fake_queue
    created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for offset in range(5):
        queue.enqueue(
            QueueMessage(job_id=uuid4(), task_type=f"task_{offset}"),
            created_at + timedelta(milliseconds=offset),
        )

    response = queue_client.get("/queue/jobs?limit=2")

    assert response.status_code == 200
    assert len(response.json()["queue"]) == 2
    assert response.json()["total_queued"] == 5
    assert response.json()["limit"] == 2


@pytest.mark.parametrize("limit", [0, 201])
def test_invalid_limit_returns_422(queue_client: TestClient, limit: int) -> None:
    response = queue_client.get(f"/queue/jobs?limit={limit}")

    assert response.status_code == 422


def test_inspection_is_read_only_across_repeated_calls(
    queue_client: TestClient,
    fake_queue: tuple[fakeredis.FakeRedis, QueueManager],
) -> None:
    _, queue = fake_queue
    messages = [
        QueueMessage(job_id=uuid4(), task_type="send_email", priority=4),
        QueueMessage(job_id=uuid4(), task_type="generate_pdf", priority=2),
    ]
    created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for offset, message in enumerate(messages):
        queue.enqueue(message, created_at + timedelta(milliseconds=offset))
    before = queue.peek(10)
    before_length = queue.length()

    for _ in range(3):
        assert queue_client.get("/queue/jobs").status_code == 200

    assert queue.peek(10) == before
    assert queue.length() == before_length


def test_empty_queue_returns_zero_count(queue_client: TestClient) -> None:
    response = queue_client.get("/queue/jobs")

    assert response.status_code == 200
    assert response.json() == {"queue": [], "total_queued": 0, "limit": 50}


def test_corrupt_member_is_skipped_and_still_counted(
    queue_client: TestClient,
    fake_queue: tuple[fakeredis.FakeRedis, QueueManager],
) -> None:
    redis_client, queue = fake_queue
    message = QueueMessage(job_id=uuid4(), task_type="valid")
    queue.enqueue(message, datetime.now(timezone.utc))
    redis_client.zadd(queue.key, {"private-corrupt-content": 0})

    response = queue_client.get("/queue/jobs")

    assert response.status_code == 200
    assert len(response.json()["queue"]) == 1
    assert response.json()["total_queued"] == 2


def test_redis_failure_returns_safe_503_body(
    queue_client: TestClient,
    fake_queue: tuple[fakeredis.FakeRedis, QueueManager],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, queue = fake_queue
    monkeypatch.setattr(settings, "redis_url", "redis://user:secret@redis-host:6379/0")

    class UnavailableClient:
        def zrange(self, *_args, **_kwargs):
            raise ConnectionError(
                "failed redis://user:secret@redis-host:6379/0"
            )

    queue._client = UnavailableClient()  # type: ignore[assignment]

    response = queue_client.get("/queue/jobs")

    assert response.status_code == 503
    assert response.json() == {"detail": "Queue unavailable"}
    assert "secret" not in response.text
    assert "redis-host" not in response.text


def test_inspection_is_404_when_disabled(
    queue_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "enable_queue_inspection", False)

    assert queue_client.get("/queue/jobs").status_code == 404


def test_unexpected_error_remains_a_server_error() -> None:
    class BrokenQueue:
        def peek(self, _limit: int):
            raise RuntimeError("unexpected failure")

    app.dependency_overrides[get_queue_manager] = lambda: BrokenQueue()  # type: ignore[assignment]
    try:
        with TestClient(app, raise_server_exceptions=False) as test_client:
            response = test_client.get("/queue/jobs")
    finally:
        app.dependency_overrides.pop(get_queue_manager, None)

    assert response.status_code == 500
