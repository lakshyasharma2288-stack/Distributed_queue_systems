import json
import os
import statistics
import tempfile
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import pytest
import redis
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from redis.exceptions import RedisError
from sqlalchemy import Engine, create_engine, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.api.routes.jobs import get_db
from app.core.config import BASE_DIR, settings
from app.db.database import Base
from app.main import app
from app.models.attempt import JobAttempt
from app.models.job import Job
from app.models.worker import WorkerRecord
from app.publisher.outbox_publisher import publish_pending_events
from app.queue.queue_manager import QueueManager, QueueMessage
from app.tasks.dispatcher import TASK_REGISTRY, dispatch
from app.tasks.schemas import GeneratePDFPayload, HandlerResult
from app.worker.worker import ClaimedJob, Worker
from week4_safety import (
    DatabaseSafetyError,
    unavailable_integration,
    validate_test_database_url,
    validate_test_redis_url,
)

_ALLOWED_JOB_COUNTS = {100, 500, 1000}
_ALLOWED_WORKER_COUNTS = {1, 2, 4}
_TASK_TYPE = "generate_pdf"
_TASK_PAYLOAD = {
    "title": "Week 7 benchmark report",
    "sections": [
        {
            "heading": "Summary",
            "paragraphs": ["Distributed job queue PDF benchmark."],
        },
    ],
}


def _sample_summary(samples: list[float]) -> dict[str, float | int | None]:
    if not samples:
        return {
            "count": 0,
            "average_ms": None,
            "p50_ms": None,
            "p95_ms": None,
            "p99_ms": None,
            "minimum_ms": None,
            "maximum_ms": None,
        }

    ordered = sorted(samples)

    def percentile(percent: float) -> float:
        position = (len(ordered) - 1) * percent
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        weight = position - lower
        return ordered[lower] * (1 - weight) + ordered[upper] * weight

    return {
        "count": len(ordered),
        "average_ms": statistics.fmean(ordered),
        "p50_ms": percentile(0.50),
        "p95_ms": percentile(0.95) if len(ordered) >= 20 else None,
        "p99_ms": percentile(0.99) if len(ordered) >= 100 else None,
        "minimum_ms": ordered[0],
        "maximum_ms": ordered[-1],
    }


def _parse_scenario() -> tuple[int, int]:
    try:
        job_count = int(os.environ.get("WEEK7_JOB_COUNT", ""))
        worker_count = int(os.environ.get("WEEK7_WORKER_COUNT", ""))
    except ValueError as exc:
        raise ValueError(
            "WEEK7_JOB_COUNT and WEEK7_WORKER_COUNT must be integers"
        ) from exc

    if job_count not in _ALLOWED_JOB_COUNTS:
        raise ValueError("WEEK7_JOB_COUNT must be 100, 500, or 1000")
    if worker_count not in _ALLOWED_WORKER_COUNTS:
        raise ValueError("WEEK7_WORKER_COUNT must be 1, 2, or 4")
    return job_count, worker_count


def _duration_ms(started_at: datetime | None, ended_at: datetime | None) -> float | None:
    if started_at is None or ended_at is None:
        return None
    duration_ms = (ended_at - started_at).total_seconds() * 1000
    if duration_ms < 0:
        raise ValueError("Database lifecycle timestamps are out of order")
    return duration_ms


