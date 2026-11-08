import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen
from uuid import uuid4

import pytest
import redis
from alembic import command
from alembic.config import Config
from redis.exceptions import RedisError
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import settings
from week4_safety import (
    DatabaseSafetyError,
    unavailable_integration,
    validate_test_database_url,
    validate_test_redis_url,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def isolated_test_database_url(configured_database_url: str):
    test_database_url = os.environ.get("TEST_DATABASE_URL")
    if not test_database_url:
        unavailable_integration(
            "TEST_DATABASE_URL is not set; worker end-to-end test unverified"
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
            "worker end-to-end test unverified"
        )

    schema_name = f"djq_week5_{uuid4().hex}"
    try:
        with admin_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
    except SQLAlchemyError as exc:
        admin_engine.dispose()
        unavailable_integration(
            f"TEST_DATABASE_URL cannot create an isolated schema "
            f"({type(exc).__name__}); worker end-to-end test unverified"
        )

    original_database_url = settings.database_url
    schema_url = base_url.update_query_dict(
        {"options": f"-csearch_path={schema_name}"}
    )
    settings.database_url = schema_url.render_as_string(hide_password=False)
    try:
        alembic_config = Config(str(PROJECT_ROOT / "alembic.ini"))
        command.upgrade(alembic_config, "head")
        yield settings.database_url
    finally:
        settings.database_url = original_database_url
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        admin_engine.dispose()


def _free_local_port() -> int:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


def _wait_for_api(url: str, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail("API process exited before becoming ready")
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, URLError):
            time.sleep(0.1)
    pytest.fail("API process did not become ready")


def _request_json(url: str, *, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    with urlopen(request, timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


def _wait_for_job(
    base_url: str,
    job_id: str,
    expected_status: str,
    worker_processes: list[subprocess.Popen],
) -> dict:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if any(process.poll() is not None for process in worker_processes):
            pytest.fail("Worker process exited before the job reached a final state")
        job = _request_json(f"{base_url}/jobs/{job_id}")
        if job["status"] in {"completed", "failed"}:
            assert job["status"] == expected_status
            return job
        time.sleep(0.1)
    pytest.fail(f"Job did not reach {expected_status} before the timeout")


@pytest.mark.integration
def test_api_publisher_and_worker_processes_complete_jobs(
    isolated_test_database_url: str,
    tmp_path: Path,
) -> None:
    redis_url = os.environ.get("REDIS_TEST_URL")
    if not redis_url:
        unavailable_integration(
            "REDIS_TEST_URL is not set; worker end-to-end test unverified"
        )
    try:
        validate_test_redis_url(redis_url, settings.redis_url)
    except DatabaseSafetyError as exc:
        pytest.fail(str(exc))
    if redis_url.rstrip("/").rsplit("/", 1)[-1] != "15":
        pytest.fail("REDIS_TEST_URL must use the dedicated Redis database index 15")

    redis_client = redis.Redis.from_url(redis_url, decode_responses=True)
    try:
        redis_client.ping()
    except RedisError as exc:
        redis_client.close()
        unavailable_integration(
            f"REDIS_TEST_URL is unreachable ({type(exc).__name__}); "
            "worker end-to-end test unverified"
        )

    queue_key = f"djq:test:week5:{uuid4().hex}"
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": isolated_test_database_url,
            "REDIS_URL": redis_url,
            "QUEUE_KEY": queue_key,
            "WORKER_POLL_INTERVAL_SECONDS": "0.1",
            "PDF_OUTPUT_DIR": str(tmp_path / "pdf-output"),
        }
    )
    port = _free_local_port()
    base_url = f"http://127.0.0.1:{port}"
    process_group_flag = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    api_process = None
    worker_processes: list[subprocess.Popen] = []
    try:
        api_process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=PROJECT_ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=process_group_flag,
        )
        _wait_for_api(f"{base_url}/health", api_process)

        for _ in range(3):
            worker_processes.append(
                subprocess.Popen(
                    [sys.executable, "-m", "app.worker.runner"],
                    cwd=PROJECT_ROOT,
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=process_group_flag,
                )
            )
        assert len({process.pid for process in worker_processes}) == 3

        completed_responses = [
            _request_json(
                f"{base_url}/jobs",
                payload={
                    "task_type": "generate_pdf",
                    "payload": {
                        "title": f"Worker integration check {index}",
                        "sections": [
                            {
                                "heading": "Result",
                                "paragraphs": ["Completed by worker."],
                            }
                        ],
                    },
                },
            )
            for index in range(3)
        ]
        failed_response = _request_json(
            f"{base_url}/jobs",
            payload={"task_type": "unsupported_test_task", "payload": {}},
        )

        publisher_result = subprocess.run(
            [
                sys.executable,
                "-m",
                "app.publisher.run_publisher",
                "--once",
            ],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert publisher_result.returncode == 0, "Publisher process failed"

        completed_jobs = [
            _wait_for_job(
                base_url,
                response["id"],
                "completed",
                worker_processes,
            )
            for response in completed_responses
        ]
        failed = _wait_for_job(
            base_url,
            failed_response["id"],
            "failed",
            worker_processes,
        )
        for completed in completed_jobs:
            assert completed["result"]["success"] is True
            output_path = Path(completed["result"]["data"]["output_path"])
            assert output_path.is_file()
        assert failed["error_message"] == (
            "UNSUPPORTED_TASK: Unsupported task type."
        )
    finally:
        shutdown_signal = getattr(signal, "CTRL_BREAK_EVENT", signal.SIGINT)
        for worker_process in worker_processes:
            if worker_process.poll() is None:
                try:
                    worker_process.send_signal(shutdown_signal)
                    worker_process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    worker_process.terminate()
                    worker_process.wait(timeout=5)
        if api_process is not None and api_process.poll() is None:
            api_process.terminate()
            api_process.wait(timeout=5)
        try:
            redis_client.delete(queue_key)
        finally:
            redis_client.close()
