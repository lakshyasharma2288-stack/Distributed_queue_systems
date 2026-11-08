from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import fakeredis
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.db.database import Base
from app.models.attempt import JobAttempt
from app.models.dead_letter import DeadLetter
from app.models.job import Job
from app.models.worker import WorkerRecord
from app.queue.queue_manager import QueueManager
from app.tasks.schemas import HandlerResult
from app.worker.leases import JobLease
from app.worker.recovery import (
    reconcile_pending_jobs,
    recover_interrupted_jobs,
)
from app.worker.worker import Worker


@pytest.fixture()
def recovery_context() -> Iterator[tuple[sessionmaker[Session], QueueManager]]:
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
            key=f"test:recovery:{uuid4().hex}",
        )
    finally:
        redis_client.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def _create_processing_job(
    session_factory: sessionmaker[Session],
    *,
    worker_id: str,
    max_attempts: int = 3,
    lease_expires_at: datetime | None,
    register_worker: bool = True,
    add_attempt: bool = True,
) -> Job:
    job = Job(
        task_type="generate_pdf",
        payload={"title": "recovery"},
        status="processing",
        attempts=1,
        max_attempts=max_attempts,
        lease_owner_id=worker_id if lease_expires_at is not None else None,
        lease_expires_at=lease_expires_at,
        lease_version=1,
        started_at=datetime.now(timezone.utc) - timedelta(minutes=2),
    )
    with session_factory() as session:
        session.add(job)
        if register_worker:
            session.add(
                WorkerRecord(
                    worker_id=worker_id,
                    hostname="test-host",
                    pid=123,
                    status="active",
                )
            )
        session.flush()
        if add_attempt:
            session.add(
                JobAttempt(
                    job_id=job.id,
                    attempt_number=1,
                    worker_id=worker_id,
                    outcome="running",
                )
            )
        session.commit()
        session.refresh(job)
    return job


def test_expired_job_is_retried_and_recovery_is_idempotent(
    recovery_context: tuple[sessionmaker[Session], QueueManager],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory, queue = recovery_context
    monkeypatch.setattr(settings, "retry_base_delay_seconds", 1.0)
    monkeypatch.setattr(settings, "retry_max_delay_seconds", 10.0)
    monkeypatch.setattr(settings, "retry_jitter_ratio", 0.0)
    job = _create_processing_job(
        session_factory,
        worker_id="expired-worker",
        lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )

    assert recover_interrupted_jobs(10, session_factory=session_factory) == {
        "recovered": 1,
        "retried": 1,
        "dead_lettered": 0,
    }
    assert recover_interrupted_jobs(10, session_factory=session_factory) == {
        "recovered": 0,
        "retried": 0,
        "dead_lettered": 0,
    }
    with session_factory() as session:
        recovered = session.get(Job, job.id)
        attempt = session.get(JobAttempt, (job.id, 1))
        assert recovered is not None
        assert recovered.status == "retry_scheduled"
        assert recovered.lease_owner_id is None
        assert recovered.lease_expires_at is None
        assert recovered.next_retry_at is not None
        assert attempt is not None
        assert attempt.outcome == "retry_scheduled"
        assert attempt.error_code == "WORKER_LOST"

    old_worker = Worker(session_factory=session_factory, worker_id="expired-worker")
    old_worker._finish_job(
        JobLease(job.id, "expired-worker", 1, 1),
        result=HandlerResult(
            success=True,
            task_type="generate_pdf",
        ),
    )
    with session_factory() as session:
        assert session.get(Job, job.id).status == "retry_scheduled"


def test_stale_worker_signal_recovers_unexpired_lease(
    recovery_context: tuple[sessionmaker[Session], QueueManager],
) -> None:
    session_factory, _queue = recovery_context
    job = _create_processing_job(
        session_factory,
        worker_id="dead-worker",
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
    )
    with session_factory() as session:
        worker = session.get(WorkerRecord, "dead-worker")
        assert worker is not None
        worker.status = "dead"
        session.commit()

    result = recover_interrupted_jobs(10, session_factory=session_factory)

    assert result["recovered"] == 1
    with session_factory() as session:
        recovered = session.get(Job, job.id)
        assert recovered is not None
        assert recovered.status == "retry_scheduled"


def test_exhausted_job_is_dead_lettered_with_legacy_attempt_history(
    recovery_context: tuple[sessionmaker[Session], QueueManager],
) -> None:
    session_factory, _queue = recovery_context
    job = _create_processing_job(
        session_factory,
        worker_id="legacy-worker",
        max_attempts=1,
        lease_expires_at=None,
        register_worker=False,
        add_attempt=False,
    )

    result = recover_interrupted_jobs(10, session_factory=session_factory)

    assert result == {"recovered": 1, "retried": 0, "dead_lettered": 1}
    with session_factory() as session:
        failed = session.get(Job, job.id)
        dead_letter = session.get(DeadLetter, job.id)
        attempt = session.get(JobAttempt, (job.id, 1))
        assert failed is not None
        assert failed.status == "failed"
        assert dead_letter is not None
        assert dead_letter.final_error_code == "WORKER_LOST"
        assert attempt is not None
        assert attempt.worker_id == "legacy-unknown"
        assert attempt.outcome == "dead"


def test_pending_job_reconciliation_reenqueues_without_mutating_job(
    recovery_context: tuple[sessionmaker[Session], QueueManager],
) -> None:
    session_factory, queue = recovery_context
    job = Job(
        task_type="generate_pdf",
        payload={"title": "lost redis hint"},
        status="pending",
    )
    with session_factory() as session:
        session.add(job)
        session.commit()
        session.refresh(job)
        result = reconcile_pending_jobs(session, queue, batch_size=10)

    assert result == {"enqueued": 1}
    assert queue.peek(10)[0].job_id == job.id
    with session_factory() as session:
        unchanged = session.get(Job, job.id)
        assert unchanged is not None
        assert unchanged.status == "pending"
