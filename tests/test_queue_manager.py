import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import fakeredis
import pytest
from pydantic import ValidationError
from redis.exceptions import ConnectionError

from app.queue.exceptions import QueueUnavailableError
from app.queue.queue_manager import InvalidQueueMessageError, QueueManager, QueueMessage


@pytest.fixture()
def fake_queue() -> tuple[fakeredis.FakeRedis, QueueManager]:
    client = fakeredis.FakeRedis(decode_responses=True)
    return client, QueueManager(client=client, key="test:queue")


def test_queue_message_serialization_is_canonical_and_round_trips() -> None:
    message = QueueMessage(
        job_id=uuid4(),
        task_type="send_email",
        priority=5,
    )

    serialized = QueueManager.serialize(message)

    assert serialized == json.dumps(
        {"job_id": str(message.job_id), "task_type": "send_email", "priority": 5},
        sort_keys=True,
        separators=(",", ":"),
    )
    assert QueueManager.parse(serialized) == message


def test_enqueue_peek_and_length(fake_queue: tuple[fakeredis.FakeRedis, QueueManager]) -> None:
    _, queue = fake_queue
    message = QueueMessage(job_id=uuid4(), task_type="send_email")

    assert queue.enqueue(message, datetime.now(timezone.utc)) is True
    assert queue.length() == 1
    assert queue.peek(50) == [message]


def test_pop_job_id_removes_highest_priority_member_once(
    fake_queue: tuple[fakeredis.FakeRedis, QueueManager],
) -> None:
    client, queue = fake_queue
    created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    normal = QueueMessage(job_id=uuid4(), task_type="normal", priority=2)
    urgent = QueueMessage(job_id=uuid4(), task_type="urgent", priority=9)
    queue.enqueue(normal, created_at)
    queue.enqueue(urgent, created_at + timedelta(milliseconds=1))

    assert queue.pop_job_id() == urgent.job_id
    assert client.zscore(queue.key, queue.serialize(urgent)) is None
    assert queue.pop_job_id() == normal.job_id
    assert queue.pop_job_id() is None


def test_pop_job_id_preserves_fifo_for_distinct_scores(
    fake_queue: tuple[fakeredis.FakeRedis, QueueManager],
) -> None:
    _, queue = fake_queue
    first = QueueMessage(job_id=uuid4(), task_type="first", priority=3)
    second = QueueMessage(job_id=uuid4(), task_type="second", priority=3)
    created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    queue.enqueue(first, created_at)
    queue.enqueue(second, created_at + timedelta(microseconds=100))

    assert queue.pop_job_id() == first.job_id
    assert queue.pop_job_id() == second.job_id


def test_pop_job_id_discards_invalid_member_without_logging_contents(
    fake_queue: tuple[fakeredis.FakeRedis, QueueManager],
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, queue = fake_queue
    valid = QueueMessage(job_id=uuid4(), task_type="valid")
    client.zadd(queue.key, {"not-json-secret": 0})
    queue.enqueue(valid, datetime.now(timezone.utc))

    assert queue.pop_job_id() == valid.job_id
    assert "not-json-secret" not in caplog.text


def test_higher_priority_is_served_first(fake_queue: tuple[fakeredis.FakeRedis, QueueManager]) -> None:
    _, queue = fake_queue
    created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    low_priority = QueueMessage(job_id=uuid4(), task_type="low", priority=1)
    high_priority = QueueMessage(job_id=uuid4(), task_type="high", priority=8)

    queue.enqueue(low_priority, created_at)
    queue.enqueue(high_priority, created_at + timedelta(milliseconds=1))

    assert queue.peek(2) == [high_priority, low_priority]


def test_equal_priority_uses_created_at_fifo_order(
    fake_queue: tuple[fakeredis.FakeRedis, QueueManager],
) -> None:
    _, queue = fake_queue
    first = QueueMessage(job_id=uuid4(), task_type="first", priority=3)
    second = QueueMessage(job_id=uuid4(), task_type="second", priority=3)
    created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)

    queue.enqueue(first, created_at)
    queue.enqueue(second, created_at + timedelta(milliseconds=1))

    assert queue.peek(2) == [first, second]


def test_equal_priority_preserves_submillisecond_order(
    fake_queue: tuple[fakeredis.FakeRedis, QueueManager],
) -> None:
    _, queue = fake_queue
    first = QueueMessage(job_id=uuid4(), task_type="first", priority=3)
    second = QueueMessage(job_id=uuid4(), task_type="second", priority=3)
    created_at = datetime(2026, 1, 1, 0, 0, 0, 100_000, tzinfo=timezone.utc)

    queue.enqueue(first, created_at)
    queue.enqueue(second, created_at + timedelta(microseconds=100))

    assert queue.peek(2) == [first, second]


def test_duplicate_enqueue_does_not_change_existing_score(
    fake_queue: tuple[fakeredis.FakeRedis, QueueManager],
) -> None:
    client, queue = fake_queue
    message = QueueMessage(job_id=uuid4(), task_type="send_email", priority=2)
    first_time = datetime(2026, 1, 1, tzinfo=timezone.utc)

    assert queue.enqueue(message, first_time) is True
    original_score = client.zscore(queue.key, queue.serialize(message))
    assert queue.enqueue(message, first_time + timedelta(days=1)) is False

    assert client.zscore(queue.key, queue.serialize(message)) == original_score
    assert queue.length() == 1


def test_invalid_message_is_rejected() -> None:
    with pytest.raises(ValidationError):
        QueueMessage(job_id=uuid4(), task_type=" ", priority=0)
    with pytest.raises(ValidationError):
        QueueMessage(job_id=uuid4(), task_type="send_email", priority=10)
    with pytest.raises(InvalidQueueMessageError):
        QueueManager.parse('{"job_id":"not-a-uuid","task_type":"send_email"}')


def test_corrupt_member_is_skipped_but_still_counted(
    fake_queue: tuple[fakeredis.FakeRedis, QueueManager],
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, queue = fake_queue
    message = QueueMessage(job_id=uuid4(), task_type="valid")
    queue.enqueue(message, datetime.now(timezone.utc))
    client.zadd(queue.key, {"not-json-secret": 0})

    assert queue.peek(10) == [message]
    assert queue.length() == 2
    assert "member_length=" in caplog.text
    assert "not-json-secret" not in caplog.text


def test_peek_rejects_out_of_range_limit(
    fake_queue: tuple[fakeredis.FakeRedis, QueueManager],
) -> None:
    _, queue = fake_queue

    with pytest.raises(ValueError):
        queue.peek(0)
    with pytest.raises(ValueError):
        queue.peek(201)


def test_redis_failure_raises_queue_unavailable() -> None:
    class UnavailableClient:
        def zadd(self, *_args, **_kwargs) -> int:
            raise ConnectionError("redis://user:secret@localhost")

    queue = QueueManager(client=UnavailableClient(), key="test:queue")

    with pytest.raises(QueueUnavailableError) as exc_info:
        queue.enqueue(
            QueueMessage(job_id=uuid4(), task_type="send_email"),
            datetime.now(timezone.utc),
        )
    assert "secret" not in str(exc_info.value)


def test_pop_job_id_wraps_redis_connection_failure() -> None:
    class UnavailableClient:
        def zpopmin(self, *_args, **_kwargs):
            raise ConnectionError("redis is unavailable")

    queue = QueueManager(client=UnavailableClient(), key="test:queue")

    with pytest.raises(QueueUnavailableError) as exc_info:
        queue.pop_job_id()

    assert "redis is unavailable" not in str(exc_info.value)
