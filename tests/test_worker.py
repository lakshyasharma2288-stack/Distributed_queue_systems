from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from threading import Event
from uuid import uuid4
import os
import socket

import fakeredis
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import Base
from app.models.job import Job
from app.models.attempt import JobAttempt
from app.models.dead_letter import DeadLetter
from app.models.worker import WorkerRecord
from app.queue.exceptions import QueueUnavailableError
from app.queue.queue_manager import QueueManager, QueueMessage
from app.tasks.schemas import HandlerResult, failure_result
from app.worker import heartbeat as heartbeat_module
from app.worker.heartbeat import WorkerHeartbeat
from app.worker.dlq import read_dead_letters
from app.worker.leases import JobLease, renew_job_lease
from app.worker.registry import (
    mark_stale_workers,
    register_worker,
    stop_worker,
    update_worker_heartbeat,
)
from app.worker.retry_policy import compute_retry_delay_seconds
from app.worker.state_machine import ALLOWED_TRANSITIONS, transition_job
from app.worker.worker import Worker


@pytest.fixture()
def worker_context() -> Iterator[tuple[sessionmaker[Session], QueueManager]]:
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
    client = fakeredis.FakeRedis(decode_responses=True)
    try:
        yield session_factory, QueueManager(client=client, key="test:worker")
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def _create_job(
    session_factory: sessionmaker[Session],
    *,
    task_type: str = "generate_pdf",
    payload: dict[str, object] | None = None,
    status: str = "pending",
) -> Job:
    job = Job(
        task_type=task_type,
        payload=payload or {"title": "test"},
        status=status,
    )
    with session_factory() as session:
        session.add(job)
        session.commit()
        session.refresh(job)
    return job


def _enqueue(queue: QueueManager, job: Job) -> None:
    queue.enqueue(
        QueueMessage(
            job_id=job.id,
            task_type=job.task_type,
            priority=job.priority,
        ),
        datetime.now(timezone.utc),
    )


def test_worker_claims_job_before_dispatch_and_saves_success(
    worker_context: tuple[sessionmaker[Session], QueueManager],
) -> None:
    session_factory, queue = worker_context
    job = _create_job(session_factory)
    queue.enqueue(
        QueueMessage(
            job_id=job.id,
            task_type="queue-data-must-not-be-used",
            priority=job.priority,
        ),
        datetime.now(timezone.utc),
    )

    def dispatch(task_type: str, payload: dict) -> HandlerResult:
        with session_factory() as session:
            processing_job = session.get(Job, job.id)
            assert processing_job is not None
            assert processing_job.status == "processing"
            assert processing_job.started_at is not None
            assert processing_job.attempts == 1
        assert task_type == "generate_pdf"
        assert payload == {"title": "test"}
        return HandlerResult(
            success=True,
            task_type=task_type,
            data={"output_path": "report.pdf"},
        )

    worker = Worker(
        queue,
        session_factory=session_factory,
        dispatch_function=dispatch,
    )

    assert worker.run_once() is True
    with session_factory() as session:
        completed = session.get(Job, job.id)
        assert completed is not None
        assert completed.status == "completed"
        assert completed.completed_at is not None
        assert completed.error_message is None
        assert completed.result == {
            "success": True,
            "task_type": "generate_pdf",
            "data": {"output_path": "report.pdf"},
            "error": None,
        }


def test_worker_marks_dispatch_failure_failed_with_safe_error(
    worker_context: tuple[sessionmaker[Session], QueueManager],
) -> None:
    session_factory, queue = worker_context
    job = _create_job(session_factory)
    _enqueue(queue, job)
    worker = Worker(
        queue,
        session_factory=session_factory,
        dispatch_function=lambda task_type, _payload: failure_result(
            task_type,
            "UNSUPPORTED_TASK",
            f"Unsupported task type: secret-{task_type}",
        ),
    )

    assert worker.run_once() is True
    with session_factory() as session:
        failed = session.get(Job, job.id)
        assert failed is not None
        assert failed.status == "failed"
        assert failed.completed_at is not None
        assert failed.result is None
        assert failed.error_message == "UNSUPPORTED_TASK: Unsupported task type."
        assert "secret" not in failed.error_message