def _database_lifecycle_samples(
    session_factory: sessionmaker[Session],
    job_ids: list[UUID],
) -> dict[str, list[float]]:
    samples: dict[str, list[float]] = {
        "database_job_created_to_started_ms": [],
        "database_job_started_to_completed_ms": [],
        "database_job_created_to_completed_ms": [],
        "database_attempt_started_to_ended_ms": [],
    }
    if not job_ids:
        return samples

    with session_factory() as session:
        job_timestamps = session.execute(
            select(Job.created_at, Job.started_at, Job.completed_at).where(
                Job.id.in_(job_ids)
            )
        ).all()
        attempt_timestamps = session.execute(
            select(JobAttempt.started_at, JobAttempt.ended_at).where(
                JobAttempt.job_id.in_(job_ids)
            )
        ).all()

    for created_at, started_at, completed_at in job_timestamps:
        durations = (
            (
                "database_job_created_to_started_ms",
                _duration_ms(created_at, started_at),
            ),
            (
                "database_job_started_to_completed_ms",
                _duration_ms(started_at, completed_at),
            ),
            (
                "database_job_created_to_completed_ms",
                _duration_ms(created_at, completed_at),
            ),
        )
        for name, duration in durations:
            if duration is not None:
                samples[name].append(duration)

    for started_at, ended_at in attempt_timestamps:
        duration = _duration_ms(started_at, ended_at)
        if duration is not None:
            samples["database_attempt_started_to_ended_ms"].append(duration)
    return samples


@dataclass
class BenchmarkMetrics:
    lock: threading.Lock = field(init=False)
    samples: dict[str, list[float]] = field(init=False)
    submitted_at: dict[UUID, float] = field(init=False)
    enqueued_at: dict[UUID, float] = field(init=False)
    dequeued_at: dict[UUID, float] = field(init=False)
    completed_observed_at: dict[UUID, float] = field(init=False)
    handler_completed_at: dict[UUID, float] = field(init=False)

    def __post_init__(self) -> None:
        self.lock = threading.Lock()
        self.samples: dict[str, list[float]] = defaultdict(list)
        self.submitted_at = {}
        self.enqueued_at: dict[UUID, float] = {}
        self.dequeued_at: dict[UUID, float] = {}
        self.completed_observed_at: dict[UUID, float] = {}
        self.handler_completed_at: dict[UUID, float] = {}

    def record(self, name: str, duration_seconds: float) -> None:
        with self.lock:
            self.samples[name].append(duration_seconds * 1000)

    def record_enqueue(self, job_id: UUID) -> None:
        with self.lock:
            enqueued_at = time.perf_counter()
            self.enqueued_at.setdefault(job_id, enqueued_at)
            submitted_at = self.submitted_at.get(job_id)
            if submitted_at is not None:
                self.samples["submission_to_redis_enqueue_ms"].append(
                    (enqueued_at - submitted_at) * 1000
                )

    def record_submission(self, job_id: UUID, submitted_at: float) -> None:
        with self.lock:
            self.submitted_at[job_id] = submitted_at

    def record_dequeue(self, job_id: UUID) -> None:
        with self.lock:
            self.dequeued_at.setdefault(job_id, time.perf_counter())

    def record_completion(self, job_id: UUID) -> None:
        with self.lock:
            self.completed_observed_at.setdefault(job_id, time.perf_counter())

    def record_handler_completion(self, job_id: UUID) -> None:
        with self.lock:
            self.handler_completed_at[job_id] = time.perf_counter()

    def record_handler_to_result_update(self, job_id: UUID) -> None:
        with self.lock:
            completed_at = self.handler_completed_at.get(job_id)
            if completed_at is not None:
                self.samples["handler_to_result_update_ms"].append(
                    (time.perf_counter() - completed_at) * 1000
                )

    def snapshot_samples(self) -> dict[str, list[float]]:
        with self.lock:
            snapshot = {name: list(values) for name, values in self.samples.items()}
            queue_waits = [
                (dequeued - enqueued) * 1000
                for job_id, enqueued in self.enqueued_at.items()
                if (dequeued := self.dequeued_at.get(job_id)) is not None
            ]
            snapshot["queue_wait_ms"] = queue_waits
            return snapshot


class TimedQueueManager(QueueManager):
    def __init__(
        self,
        *,
        client: redis.Redis,
        key: str,
        metrics: BenchmarkMetrics,
    ) -> None:
        super().__init__(client=client, key=key)
        self.metrics = metrics

    def enqueue(self, message: QueueMessage, created_at: datetime) -> bool:
        started = time.perf_counter()
        try:
            added = super().enqueue(message, created_at)
        finally:
            self.metrics.record("redis_enqueue_ms", time.perf_counter() - started)
        if added:
            self.metrics.record_enqueue(message.job_id)
        return added

    def pop_job_id(self) -> UUID | None:
        started = time.perf_counter()
        try:
            job_id = super().pop_job_id()
        finally:
            duration = time.perf_counter() - started
        if job_id is not None:
            self.metrics.record("redis_dequeue_ms", duration)
            self.metrics.record_dequeue(job_id)
        else:
            self.metrics.record("redis_empty_pop_ms", duration)
        return job_id


