import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import fakeredis
import pytest
from redis.exceptions import RedisError
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, func, inspect, select, text, update
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.api.routes.jobs import get_db
from app.api.routes.queue import get_queue_manager
from app.core.config import settings
from app.main import app
from app.models.job import Job
from app.models.outbox_event import OutboxEvent
from app.publisher.outbox_publisher import publish_pending_events
from app.queue.exceptions import QueueUnavailableError
from app.queue.queue_manager import QueueManager, QueueMessage
from app.tasks.schemas import HandlerResult
from app.worker.leases import renew_job_lease
from app.worker.worker import Worker
from week4_safety import (
    DatabaseSafetyError,
    unavailable_integration,
    validate_test_database_url,
    validate_test_redis_url,
)


@dataclass
class PostgresTestContext:
    engine: Engine
    schema_url: URL
    alembic_config: Config


@pytest.fixture(scope="module")
def postgres_context(configured_database_url: str) -> Iterator[PostgresTestContext]:
    test_database_url = os.environ.get("TEST_DATABASE_URL")
    if not test_database_url:
        unavailable_integration(
            "TEST_DATABASE_URL is not set; PostgreSQL integration tests unverified"
        )

    try:
        base_url = validate_test_database_url(
            test_database_url,
            configured_database_url,
        )
    except DatabaseSafetyError as exc:
        pytest.fail(str(exc))

    admin_engine = create_engine(base_url, pool_pre_ping=True)
    try:
        with admin_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        admin_engine.dispose()
        unavailable_integration(
            f"TEST_DATABASE_URL is unreachable ({type(exc).__name__}); "
            "PostgreSQL integration tests unverified"
        )

    schema_name = f"djq_test_{uuid4().hex}"
    try:
        with admin_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
    except SQLAlchemyError as exc:
        admin_engine.dispose()
        unavailable_integration(
            f"TEST_DATABASE_URL cannot create an isolated schema "
            f"({type(exc).__name__}); integration tests unverified"
        )

    original_database_url = settings.database_url
    schema_url = base_url.update_query_dict(
        {"options": f"-csearch_path={schema_name}"}
    )
    settings.database_url = schema_url.render_as_string(hide_password=False)
    alembic_config = Config(
        str(Path(__file__).resolve().parents[1] / "alembic.ini")
    )
    engine: Engine | None = None
    try:
        command.upgrade(alembic_config, "head")
        engine = create_engine(schema_url, pool_pre_ping=True)
        yield PostgresTestContext(engine, schema_url, alembic_config)
    finally:
        if engine is not None:
            engine.dispose()
        settings.database_url = original_database_url
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        admin_engine.dispose()


@pytest.fixture(autouse=True)
def clear_postgres_test_schema(postgres_context: PostgresTestContext) -> Iterator[None]:
    with postgres_context.engine.begin() as connection:
        connection.execute(text("TRUNCATE TABLE outbox_events, jobs CASCADE"))
    yield


def _create_job_event(
    session: Session,
    task_type: str = "send_email",
    priority: int = 0,
    payload_override: dict[str, object] | None = None,
) -> OutboxEvent:
    job = Job(task_type=task_type, payload={}, priority=priority)
    session.add(job)
    session.flush()
    payload = payload_override or {
        "job_id": str(job.id),
        "task_type": task_type,
        "priority": priority,
    }
    event = OutboxEvent(
        event_type="job_created",
        job_id=job.id,
        payload=payload,
    )
    session.add(event)
    session.commit()
    session.refresh(event)
    return event


@pytest.mark.integration
def test_outbox_defaults_relationship_unique_and_migration_cycle(
    postgres_context: PostgresTestContext,
) -> None:
    engine = postgres_context.engine
    with Session(engine) as session:
        event = _create_job_event(session)
        job = session.get(Job, event.job_id)
        assert job is not None
        assert event.job == job
        assert event.status == "pending"
        assert event.attempt_count == 0
        assert event.created_at is not None

        duplicate = OutboxEvent(
            event_type=event.event_type,
            job_id=event.job_id,
            payload=event.payload,
        )
        session.add(duplicate)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    command.downgrade(postgres_context.alembic_config, "c3176ff8c2cd")
    assert not inspect(engine).has_table("outbox_events")
    command.upgrade(postgres_context.alembic_config, "head")
    assert inspect(engine).has_table("outbox_events")