def test_worker_converts_unexpected_dispatch_exception_to_safe_failure(
    worker_context: tuple[sessionmaker[Session], QueueManager],
) -> None:
    session_factory, queue = worker_context
    job = _create_job(session_factory)
    _enqueue(queue, job)

    def broken_dispatch(_task_type: str, _payload: dict) -> HandlerResult:
        raise RuntimeError("credential-secret")

    worker = Worker(
        queue,
        session_factory=session_factory,
        dispatch_function=broken_dispatch,
    )

    assert worker.run_once() is True
    with session_factory() as session:
        failed = session.get(Job, job.id)
        assert failed is not None
        assert failed.status == "failed"
        assert failed.error_message == (
            "INTERNAL_ERROR: An unexpected error occurred while handling the task."
        )
        assert "credential-secret" not in failed.error_message


def test_unexpected_job_failure_does_not_stop_worker_loop(
    worker_context: tuple[sessionmaker[Session], QueueManager],
) -> None:
    session_factory, queue = worker_context
    failing_job = _create_job(session_factory, payload={"title": "broken"})
    succeeding_job = _create_job(session_factory, payload={"title": "good"})
    created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    queue.enqueue(
        QueueMessage(job_id=failing_job.id, task_type="generate_pdf"),
        created_at,
    )
    queue.enqueue(
        QueueMessage(job_id=succeeding_job.id, task_type="generate_pdf"),
        created_at.replace(microsecond=1000),
    )
    stop_event = Event()

    def dispatch(task_type: str, payload: dict[str, object]) -> HandlerResult:
        if payload["title"] == "broken":
            raise RuntimeError("handler failure")
        stop_event.set()
        return HandlerResult(success=True, task_type=task_type)

    worker = Worker(
        queue,
        session_factory=session_factory,
        dispatch_function=dispatch,
        poll_interval=0.01,
    )
    worker.run(stop_event)

    with session_factory() as session:
        assert session.get(Job, failing_job.id).status == "failed"
        assert session.get(Job, succeeding_job.id).status == "completed"


@pytest.mark.parametrize("status", ["processing", "completed", "failed"])
def test_worker_skips_job_unless_pending(
    worker_context: tuple[sessionmaker[Session], QueueManager],
    status: str,
) -> None:
    session_factory, queue = worker_context
    job = _create_job(session_factory, status=status)
    _enqueue(queue, job)
    calls: list[bool] = []

    def unexpected_dispatch(
        _task_type: str,
        _payload: dict[str, object],
    ) -> HandlerResult:
        calls.append(True)
        return HandlerResult(success=True, task_type="unexpected")

    worker = Worker(
        queue,
        session_factory=session_factory,
        dispatch_function=unexpected_dispatch,
    )

    assert worker.run_once() is True
    assert calls == []
    with session_factory() as session:
        skipped = session.get(Job, job.id)
        assert skipped is not None
        assert skipped.status == status
        assert skipped.attempts == 0


def test_worker_skips_missing_job_without_crashing(
    worker_context: tuple[sessionmaker[Session], QueueManager],
) -> None:
    session_factory, queue = worker_context
    missing_id = uuid4()
    queue.enqueue(
        QueueMessage(job_id=missing_id, task_type="send_email"),
        datetime.now(timezone.utc),
    )
    worker = Worker(
        queue,
        session_factory=session_factory,
        dispatch_function=lambda *_args: pytest.fail("unexpected dispatch"),
    )

    assert worker.run_once() is True


def test_empty_queue_is_normal(
    worker_context: tuple[sessionmaker[Session], QueueManager],
) -> None:
    session_factory, queue = worker_context
    worker = Worker(queue, session_factory=session_factory)

    assert worker.run_once() is False


def test_worker_identity_is_stable_and_unique_to_the_process(
    worker_context: tuple[sessionmaker[Session], QueueManager],
) -> None:
    session_factory, queue = worker_context
    first_worker = Worker(queue, session_factory=session_factory)
    second_worker = Worker(queue, session_factory=session_factory)

    assert first_worker.worker_id == second_worker.worker_id
    assert first_worker.worker_id.startswith(
        f"{socket.gethostname()}-{os.getpid()}-"
    )


