from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import fakeredis
import pytest
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy import create_engine

from app.db.database import Base
from app.models.job import Job
from app.models.worker import WorkerRecord
from app.queue.exceptions import QueueUnavailableError
from app.queue.queue_manager import QueueManager
from app.worker.retry_scheduler import publish_due_retries


@pytest.fixture()
def retry_context() -> Iterator[tuple[sessionmaker[Session], QueueManager]]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )
    redis_client = fakeredis.FakeRedis(decode_responses=True)
    try:
        yield session_factory, QueueManager(
            client=redis_client,
            key=f"test:retry:{uuid4().hex}",
        )
    finally:
        redis_client.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def _create_retry_job(
    session_factory: sessionmaker[Session],
    *,
    next_retry_at: datetime,
) -> Job:
    job = Job(
        task_type="generate_pdf",
        payload={"title": "retry"},
        status="retry_scheduled",
        attempts=1,
        next_retry_at=next_retry_at,
    )
    with session_factory() as session:
        session.add(job)
        session.commit()
        session.refresh(job)
    return job


def test_scheduler_publishes_only_due_jobs_and_transitions_atomically(
    retry_context: tuple[sessionmaker[Session], QueueManager],
) -> None:
    session_factory, queue = retry_context
    due_job = _create_retry_job(
        session_factory,
        next_retry_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    future_job = _create_retry_job(
        session_factory,
        next_retry_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )

    with session_factory() as session:
        result = publish_due_retries(session, queue, batch_size=10)

    assert result == {"published": 1, "still_due": 0}
    queued = queue.peek(10)
    assert len(queued) == 1
    assert queued[0].job_id == due_job.id
    with session_factory() as session:
        due = session.get(Job, due_job.id)
        future = session.get(Job, future_job.id)
        assert due is not None
        assert due.status == "pending"
        assert due.next_retry_at is None
        assert future is not None
        assert future.status == "retry_scheduled"


def test_scheduler_keeps_retry_scheduled_when_redis_is_unavailable(
    retry_context: tuple[sessionmaker[Session], QueueManager],
) -> None:
    session_factory, _queue = retry_context
    job = _create_retry_job(
        session_factory,
        next_retry_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )

    class UnavailableQueue(QueueManager):
        def enqueue(self, *_args, **_kwargs) -> bool:
            raise QueueUnavailableError("Redis unavailable")

    with session_factory() as session:
        result = publish_due_retries(session, UnavailableQueue(), batch_size=10)

    assert result == {"published": 0, "still_due": 1}
    with session_factory() as session:
        current = session.get(Job, job.id)
        assert current is not None
        assert current.status == "retry_scheduled"
        assert current.next_retry_at is not None