@pytest.mark.integration
def test_publisher_success_duplicate_recovery_and_job_status_unchanged(
    postgres_context: PostgresTestContext,
) -> None:
    engine = postgres_context.engine
    redis_client = fakeredis.FakeRedis(decode_responses=True)
    queue = QueueManager(client=redis_client, key=f"djq:test:{uuid4().hex}")
    try:
        with Session(engine) as session:
            event = _create_job_event(session, task_type="resize_image", priority=3)
            job = session.get(Job, event.job_id)
            assert job is not None
            message = QueueMessage(
                job_id=job.id,
                task_type=job.task_type,
                priority=job.priority,
            )
            assert queue.enqueue(message, event.created_at) is True

            result = publish_pending_events(session, queue, batch_size=10)

            assert result == {
                "fetched": 1,
                "published": 1,
                "failed": 0,
                "still_pending": 0,
            }
            assert event.status == "published"
            assert event.published_at is not None
            assert event.attempt_count == 1
            assert job.status == "pending"
            assert queue.length() == 1
            assert publish_pending_events(session, queue, batch_size=10)["fetched"] == 0
            assert queue.length() == 1
    finally:
        redis_client.close()


@pytest.mark.integration
def test_publisher_redis_failure_stays_pending_and_recovers(
    postgres_context: PostgresTestContext,
) -> None:
    class UnavailableQueue(QueueManager):
        def __init__(self) -> None:
            pass

        def enqueue(self, _message: QueueMessage, _created_at: datetime) -> bool:
            del _message, _created_at
            raise QueueUnavailableError("Redis unavailable")

    engine = postgres_context.engine
    with Session(engine) as session:
        event = _create_job_event(session)
        failed = publish_pending_events(
            session,
            UnavailableQueue(),
            batch_size=10,
        )
        assert failed["fetched"] == 1
        assert failed["published"] == 0
        assert failed["still_pending"] == 1
        assert event.status == "pending"
        assert event.attempt_count == 1

        redis_client = fakeredis.FakeRedis(decode_responses=True)
        queue = QueueManager(client=redis_client, key=f"djq:test:{uuid4().hex}")
        try:
            recovered = publish_pending_events(session, queue, batch_size=10)
            assert recovered["published"] == 1
            assert event.status == "published"
            assert event.attempt_count == 2
        finally:
            redis_client.close()


@pytest.mark.integration
def test_malformed_event_fails_and_publisher_continues(
    postgres_context: PostgresTestContext,
) -> None:
    engine = postgres_context.engine
    redis_client = fakeredis.FakeRedis(decode_responses=True)
    queue = QueueManager(client=redis_client, key=f"djq:test:{uuid4().hex}")
    try:
        with Session(engine) as session:
            invalid = _create_job_event(
                session,
                payload_override={
                    "job_id": str(uuid4()),
                    "task_type": "send_email",
                    "priority": 0,
                },
            )
            valid = _create_job_event(
                session,
                task_type="generate_pdf",
                priority=2,
            )
            result = publish_pending_events(session, queue, batch_size=10)

            assert result["fetched"] == 2
            assert result["failed"] == 1
            assert result["published"] == 1
            assert result["still_pending"] == 0
            assert invalid.status == "failed"
            assert invalid.last_error == "Outbox payload job_id does not match the event"
            assert valid.status == "published"
    finally:
        redis_client.close()