def test_worker_claims_pending_job_only_once(
    worker_context: tuple[sessionmaker[Session], QueueManager],
) -> None:
    session_factory, queue = worker_context
    job = _create_job(session_factory)
    calls: list[bool] = []

    def dispatch(task_type: str, _payload: dict[str, object]) -> HandlerResult:
        calls.append(True)
        return HandlerResult(success=True, task_type=task_type)

    first_worker = Worker(
        queue,
        session_factory=session_factory,
        dispatch_function=dispatch,
    )
    second_worker = Worker(
        queue,
        session_factory=session_factory,
        dispatch_function=dispatch,
    )

    assert first_worker.process_job(job.id) is True
    assert second_worker.process_job(job.id) is False
    assert calls == [True]
    with session_factory() as session:
        claimed = session.get(Job, job.id)
        assert claimed is not None
        assert claimed.status == "completed"
        assert claimed.attempts == 1
        assert claimed.lease_version == 1
        assert claimed.lease_owner_id is None
        assert claimed.lease_expires_at is None


def test_retryable_failure_is_scheduled_and_attempt_limit_is_enforced(
    worker_context: tuple[sessionmaker[Session], QueueManager],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import settings

    session_factory, queue = worker_context
    monkeypatch.setattr(settings, "retry_base_delay_seconds", 10.0)
    monkeypatch.setattr(settings, "retry_max_delay_seconds", 30.0)
    monkeypatch.setattr(settings, "retry_jitter_ratio", 0.0)
    job = _create_job(session_factory)
    worker = Worker(
        queue,
        session_factory=session_factory,
        dispatch_function=lambda task_type, _payload: failure_result(
            task_type,
            "TEMPORARY",
            "Retry this failure.",
            retryable=True,
        ),
    )

    assert worker.process_job(job.id)
    with session_factory() as session:
        retry = session.get(Job, job.id)
        assert retry is not None
        assert retry.status == "retry_scheduled"
        assert retry.attempts == 1
        assert retry.next_retry_at is not None
        assert retry.error_message == "TEMPORARY: Retry this failure."
        attempt = session.get(JobAttempt, (job.id, 1))
        assert attempt is not None
        assert attempt.outcome == "retry_scheduled"
        assert attempt.error_code == "TEMPORARY"
        assert attempt.ended_at is not None

    exhausted_job = _create_job(session_factory)
    with session_factory() as session:
        exhausted = session.get(Job, exhausted_job.id)
        assert exhausted is not None
        exhausted.max_attempts = 1
        session.commit()

    assert worker.process_job(exhausted_job.id)
    with session_factory() as session:
        exhausted = session.get(Job, exhausted_job.id)
        assert exhausted is not None
        assert exhausted.status == "failed"
        assert exhausted.next_retry_at is None
        assert session.get(DeadLetter, exhausted_job.id) is not None
        dead_letters = read_dead_letters(
            session,
            limit=10,
            job_id=exhausted_job.id,
        )

    assert len(dead_letters) == 1
    assert dead_letters[0]["payload"] == {"title": "test"}
    assert dead_letters[0]["attempts"][0]["outcome"] == "dead"


def test_retry_backoff_cap_and_jitter_bounds() -> None:
    assert compute_retry_delay_seconds(
        1,
        base_delay_seconds=10,
        max_delay_seconds=30,
        jitter_ratio=0,
        random_value=0,
    ) == 10
    assert compute_retry_delay_seconds(
        2,
        base_delay_seconds=10,
        max_delay_seconds=30,
        jitter_ratio=0.25,
        random_value=0,
    ) == 15
    assert compute_retry_delay_seconds(
        10,
        base_delay_seconds=10,
        max_delay_seconds=30,
        jitter_ratio=0,
        random_value=1,
    ) == 30
    assert compute_retry_delay_seconds(
        1,
        base_delay_seconds=10,
        max_delay_seconds=5,
        jitter_ratio=0,
        random_value=0,
    ) == 5
    with pytest.raises(ValueError):
        compute_retry_delay_seconds(
            0,
            base_delay_seconds=10,
            max_delay_seconds=30,
            jitter_ratio=0,
        )


def test_state_machine_accepts_all_declared_transitions_and_rejects_others() -> None:
    statuses = set(ALLOWED_TRANSITIONS)
    for current_status, next_statuses in ALLOWED_TRANSITIONS.items():
        for next_status in statuses:
            if next_status in next_statuses:
                transition_job(
                    uuid4(),
                    expected_status=current_status,
                    new_status=next_status,
                )
            else:
                with pytest.raises(ValueError, match="Invalid job transition"):
                    transition_job(
                        uuid4(),
                        expected_status=current_status,
                        new_status=next_status,
                    )


def test_job_lease_renewal_and_fencing_reject_stale_worker_results(
    worker_context: tuple[sessionmaker[Session], QueueManager],
) -> None:
    session_factory, queue = worker_context
    job = _create_job(session_factory)
    old_worker = Worker(
        queue,
        session_factory=session_factory,
        worker_id="old-worker",
    )
    first_claim = old_worker._claim_job(job.id)
    assert first_claim is not None
    assert first_claim.lease.lease_version == 1

    with session_factory() as session:
        leased = session.get(Job, job.id)
        assert leased is not None
        assert leased.status == "processing"
        assert leased.lease_owner_id == "old-worker"
        assert leased.lease_expires_at is not None

    assert renew_job_lease(
        session_factory,
        first_claim.lease,
        duration_seconds=60,
    )
    with session_factory() as session:
        leased = session.get(Job, job.id)
        assert leased is not None
        leased.lease_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        session.commit()

    assert not renew_job_lease(
        session_factory,
        first_claim.lease,
        duration_seconds=60,
    )
    old_worker._finish_job(
        first_claim.lease,
        HandlerResult(success=True, task_type="generate_pdf"),
    )
    with session_factory() as session:
        expired = session.get(Job, job.id)
        assert expired is not None
        assert expired.status == "processing"
        assert expired.result is None

        expired.status = "pending"
        expired.lease_owner_id = None
        expired.lease_expires_at = None
        session.commit()

    new_worker = Worker(
        queue,
        session_factory=session_factory,
        worker_id="new-worker",
    )
    second_claim = new_worker._claim_job(job.id)
    assert second_claim is not None
    assert second_claim.lease.lease_version == 2
    old_worker._finish_job(
        first_claim.lease,
        HandlerResult(success=True, task_type="generate_pdf"),
    )
    with session_factory() as session:
        current = session.get(Job, job.id)
        assert current is not None
        assert current.status == "processing"
        assert current.lease_owner_id == "new-worker"
        assert current.lease_version == 2

    new_worker._finish_job(
        second_claim.lease,
        HandlerResult(success=True, task_type="generate_pdf"),
    )
    with session_factory() as session:
        completed = session.get(Job, job.id)
        assert completed is not None
        assert completed.status == "completed"
        assert completed.lease_version == 2
        assert completed.lease_owner_id is None


def test_worker_registry_tracks_heartbeat_and_current_job(
    worker_context: tuple[sessionmaker[Session], QueueManager],
) -> None:
    session_factory, _queue = worker_context
    job = _create_job(session_factory)
    worker_id = f"test-worker-{uuid4().hex}"

    register_worker(
        session_factory,
        worker_id=worker_id,
        hostname="test-host",
        pid=123,
    )
    assert update_worker_heartbeat(
        session_factory,
        worker_id=worker_id,
        current_job_id=job.id,
        status="active",
        timeout_seconds=30,
    )
    with session_factory() as session:
        worker = session.get(WorkerRecord, worker_id)
        assert worker is not None
        assert worker.status == "active"
        assert worker.current_job_id == job.id
        assert worker.started_at is not None
        assert worker.last_heartbeat_at is not None

    assert stop_worker(session_factory, worker_id=worker_id)
    with session_factory() as session:
        worker = session.get(WorkerRecord, worker_id)
        assert worker is not None
        assert worker.status == "stopped"
        assert worker.stopped_at is not None
        assert worker.current_job_id is None


def test_worker_time_updates_do_not_select_database_clock(
    worker_context: tuple[sessionmaker[Session], QueueManager],
) -> None:
    session_factory, _queue = worker_context
    engine = session_factory.kw["bind"]
    statements: list[str] = []

    def record_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        worker_id = f"clock-worker-{uuid4().hex}"
        register_worker(
            session_factory,
            worker_id=worker_id,
            hostname="test-host",
            pid=123,
        )
        assert update_worker_heartbeat(
            session_factory,
            worker_id=worker_id,
            current_job_id=None,
            status="active",
            timeout_seconds=30,
        )

        job = _create_job(session_factory)
        worker = Worker(session_factory=session_factory, worker_id=worker_id)
        claim = worker._claim_job(job.id)
        assert claim is not None
        worker._finish_job(
            claim.lease,
            HandlerResult(success=True, task_type="generate_pdf"),
        )
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)

    assert not any(
        statement.lstrip().upper().startswith("SELECT CURRENT_TIMESTAMP")
        for statement in statements
    )
    with session_factory() as session:
        completed = session.get(Job, job.id)
        assert completed is not None
        assert completed.status == "completed"