class TimedWorker(Worker):
    def __init__(self, *args: Any, metrics: BenchmarkMetrics, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.metrics = metrics

    def _claim_job(self, job_id: UUID) -> ClaimedJob | None:
        started = time.perf_counter()
        try:
            return super()._claim_job(job_id)
        finally:
            self.metrics.record("worker_claim_ms", time.perf_counter() - started)

    def _finish_job(self, lease: Any, result: HandlerResult) -> None:
        started = time.perf_counter()
        try:
            super()._finish_job(lease, result)
        finally:
            self.metrics.record("worker_result_update_ms", time.perf_counter() - started)
            self.metrics.record_handler_to_result_update(lease.job_id)

    def process_job(self, job_id: UUID) -> bool:
        started = time.perf_counter()
        try:
            return super().process_job(job_id)
        finally:
            self.metrics.record("worker_total_ms", time.perf_counter() - started)


@dataclass
class IsolatedBenchmarkResources:
    session_factory: sessionmaker[Session]
    engine: Engine
    redis_client: redis.Redis
    queue: TimedQueueManager
    client: TestClient
    metrics: BenchmarkMetrics
    run_id: str
    scenario: tuple[int, int]
    pdf_output_dir: Path


@pytest.fixture()
def isolated_benchmark_resources(
    configured_database_url: str,
    benchmark_scenario: tuple[int, int],
) -> Any:
    test_database_url = os.environ.get("TEST_DATABASE_URL")
    redis_test_url = os.environ.get("REDIS_TEST_URL")
    try:
        base_database_url = validate_test_database_url(
            test_database_url,
            configured_database_url,
        )
        validate_test_redis_url(redis_test_url, settings.redis_url)
    except DatabaseSafetyError as exc:
        pytest.fail(str(exc))

    assert redis_test_url is not None
    if urlsplit(redis_test_url).path.strip("/") != "15":
        pytest.fail("REDIS_TEST_URL must use the dedicated Redis database index 15")

    run_id = uuid4().hex
    schema_name = f"djq_week7_{run_id}"
    queue_key = f"djq:benchmark:{run_id}"
    admin_engine = create_engine(base_database_url, pool_pre_ping=True)
    engine: Engine | None = None
    redis_client: redis.Redis | None = None
    schema_created = False
    original_database_url = settings.database_url
    original_pdf_output_dir = settings.pdf_output_dir
    pdf_output_temp = tempfile.TemporaryDirectory(prefix="week7-pdf-")
    pdf_output_dir = Path(pdf_output_temp.name)
    settings.pdf_output_dir = str(pdf_output_dir)
    missing_override = object()
    previous_get_db = app.dependency_overrides.get(get_db, missing_override)

    try:
        try:
            with admin_engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError as exc:
            unavailable_integration(
                "TEST_DATABASE_URL is unreachable "
                f"({type(exc).__name__}); benchmark not run"
            )

        try:
            with admin_engine.begin() as connection:
                connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
            schema_created = True
        except SQLAlchemyError as exc:
            pytest.fail(
                "TEST_DATABASE_URL cannot create an isolated schema "
                f"({type(exc).__name__})"
            )

        schema_url = base_database_url.update_query_dict(
            {"options": f"-csearch_path={schema_name}"}
        )
        settings.database_url = schema_url.render_as_string(hide_password=False)
        alembic_config = Config(str(BASE_DIR / "alembic.ini"))
        command.upgrade(alembic_config, "head")
        engine = create_engine(schema_url, pool_pre_ping=True)
        session_factory = sessionmaker(
            bind=engine,
            autoflush=False,
            expire_on_commit=False,
        )

        redis_client = redis.Redis.from_url(
            redis_test_url,
            decode_responses=True,
            socket_connect_timeout=settings.redis_connect_timeout_seconds,
            socket_timeout=settings.redis_socket_timeout_seconds,
        )
        try:
            redis_client.ping()
        except RedisError as exc:
            unavailable_integration(
                f"REDIS_TEST_URL is unreachable ({type(exc).__name__}); "
                "benchmark not run"
            )

        metrics = BenchmarkMetrics()
        queue = TimedQueueManager(
            client=redis_client,
            key=queue_key,
            metrics=metrics,
        )

        def override_get_db() -> Any:
            with session_factory() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db
        with TestClient(app) as client:
            yield IsolatedBenchmarkResources(
                session_factory=session_factory,
                engine=engine,
                redis_client=redis_client,
                queue=queue,
                client=client,
                metrics=metrics,
                run_id=run_id,
                scenario=benchmark_scenario,
                pdf_output_dir=pdf_output_dir,
            )
    finally:
        if previous_get_db is missing_override:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous_get_db
        settings.database_url = original_database_url
        settings.pdf_output_dir = original_pdf_output_dir
        pdf_output_temp.cleanup()
        if redis_client is not None:
            try:
                redis_client.delete(queue_key)
            finally:
                redis_client.close()
        if engine is not None:
            engine.dispose()
        if schema_created:
            with admin_engine.begin() as connection:
                connection.execute(
                    text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
                )
        admin_engine.dispose()


@pytest.fixture()
def benchmark_scenario() -> tuple[int, int]:
    try:
        return _parse_scenario()
    except ValueError as exc:
        pytest.fail(str(exc))


def _render_markdown_report(report: dict[str, Any]) -> str:
    result = report["results"]
    lines = [
        "# Week 7 Performance Benchmark",
        "",
        f"- Timestamp: {report['timestamp']}",
        f"- Workload: {report['workload']['jobs']} jobs",
        f"- Workers: {report['workload']['workers']}",
        f"- Task: `{report['workload']['task_type']}`",
        "",
        "## Results",
        "",
        "| Metric | Result |",
        "|---|---:|",
        f"| Completed jobs | {result['completed_jobs']} |",
        f"| Failed jobs | {result['failed_jobs']} |",
        f"| End-to-end duration (s) | {result['end_to_end_duration_seconds']:.4f} |",
        f"| End-to-end throughput (jobs/s) | {result['end_to_end_throughput_jobs_per_second']:.2f} |",
        f"| Published jobs | {result['published_jobs']} |",
        f"| Publisher throughput (jobs/s) | {result['publisher_throughput_jobs_per_second']:.2f} |",
        "",
        "## Latency summaries",
        "",
        "| Stage | Samples | Average ms | P50 ms | P95 ms | P99 ms | Min ms | Max ms |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for stage, summary in result["latencies"].items():
        values = [
            summary["count"],
            summary["average_ms"],
            summary["p50_ms"],
            summary["p95_ms"],
            summary["p99_ms"],
            summary["minimum_ms"],
            summary["maximum_ms"],
        ]
        formatted = [
            str(values[0]),
            *(f"{value:.3f}" if value is not None else "n/a" for value in values[1:]),
        ]
        lines.append(f"| {stage} | " + " | ".join(formatted) + " |")
    lines.extend(
        [
            "",
            "## Method and limitations",
            "",
            report["methodology"],
            "",
            report["limitations"],
            "",
            f"Failed job IDs: {', '.join(result['failed_job_ids']) or 'none'}",
            "",
            "CPU and memory sampling: unavailable (standard-library-only run).",
            "",
        ]
    )
    return "\n".join(lines)


def _write_report(
    report: dict[str, Any],
    run_id: str,
    output_dir: Path | None = None,
) -> tuple[Path, Path]:
    if output_dir is None:
        output_dir = Path(__file__).resolve().parents[1] / "benchmark-results"
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"week7_{report['timestamp_filename']}_{run_id}"
    json_path = output_dir / f"{stem}.json"
    markdown_path = output_dir / f"{stem}.md"
    with json_path.open("x", encoding="utf-8") as report_file:
        json.dump(report, report_file, indent=2, sort_keys=True)
        report_file.write("\n")
    with markdown_path.open("x", encoding="utf-8") as report_file:
        report_file.write(_render_markdown_report(report))
    return json_path, markdown_path


def _report_fixture() -> dict[str, Any]:
    return {
        "timestamp": "2026-11-06T12:00:00+00:00",
        "timestamp_filename": "20261106T120000000000Z",
        "workload": {
            "jobs": 100,
            "workers": 2,
            "task_type": _TASK_TYPE,
        },
        "methodology": "TestClient and isolated worker timings.",
        "limitations": "In-process benchmark.",
        "results": {
            "completed_jobs": 100,
            "failed_jobs": 0,
            "failed_job_ids": [],
            "published_jobs": 100,
            "end_to_end_duration_seconds": 2.5,
            "end_to_end_throughput_jobs_per_second": 40.0,
            "publisher_throughput_jobs_per_second": 80.0,
            "latencies": {
                "api_submit_ms": {
                    "count": 100,
                    "average_ms": 10.0,
                    "p50_ms": 9.0,
                    "p95_ms": 14.0,
                    "p99_ms": 19.0,
                    "minimum_ms": 5.0,
                    "maximum_ms": 25.0,
                }
            },
        },
    }


def test_report_generation_writes_json_and_markdown(tmp_path: Path) -> None:
    report = _report_fixture()

    json_path, markdown_path = _write_report(
        report,
        "report-generation-test",
        output_dir=tmp_path,
    )

    assert json_path.is_file()
    assert json.loads(json_path.read_text(encoding="utf-8")) == report
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "2026-11-06T12:00:00+00:00" in markdown
    assert "100 jobs" in markdown
    assert "Workers: 2" in markdown
    assert _TASK_TYPE in markdown
    assert "40.00" in markdown
    assert "Host:" not in markdown


def test_report_generation_does_not_overwrite_existing_reports(
    tmp_path: Path,
) -> None:
    report = _report_fixture()
    stem = f"week7_{report['timestamp_filename']}_existing-report-test"
    json_path = tmp_path / f"{stem}.json"
    markdown_path = tmp_path / f"{stem}.md"
    json_path.write_text("existing JSON", encoding="utf-8")
    markdown_path.write_text("existing Markdown", encoding="utf-8")

    with pytest.raises(FileExistsError):
        _write_report(
            report,
            "existing-report-test",
            output_dir=tmp_path,
        )

    assert json_path.read_text(encoding="utf-8") == "existing JSON"
    assert markdown_path.read_text(encoding="utf-8") == "existing Markdown"


def _verify_pdf_outputs(
    session_factory: sessionmaker[Session],
    job_ids: list[UUID],
    output_dir: Path,
) -> int:
    output_root = output_dir.resolve()
    with session_factory() as session:
        rows = session.execute(
            select(Job.id, Job.status, Job.result).where(Job.id.in_(job_ids))
        ).all()

    verified = 0
    for job_id, status_value, result in rows:
        if status_value != "completed":
            continue
        if not isinstance(result, dict):
            raise AssertionError(f"Completed PDF job {job_id} has no result")
        data = result.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("output_path"), str):
            raise AssertionError(f"Completed PDF job {job_id} has no output path")

        output_path = Path(data["output_path"]).resolve(strict=True)
        try:
            output_path.relative_to(output_root)
        except ValueError as exc:
            raise AssertionError(
                f"PDF output for job {job_id} escaped the temporary directory"
            ) from exc
        with output_path.open("rb") as pdf_file:
            if pdf_file.read(5) != b"%PDF-":
                raise AssertionError(f"PDF output for job {job_id} is invalid")
        verified += 1
    return verified


def _run_benchmark(
    resources: IsolatedBenchmarkResources,
    *,
    job_count: int,
    worker_count: int,
) -> dict[str, Any]:
    workers: list[TimedWorker] = []
    stop_events: list[threading.Event] = []
    worker_threads: list[threading.Thread] = []
    task_registration = TASK_REGISTRY[_TASK_TYPE]
    payload_model, original_handler = task_registration

    def timed_pdf_handler(
        payload: GeneratePDFPayload,
        *,
        job_id: UUID | None = None,
    ) -> HandlerResult:
        started = time.perf_counter()
        try:
            return original_handler(payload, job_id=job_id)
        finally:
            resources.metrics.record(
                "handler_execution_ms",
                time.perf_counter() - started,
            )
            if job_id is not None:
                resources.metrics.record_handler_completion(job_id)

    for worker_index in range(worker_count):
        worker = TimedWorker(
            resources.queue,
            session_factory=resources.session_factory,
            dispatch_function=dispatch,
            worker_id=f"week7-{resources.run_id}-{worker_index}",
            poll_interval=0.01,
            metrics=resources.metrics,
        )
        stop_event = threading.Event()
        thread = threading.Thread(
            target=worker.run,
            args=(stop_event,),
            name=f"week7-worker-{worker_index}",
            daemon=True,
        )
        worker_threads.append(thread)
        stop_events.append(stop_event)
        workers.append(worker)

    started_threads: list[threading.Thread] = []
    TASK_REGISTRY[_TASK_TYPE] = (payload_model, timed_pdf_handler)
    try:
        for thread in worker_threads:
            thread.start()
            started_threads.append(thread)

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if any(not thread.is_alive() for thread in started_threads):
                raise RuntimeError("A benchmark worker exited during startup")
            with resources.session_factory() as session:
                worker_count_ready = session.scalar(
                    select(func.count())
                    .select_from(WorkerRecord)
                    .where(
                        WorkerRecord.status == "active",
                        WorkerRecord.worker_id.like(f"week7-{resources.run_id}-%"),
                    )
                )
            if worker_count_ready == worker_count:
                break
            time.sleep(0.01)
        else:
            raise RuntimeError("Benchmark workers did not become ready")

        submission_started = time.perf_counter()
        submit_latencies: list[float] = []
        submission_times: dict[UUID, float] = {}
        for index in range(job_count):
            request_started = time.perf_counter()
            response = resources.client.post(
                "/jobs",
                json={
                    "task_type": _TASK_TYPE,
                    "payload": _TASK_PAYLOAD,
                },
            )
            response_finished = time.perf_counter()
            if response.status_code != 201:
                raise AssertionError(
                    f"Benchmark job submission returned HTTP {response.status_code}"
                )
            job_id = UUID(response.json()["id"])
            resources.metrics.record_submission(job_id, response_finished)
            submission_times[job_id] = response_finished
            submit_latencies.append((response_finished - request_started) * 1000)
        submission_duration = time.perf_counter() - submission_started

        publisher_started = time.perf_counter()
        with resources.session_factory() as session:
            publisher_result = publish_pending_events(
                session,
                resources.queue,
                batch_size=job_count,
            )
        publisher_duration = time.perf_counter() - publisher_started
        if publisher_result["published"] != job_count or publisher_result["failed"]:
            raise AssertionError(
                "Publisher did not publish the complete benchmark workload"
            )

        job_ids = list(submission_times)
        terminal_statuses: dict[UUID, str] = {}
        completion_deadline = time.monotonic() + 300
        end_to_end_started = submission_started
        while len(terminal_statuses) < job_count:
            with resources.session_factory() as session:
                rows = session.execute(
                    select(Job.id, Job.status).where(Job.id.in_(job_ids))
                ).all()
            for job_id, status_value in rows:
                if status_value in {"completed", "failed"}:
                    terminal_statuses[job_id] = status_value
                    resources.metrics.record_completion(job_id)
            if len(terminal_statuses) == job_count:
                break
            if time.monotonic() >= completion_deadline:
                raise TimeoutError(
                    f"Only {len(terminal_statuses)} of {job_count} jobs "
                    "reached a final state"
                )
            time.sleep(0.02)

        end_to_end_duration = max(
            resources.metrics.completed_observed_at.values(),
            default=time.perf_counter(),
        ) - end_to_end_started

        get_latencies: list[float] = []
        for job_id in job_ids:
            request_started = time.perf_counter()
            response = resources.client.get(f"/jobs/{job_id}")
            get_latencies.append((time.perf_counter() - request_started) * 1000)
            if response.status_code != 200:
                raise AssertionError(
                    "Benchmark job retrieval did not return HTTP 200"
                )

        samples = resources.metrics.snapshot_samples()
        samples.update(_database_lifecycle_samples(resources.session_factory, job_ids))
        samples["api_submit_ms"] = submit_latencies
        samples["api_get_ms"] = get_latencies
        samples["api_submission_duration_ms"] = [submission_duration * 1000]
        samples["publisher_duration_ms"] = [publisher_duration * 1000]
        samples["end_to_end_job_ms"] = [
            (resources.metrics.completed_observed_at[job_id] - submitted) * 1000
            for job_id, submitted in submission_times.items()
            if job_id in resources.metrics.completed_observed_at
        ]
        samples["successful_completion_ms"] = [
            (
                resources.metrics.completed_observed_at[job_id] - submitted
            )
            * 1000
            for job_id, submitted in submission_times.items()
            if terminal_statuses.get(job_id) == "completed"
        ]
        verified_pdf_files = _verify_pdf_outputs(
            resources.session_factory,
            job_ids,
            resources.pdf_output_dir,
        )

        completed_jobs = sum(
            status_value == "completed"
            for status_value in terminal_statuses.values()
        )
        failed_job_ids = [
            str(job_id)
            for job_id, status_value in terminal_statuses.items()
            if status_value == "failed"
        ]
        latencies = {
            name: _sample_summary(values)
            for name, values in sorted(samples.items())
        }
        return {
            "completed_jobs": completed_jobs,
            "failed_jobs": len(failed_job_ids),
            "failed_job_ids": failed_job_ids,
            "verified_pdf_files": verified_pdf_files,
            "published_jobs": publisher_result["published"],
            "submission_throughput_jobs_per_second": (
                job_count / submission_duration if submission_duration else 0
            ),
            "publisher_throughput_jobs_per_second": (
                publisher_result["published"] / publisher_duration
                if publisher_duration
                else 0
            ),
            "end_to_end_duration_seconds": end_to_end_duration,
            "end_to_end_throughput_jobs_per_second": (
                completed_jobs / end_to_end_duration if end_to_end_duration else 0
            ),
            "latencies": latencies,
        }
    finally:
        try:
            for event in stop_events:
                event.set()
            for thread in started_threads:
                thread.join(timeout=30)
                if thread.is_alive():
                    raise RuntimeError("Benchmark worker did not stop gracefully")
        finally:
            TASK_REGISTRY[_TASK_TYPE] = task_registration


@pytest.mark.performance
@pytest.mark.skipif(
    os.environ.get("WEEK7_RUN") != "1",
    reason="Set WEEK7_RUN=1 to explicitly execute the performance benchmark",
)
def test_performance_benchmark(
    isolated_benchmark_resources: IsolatedBenchmarkResources,
) -> None:
    resources = isolated_benchmark_resources
    job_count, worker_count = resources.scenario
    started_at = datetime.now(timezone.utc)
    result = _run_benchmark(
        resources,
        job_count=job_count,
        worker_count=worker_count,
    )
    timestamp = started_at.isoformat()
    report: dict[str, Any] = {
        "timestamp": timestamp,
        "timestamp_filename": started_at.strftime("%Y%m%dT%H%M%S%fZ"),
        "workload": {
            "jobs": job_count,
            "workers": worker_count,
            "task_type": _TASK_TYPE,
        },
        "resource_usage": {
            "cpu": None,
            "memory": None,
            "note": "Not sampled by this standard-library-only benchmark.",
        },
        "methodology": (
            "API requests use FastAPI TestClient in-process. The outbox publisher "
            "function and Redis queue use the real isolated test services. Worker "
            "instances run as threads in this Python process and execute the real "
            "generate_pdf handler using an identical payload for every job. PDF "
            "files are written under a temporary output directory and removed after "
            "workers stop. Worker startup is complete before workload timing starts. "
            "Same-process durations use time.perf_counter(). Job and attempt lifecycle "
            "intervals use only timestamps read from PostgreSQL; no interval combines "
            "database timestamps with OutboxEvent.published_at."
        ),
        "limitations": (
            "This benchmark excludes real HTTP socket/server overhead and process "
            "startup. Worker-count comparisons use threads on one local machine. "
            "Queue wait is measured between Redis enqueue and dequeue calls using "
            "the benchmark process monotonic clock. Completion latency is observed "
            "through DB polling and includes up to one poll interval of observation "
            "delay. PostgreSQL lifecycle fields mark application transitions, not "
            "the exact transaction commit time. CPU and memory measurements are "
            "unavailable."
        ),
        "results": result,
    }
    json_path, markdown_path = _write_report(report, resources.run_id)
    assert result["completed_jobs"] + result["failed_jobs"] == job_count
    assert result["failed_jobs"] == 0
    assert result["verified_pdf_files"] == result["completed_jobs"]
    print(f"JSON report: {json_path}")
    print(f"Markdown report: {markdown_path}")


def test_sample_summary_reports_only_supported_percentiles() -> None:
    small = _sample_summary([1.0, 3.0, 2.0])
    large = _sample_summary([float(value) for value in range(1, 101)])
    empty = _sample_summary([])

    assert small["count"] == 3
    assert small["p50_ms"] == 2.0
    assert small["p95_ms"] is None
    assert small["p99_ms"] is None
    assert large["p95_ms"] == pytest.approx(95.05)
    assert large["p99_ms"] == pytest.approx(99.01)
    assert empty["average_ms"] is None


def test_database_lifecycle_samples_use_existing_timestamps() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )
    created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    job = Job(
        task_type=_TASK_TYPE,
        payload={},
        created_at=created_at,
        started_at=created_at.replace(second=2),
        completed_at=created_at.replace(second=5),
    )

    try:
        with session_factory() as session:
            session.add(job)
            session.flush()
            session.add(
                JobAttempt(
                    job_id=job.id,
                    attempt_number=1,
                    worker_id="benchmark-test",
                    started_at=created_at.replace(microsecond=500_000, second=2),
                    ended_at=created_at.replace(microsecond=500_000, second=4),
                    outcome="completed",
                )
            )
            session.commit()

        samples = _database_lifecycle_samples(session_factory, [job.id])
        assert samples == {
            "database_job_created_to_started_ms": [2000.0],
            "database_job_started_to_completed_ms": [3000.0],
            "database_job_created_to_completed_ms": [5000.0],
            "database_attempt_started_to_ended_ms": [2000.0],
        }
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_duration_ms_rejects_out_of_order_database_timestamps() -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2025, 12, 31, tzinfo=timezone.utc)

    with pytest.raises(ValueError, match="out of order"):
        _duration_ms(start, end)


@pytest.mark.parametrize(
    ("job_count", "worker_count", "valid"),
    [
        ("100", "1", True),
        ("500", "2", True),
        ("1000", "4", True),
        ("1001", "1", False),
        ("100", "3", False),
        ("many", "1", False),
    ],
)
def test_scenario_parser_enforces_benchmark_caps(
    monkeypatch: pytest.MonkeyPatch,
    job_count: str,
    worker_count: str,
    valid: bool,
) -> None:
    monkeypatch.setenv("WEEK7_JOB_COUNT", job_count)
    monkeypatch.setenv("WEEK7_WORKER_COUNT", worker_count)

    if valid:
        assert _parse_scenario() == (int(job_count), int(worker_count))
    else:
        with pytest.raises(ValueError):
            _parse_scenario()