@pytest.mark.integration
def test_post_publish_inspection_end_to_end(
    postgres_context: PostgresTestContext,
) -> None:
    redis_url = os.environ.get("REDIS_TEST_URL")
    if not redis_url:
        unavailable_integration(
            "REDIS_TEST_URL is not set; Redis end-to-end test unverified"
        )
    try:
        validate_test_redis_url(redis_url, settings.redis_url)
    except DatabaseSafetyError as exc:
        pytest.fail(str(exc))
    from urllib.parse import urlsplit

    db_index = urlsplit(redis_url).path.strip("/")
    if db_index != "15":
        pytest.fail("REDIS_TEST_URL must use the dedicated Redis database index 15")

    import redis

    redis_client = redis.Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=settings.redis_connect_timeout_seconds,
        socket_timeout=settings.redis_socket_timeout_seconds,
    )
    try:
        redis_client.ping()
    except RedisError as exc:
        redis_client.close()
        unavailable_integration(
            f"REDIS_TEST_URL is unreachable ({type(exc).__name__}); "
            "end-to-end test unverified"
        )

    queue_key = f"djq:test:{uuid4().hex}"
    queue = QueueManager(client=redis_client, key=queue_key)
    session_factory = sessionmaker(
        bind=postgres_context.engine,
        autoflush=False,
        expire_on_commit=False,
    )

    def override_get_db() -> Iterator[Session]:
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_queue_manager] = lambda: queue
    try:
        with TestClient(app) as client:
            response = client.post(
                "/jobs",
                json={
                    "task_type": "send_email",
                    "payload": {"to": "person@example.com"},
                },
            )
            assert response.status_code == 201
            job_id = response.json()["id"]
            assert client.get("/queue/jobs").json()["queue"] == []

            with session_factory() as session:
                event = session.scalar(
                    select(OutboxEvent).where(OutboxEvent.job_id == job_id)
                )
                assert event is not None
                assert event.status == "pending"
                result = publish_pending_events(session, queue, batch_size=10)
                assert result["published"] == 1
                assert event.status == "published"

            inspection = client.get("/queue/jobs")
            assert inspection.status_code == 200
            assert inspection.json()["queue"] == [
                {
                    "job_id": job_id,
                    "task_type": "send_email",
                    "priority": 0,
                }
            ]
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_queue_manager, None)
        try:
            redis_client.delete(queue_key)
        finally:
            redis_client.close()


@pytest.mark.integration
def test_concurrent_workers_only_one_claims_the_same_job(
    postgres_context: PostgresTestContext,
) -> None:
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
    from threading import Barrier, Event

    engine = postgres_context.engine
    session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )
    with Session(engine) as session:
        job = Job(task_type="test_task", payload={})
        session.add(job)
        session.commit()
        job_id = job.id

    start_barrier = Barrier(3)
    handler_started = Event()
    release_handler = Event()
    dispatch_calls: list[bool] = []

    def dispatch(task_type: str, _payload: dict[str, object]) -> HandlerResult:
        dispatch_calls.append(True)
        handler_started.set()
        if not release_handler.wait(timeout=5):
            raise TimeoutError("test handler was not released")
        return HandlerResult(success=True, task_type=task_type)

    workers = [
        Worker(session_factory=session_factory, dispatch_function=dispatch)
        for _ in range(2)
    ]

    def process_after_barrier(worker: Worker) -> bool:
        start_barrier.wait(timeout=5)
        return worker.process_job(job_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(process_after_barrier, worker)
            for worker in workers
        ]
        start_barrier.wait(timeout=5)
        try:
            assert handler_started.wait(timeout=5)
            completed, _ = wait(
                futures,
                timeout=5,
                return_when=FIRST_COMPLETED,
            )
            assert len(completed) == 1
            assert next(iter(completed)).result() is False
        finally:
            release_handler.set()

        assert sorted(future.result(timeout=5) for future in futures) == [False, True]

    assert dispatch_calls == [True]
    with Session(engine) as session:
        claimed = session.get(Job, job_id)
        assert claimed is not None
        assert claimed.status == "completed"
        assert claimed.attempts == 1