def test_stale_worker_is_marked_dead_and_cannot_be_revived(
    worker_context: tuple[sessionmaker[Session], QueueManager],
) -> None:
    session_factory, _queue = worker_context
    worker_id = f"stale-worker-{uuid4().hex}"
    register_worker(
        session_factory,
        worker_id=worker_id,
        hostname="test-host",
        pid=456,
    )
    with session_factory() as session:
        worker = session.get(WorkerRecord, worker_id)
        assert worker is not None
        worker.last_heartbeat_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        session.commit()

    assert mark_stale_workers(session_factory, timeout_seconds=30) == [worker_id]
    assert not update_worker_heartbeat(
        session_factory,
        worker_id=worker_id,
        current_job_id=None,
        status="active",
        timeout_seconds=30,
    )
    with session_factory() as session:
        worker = session.get(WorkerRecord, worker_id)
        assert worker is not None
        assert worker.status == "dead"


def test_heartbeat_thread_updates_while_handler_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler_running = Event()
    heartbeat_during_handler = Event()
    renewal_during_handler = Event()
    stop_calls: list[bool] = []
    active_lease = JobLease(uuid4(), "test-worker", 1, 1)

    monkeypatch.setattr(
        heartbeat_module,
        "register_worker",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        heartbeat_module,
        "stop_worker",
        lambda *_args, **_kwargs: stop_calls.append(True) or True,
    )
    monkeypatch.setattr(
        heartbeat_module,
        "mark_stale_workers",
        lambda *_args, **_kwargs: [],
    )
    def renew_lease(*_args, **_kwargs) -> bool:
        if handler_running.is_set():
            renewal_during_handler.set()
        return True

    monkeypatch.setattr(heartbeat_module, "renew_job_lease", renew_lease)

    def update_heartbeat(*_args, **_kwargs) -> bool:
        if handler_running.is_set():
            heartbeat_during_handler.set()
        return True

    monkeypatch.setattr(
        heartbeat_module,
        "update_worker_heartbeat",
        update_heartbeat,
    )
    heartbeat = WorkerHeartbeat(
        lambda: None,
        worker_id="test-worker",
        hostname="test-host",
        pid=789,
        interval_seconds=0.01,
        timeout_seconds=0.1,
        current_job=lambda: None,
        active_lease=lambda: active_lease,
        draining_event=Event(),
        lease_duration_seconds=0.6,
        lease_renewal_interval_seconds=0.1,
    )

    heartbeat.start()
    handler_running.set()
    try:
        assert heartbeat_during_handler.wait(timeout=2)
        assert renewal_during_handler.wait(timeout=2)
    finally:
        handler_running.clear()
        assert heartbeat.stop()

    assert stop_calls == [True]


