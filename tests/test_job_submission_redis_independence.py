from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import Base, get_db
from app.main import app
from app.models.job import Job
from app.models.outbox_event import OutboxEvent
from app.queue import redis_client


@pytest.fixture()
def test_engine() -> Iterator[Engine]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture()
def client(
    test_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    test_session_factory = sessionmaker(
        bind=test_engine,
        autoflush=False,
        expire_on_commit=False,
    )

    def override_get_db() -> Iterator[Session]:
        db = test_session_factory()
        try:
            yield db
        finally:
            db.close()

    def redis_must_not_be_used():
        raise AssertionError("POST /jobs must not request a Redis client")

    monkeypatch.setattr(redis_client, "get_redis_client", redis_must_not_be_used)
    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_post_job_returns_same_response_without_using_redis(
    client: TestClient,
    test_engine: Engine,
) -> None:
    response = client.post(
        "/jobs",
        json={"task_type": "send_email", "payload": {"to": "person@example.com"}},
    )

    assert response.status_code == 201
    assert set(response.json()) == {"id", "status"}
    assert response.json()["status"] == "pending"

    with Session(test_engine) as session:
        job = session.scalar(select(Job))
        outbox_event = session.scalar(select(OutboxEvent))
        assert job is not None
        assert outbox_event is not None
        assert outbox_event.job_id == job.id
        assert outbox_event.job == job
        assert outbox_event.event_type == "job_created"
        assert outbox_event.status == "pending"
        assert outbox_event.payload == {
            "job_id": str(job.id),
            "task_type": "send_email",
            "priority": 0,
        }


def test_job_and_outbox_event_roll_back_together(
    client: TestClient,
    test_engine: Engine,
) -> None:
    def fail_outbox_insert(session, _flush_context, _instances) -> None:
        if any(isinstance(item, OutboxEvent) for item in session.new):
            raise IntegrityError(
                "forced outbox failure",
                {},
                RuntimeError("forced failure"),
            )

    event.listen(Session, "before_flush", fail_outbox_insert)
    try:
        response = client.post(
            "/jobs",
            json={"task_type": "send_email", "payload": {"to": "person@example.com"}},
        )
    finally:
        event.remove(Session, "before_flush", fail_outbox_insert)

    assert response.status_code == 500
    with Session(test_engine) as session:
        assert session.scalar(select(Job)) is None
        assert session.scalar(select(OutboxEvent)) is None