@pytest.mark.integration
def test_concurrent_recovery_transitions_an_interrupted_job_once(
    postgres_context: PostgresTestContext,
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from app.models.attempt import JobAttempt
    from app.models.worker import WorkerRecord
    from app.worker.recovery import recover_interrupted_jobs

    session_factory = sessionmaker(
        bind=postgres_context.engine,
        autoflush=False,
        expire_on_commit=False,
    )
    worker_id = f"recovery-worker-{uuid4().hex}"
    with Session(postgres_context.engine) as session:
        job = Job(
            task_type="test_task",
            payload={},
            status="processing",
            attempts=1,
            max_attempts=3,
            lease_owner_id=worker_id,
            lease_expires_at=datetime.now().astimezone() - timedelta(minutes=1),
            lease_version=1,
            started_at=datetime.now().astimezone() - timedelta(minutes=2),
        )
        session.add(job)
        session.add(
            WorkerRecord(
                worker_id=worker_id,
                hostname="test-host",
                pid=123,
                status="active",
            )
        )
        session.flush()
        session.add(
            JobAttempt(
                job_id=job.id,
                attempt_number=1,
                worker_id=worker_id,
                outcome="running",
            )
        )
        session.commit()
        job_id = job.id

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _index: recover_interrupted_jobs(
                    10,
                    session_factory=session_factory,
                ),
                range(2),
            )
        )

    assert sum(result["recovered"] for result in results) == 1
    with Session(postgres_context.engine) as session:
        recovered = session.get(Job, job_id)
        assert recovered is not None
        assert recovered.status == "retry_scheduled"


@pytest.mark.integration
def test_concurrent_idempotent_submissions_create_one_job_and_event(
    postgres_context: PostgresTestContext,
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    session_factory = sessionmaker(
        bind=postgres_context.engine,
        autoflush=False,
        expire_on_commit=False,
    )

    def override_get_db() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    def submit_job() -> dict[str, object]:
        with TestClient(app) as client:
            response = client.post(
                "/jobs",
                headers={"Idempotency-Key": "concurrent-request-key"},
                json={
                    "task_type": "test_task",
                    "payload": {"value": "same"},
                },
            )
        assert response.status_code == 201
        return response.json()

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(lambda _index: submit_job(), range(2)))
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert responses[0] == responses[1]
    with Session(postgres_context.engine) as session:
        assert session.query(Job).count() == 1
        assert session.query(OutboxEvent).count() == 1


@pytest.mark.integration
def test_postgres_lease_expiry_and_fencing_reject_late_result(
    postgres_context: PostgresTestContext,
) -> None:
    engine = postgres_context.engine
    session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )
    with Session(engine) as session:
        job = Job(task_type="test_task", payload={})
        session.add(job)
        session.commit()
        job_id = job.id

    old_worker = Worker(session_factory=session_factory, worker_id="old-owner")
    old_claim = old_worker._claim_job(job_id)
    assert old_claim is not None
    with Session(engine) as session:
        session.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(lease_expires_at=func.now() - timedelta(seconds=1))
        )
        session.commit()

    assert not renew_job_lease(
        session_factory,
        old_claim.lease,
        duration_seconds=60,
    )
    with Session(engine) as session:
        session.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(
                status="pending",
                lease_owner_id=None,
                lease_expires_at=None,
            )
        )
        session.commit()

    new_worker = Worker(session_factory=session_factory, worker_id="new-owner")
    new_claim = new_worker._claim_job(job_id)
    assert new_claim is not None
    assert new_claim.lease.lease_version == old_claim.lease.lease_version + 1
    old_worker._finish_job(
        old_claim.lease,
        HandlerResult(success=True, task_type="test_task"),
    )
    with Session(engine) as session:
        current = session.get(Job, job_id)
        assert current is not None
        assert current.status == "processing"
        assert current.lease_owner_id == "new-owner"
        assert current.result is None

    new_worker._finish_job(
        new_claim.lease,
        HandlerResult(success=True, task_type="test_task"),
    )
    with Session(engine) as session:
        completed = session.get(Job, job_id)
        assert completed is not None
        assert completed.status == "completed"
        assert completed.lease_owner_id is None