def test_worker_loop_waits_on_empty_queue_and_stops_cleanly(
    worker_context: tuple[sessionmaker[Session], QueueManager],
) -> None:
    session_factory, queue = worker_context

    class StopOnWait(Event):
        wait_calls = 0

        def wait(self, timeout: float | None = None) -> bool:
            self.wait_calls += 1
            self.set()
            return True

    stop_event = StopOnWait()
    worker = Worker(queue, session_factory=session_factory, poll_interval=0.01)

    worker.run(stop_event)

    assert stop_event.wait_calls == 1
    with session_factory() as session:
        record = session.get(WorkerRecord, worker.worker_id)
        assert record is not None
        assert record.status == "stopped"
        assert record.stopped_at is not None


def test_worker_retries_queue_after_connection_failure(
    worker_context: tuple[sessionmaker[Session], QueueManager],
) -> None:
    session_factory, _queue = worker_context

    class FailingOnceQueue:
        calls = 0

        def pop_job_id(self):
            self.calls += 1
            if self.calls == 1:
                raise QueueUnavailableError("queue unavailable")
            return None

    class StopAfterRetry(Event):
        def wait(self, timeout: float | None = None) -> bool:
            if failing_queue.calls == 2:
                self.set()
            return self.is_set()

    failing_queue = FailingOnceQueue()
    worker = Worker(
        failing_queue,  # type: ignore[arg-type]
        session_factory=session_factory,
        poll_interval=0.01,
    )
    stop_event = StopAfterRetry()

    worker.run(stop_event)

    assert failing_queue.calls == 2


def test_worker_rejects_nonpositive_poll_interval(
    worker_context: tuple[sessionmaker[Session], QueueManager],
) -> None:
    _, queue = worker_context

    with pytest.raises(ValueError, match="poll_interval"):
        Worker(queue, poll_interval=0)
