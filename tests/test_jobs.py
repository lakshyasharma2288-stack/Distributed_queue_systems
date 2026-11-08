import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import Base, get_db
from app.main import app
from app.models.job import Job
from app.models.outbox_event import OutboxEvent


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
def client(test_engine: Engine) -> Iterator[TestClient]:
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

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_database_connectivity(test_engine: Engine) -> None:
    with test_engine.connect() as connection:
        assert connection.scalar(text("SELECT 1")) == 1


def test_job_creation(client: TestClient) -> None:
    response = client.post(
        "/jobs",
        json={"task_type": "send_email", "payload": {"to": "person@example.com"}},
    )

    assert response.status_code == 201
    job = response.json()
    assert uuid.UUID(job["id"])
    assert job["status"] == "pending"


def test_job_submission_is_persisted(client: TestClient) -> None:
    payload = {"to": "person@example.com"}
    create_response = client.post(
        "/jobs",
        json={"task_type": "send_email", "payload": payload},
    )

    job_id = create_response.json()["id"]
    response = client.get(f"/jobs/{job_id}")

    assert response.status_code == 200
    assert response.json()["id"] == job_id
    assert response.json()["task_type"] == "send_email"
    assert response.json()["payload"] == payload
    assert response.json()["status"] == "pending"
    assert response.json()["attempts"] == 0


def test_job_list_returns_newest_first(client: TestClient, test_engine: Engine) -> None:
    now = datetime.now(timezone.utc)
    jobs = [
        Job(
            id=uuid.uuid4(),
            task_type="send_email",
            payload={"to": "older@example.com"},
            created_at=now - timedelta(minutes=5),
        ),
        Job(
            id=uuid.uuid4(),
            task_type="generate_pdf",
            payload={"title": "Newer", "sections": [{"paragraphs": ["Report"]}]},
            created_at=now,
        ),
    ]
    newest_id, older_id = (str(job.id) for job in reversed(jobs))
    with Session(test_engine) as db:
        db.add_all(jobs)
        db.commit()

    response = client.get("/jobs")

    assert response.status_code == 200
    listed_jobs = response.json()
    assert [job["id"] for job in listed_jobs] == [newest_id, older_id]
    assert listed_jobs[0]["created_at"] > listed_jobs[1]["created_at"]
    assert listed_jobs[0]["status"] == "pending"
    assert listed_jobs[0]["payload"]["title"] == "Newer"


def test_idempotency_key_reuses_job_and_outbox_event(
    client: TestClient,
    test_engine: Engine,
) -> None:
    request = {
        "task_type": "send_email",
        "payload": {
            "to": "person@example.com",
            "subject": "Hello",
            "body": "Message",
        },
    }
    first = client.post(
        "/jobs",
        json=request,
        headers={"Idempotency-Key": "request-123"},
    )
    replay = client.post(
        "/jobs",
        json=request,
        headers={"Idempotency-Key": "request-123"},
    )

    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    with Session(test_engine) as db:
        assert db.query(Job).count() == 1
        assert db.query(OutboxEvent).count() == 1


def test_idempotency_key_rejects_different_request(client: TestClient) -> None:
    headers = {"Idempotency-Key": "request-456"}
    first = client.post(
        "/jobs",
        json={"task_type": "generate_report", "payload": {"format": "csv"}},
        headers=headers,
    )
    second = client.post(
        "/jobs",
        json={"task_type": "generate_report", "payload": {"format": "json"}},
        headers=headers,
    )

    assert first.status_code == 201
    assert second.status_code == 409


def test_job_retrieval(client: TestClient, test_engine: Engine) -> None:
    job_id = uuid.uuid4()
    with Session(test_engine) as db:
        db.add(Job(id=job_id, task_type="generate_report", payload={"format": "csv"}))
        db.commit()

    response = client.get(f"/jobs/{job_id}")

    assert response.status_code == 200
    assert response.json()["id"] == str(job_id)
    assert response.json()["task_type"] == "generate_report"
    assert response.json()["status"] == "pending"


def test_invalid_job_id_returns_validation_error(client: TestClient) -> None:
    response = client.get("/jobs/not-a-uuid")

    assert response.status_code == 422


def test_nonexistent_job_returns_404(client: TestClient) -> None:
    response = client.get(f"/jobs/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["detail"] == "Job not found"


@pytest.mark.parametrize(
    "job_request",
    [
        {"payload": {"key": "value"}},
        {"task_type": "", "payload": {"key": "value"}},
        {"task_type": "send_email", "payload": []},
    ],
)
def test_invalid_job_request_returns_validation_error(
    client: TestClient,
    job_request: dict[str, object],
) -> None:
    response = client.post("/jobs", json=job_request)

    assert response.status_code == 422
