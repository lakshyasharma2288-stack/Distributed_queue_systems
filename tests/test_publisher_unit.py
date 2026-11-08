from datetime import datetime, timedelta, timezone
from threading import Event
from uuid import uuid4

import pytest
from redis.exceptions import ConnectionError

from app.core.config import settings
from app.models.outbox_event import OutboxEvent
from app.publisher.outbox_publisher import publish_pending_events
from app.queue.exceptions import QueueUnavailableError
from app.queue.queue_manager import QueueMessage


class FakeSession:
    def __init__(self, events: list[OutboxEvent]) -> None:
        self.events = events
        self.commits = 0
        self.rollbacks = 0

    def scalar(self, statement):
        description = statement.column_descriptions[0]
        if description["entity"] is OutboxEvent:
            assert statement._for_update_arg is not None
            assert statement._for_update_arg.skip_locked is True
            pending = sorted(
                (event for event in self.events if event.status == "pending"),
                key=lambda event: event.created_at,
            )
            return pending[0] if pending else None
        return sum(event.status == "pending" for event in self.events)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class FakeQueue:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.messages: list[tuple[QueueMessage, datetime]] = []

    def enqueue(self, message: QueueMessage, created_at: datetime) -> bool:
        if self.failure is not None:
            raise self.failure
        self.messages.append((message, created_at))
        return True


def _event(
    *,
    payload: dict[str, object] | list[object],
    event_type: str = "job_created",
    created_at: datetime | None = None,
) -> OutboxEvent:
    job_id = uuid4()
    if isinstance(payload, dict) and "job_id" not in payload:
        payload = {**payload, "job_id": str(job_id)}
    return OutboxEvent(
        id=uuid4(),
        event_type=event_type,
        job_id=job_id,
        payload=payload,
        status="pending",
        attempt_count=0,
        created_at=created_at or datetime.now(timezone.utc),
    )


def test_publisher_marks_successful_event_published() -> None:
    created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    event = _event(
        payload={"task_type": "send_email", "priority": 2},
        created_at=created_at,
    )
    session = FakeSession([event])
    queue = FakeQueue()

    result = publish_pending_events(session, queue, batch_size=10)  # type: ignore[arg-type]

    assert result == {
        "fetched": 1,
        "published": 1,
        "failed": 0,
        "still_pending": 0,
    }
    assert event.status == "published"
    assert event.published_at is not None
    assert event.attempt_count == 1
    assert event.last_error is None
    assert queue.messages[0][1] == created_at
    assert session.commits == 1


def test_publisher_marks_malformed_event_failed_then_continues() -> None:
    invalid = _event(
        payload={
            "job_id": str(uuid4()),
            "task_type": "send_email",
            "priority": 0,
        }
    )
    valid = _event(
        payload={"task_type": "resize_image", "priority": 1},
        created_at=invalid.created_at + timedelta(milliseconds=1),
    )
    session = FakeSession([invalid, valid])
    queue = FakeQueue()

    result = publish_pending_events(session, queue, batch_size=10)  # type: ignore[arg-type]

    assert result["fetched"] == 2
    assert result["failed"] == 1
    assert result["published"] == 1
    assert result["still_pending"] == 0
    assert invalid.status == "failed"
    assert invalid.attempt_count == 1
    assert invalid.last_error == "Outbox payload job_id does not match the event"
    assert valid.status == "published"
    assert len(queue.messages) == 1


def test_redis_failure_leaves_event_pending_and_stops_batch(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(settings, "redis_url", "redis://user:secret@localhost:6379/0")
    event = _event(payload={"task_type": "send_email", "priority": 0})
    session = FakeSession([event])
    queue = FakeQueue(
        QueueUnavailableError("queue unavailable")
    )
    queue.failure.__cause__ = ConnectionError(
        "Error connecting to redis://user:secret@localhost:6379/0"
    )

    result = publish_pending_events(session, queue, batch_size=10)  # type: ignore[arg-type]

    assert result["fetched"] == 1
    assert result["published"] == 0
    assert result["failed"] == 0
    assert result["still_pending"] == 1
    assert event.status == "pending"
    assert event.attempt_count == 1
    assert event.last_error == "Redis unavailable"
    assert "secret" not in caplog.text


def test_redis_outage_stops_multi_event_batch_and_recovery_publishes_all() -> None:
    created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    first = _event(
        payload={"task_type": "send_email", "priority": 0},
        created_at=created_at,
    )
    second = _event(
        payload={"task_type": "resize_image", "priority": 1},
        created_at=created_at + timedelta(milliseconds=1),
    )
    session = FakeSession([first, second])
    unavailable_queue = FakeQueue(QueueUnavailableError("queue unavailable"))

    failed = publish_pending_events(session, unavailable_queue, batch_size=10)  # type: ignore[arg-type]

    assert failed == {
        "fetched": 1,
        "published": 0,
        "failed": 0,
        "still_pending": 2,
    }
    assert first.status == "pending"
    assert first.attempt_count == 1
    assert first.last_error == "Redis unavailable"
    assert second.status == "pending"
    assert second.attempt_count == 0
    assert unavailable_queue.messages == []

    recovered_queue = FakeQueue()
    recovered = publish_pending_events(session, recovered_queue, batch_size=10)  # type: ignore[arg-type]

    assert recovered == {
        "fetched": 2,
        "published": 2,
        "failed": 0,
        "still_pending": 0,
    }
    assert [first.status, second.status] == ["published", "published"]
    assert [message.job_id for message, _ in recovered_queue.messages] == [
        first.job_id,
        second.job_id,
    ]


def test_pre_set_stop_flag_leaves_pending_events_untouched() -> None:
    event = _event(payload={"task_type": "send_email", "priority": 0})
    session = FakeSession([event])
    queue = FakeQueue()
    stop_event = Event()
    stop_event.set()

    result = publish_pending_events(
        session,
        queue,
        batch_size=10,  # type: ignore[arg-type]
        stop_event=stop_event,
    )

    assert result == {
        "fetched": 0,
        "published": 0,
        "failed": 0,
        "still_pending": 1,
    }
    assert event.status == "pending"
    assert event.attempt_count == 0
    assert queue.messages == []
    assert session.commits == 0
