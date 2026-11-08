import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
import socket
from threading import Event, Lock
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.database import SessionLocal
from app.models.job import Job
from app.models.attempt import JobAttempt
from app.models.dead_letter import DeadLetter
from app.queue.exceptions import QueueConfigurationError, QueueUnavailableError
from app.queue.queue_manager import QueueManager
from app.queue.redis_client import masked_redis_error
from app.tasks.dispatcher import dispatch
from app.tasks.schemas import HandlerResult, failure_result
from app.worker.heartbeat import WorkerHeartbeat
from app.worker.leases import JobLease
from app.worker.retry_policy import compute_retry_delay_seconds
from app.worker.state_machine import transition_job

logger = logging.getLogger(__name__)
SessionFactory = Callable[[], Session]
DispatchFunction = Callable[[str, dict[str, Any]], HandlerResult]
PROCESS_WORKER_ID = f"{socket.gethostname()}-{os.getpid()}-{uuid4().hex}"


@dataclass(frozen=True)
class ClaimedJob:
    task_type: str
    payload: dict[str, Any]
    lease: JobLease


def _safe_error_message(result: HandlerResult) -> str:
    if result.error is None:
        return "TASK_FAILED: Task execution failed."

    code = result.error.code[:100]
    message = result.error.message
    if code == "UNSUPPORTED_TASK":
        message = "Unsupported task type."
    elif code == "INTERNAL_ERROR":
        message = "An unexpected error occurred while handling the task."
    message = " ".join(message.split())[:1000] or "Task execution failed."
    return f"{code}: {message}"


class Worker:
    def __init__(
        self,
        queue: QueueManager | None = None,
        *,
        session_factory: SessionFactory = SessionLocal,
        dispatch_function: DispatchFunction = dispatch,
        poll_interval: float | None = None,
        worker_id: str | None = None,
    ) -> None:
        interval = (
            settings.worker_poll_interval_seconds
            if poll_interval is None
            else poll_interval
        )
        if interval <= 0:
            raise ValueError("poll_interval must be positive")

        self.queue = queue or QueueManager()
        self.session_factory = session_factory
        self.dispatch_function = dispatch_function
        self.poll_interval = interval
        self.worker_id = worker_id or PROCESS_WORKER_ID
        heartbeat_interval = settings.worker_heartbeat_interval_seconds
        heartbeat_timeout = settings.worker_heartbeat_timeout_seconds
        if heartbeat_interval <= 0:
            raise ValueError("worker heartbeat interval must be positive")
        if heartbeat_timeout < 3 * heartbeat_interval:
            raise ValueError(
                "worker heartbeat timeout must be at least three heartbeat intervals"
            )
        self._heartbeat_interval = heartbeat_interval
        self._heartbeat_timeout = heartbeat_timeout
        self._lease_duration = settings.worker_lease_duration_seconds
        self._lease_renewal_interval = settings.worker_lease_renewal_interval_seconds
        if self._lease_duration <= 0:
            raise ValueError("worker lease duration must be positive")
        if self._lease_renewal_interval <= 0:
            raise ValueError("worker lease renewal interval must be positive")
        if self._lease_renewal_interval > self._lease_duration / 3:
            raise ValueError(
                "worker lease renewal interval must be at most one third "
                "of the lease duration"
            )
        if settings.default_max_attempts < 1:
            raise ValueError("default max attempts must be positive")
        if settings.retry_base_delay_seconds <= 0:
            raise ValueError("retry base delay must be positive")
        if settings.retry_max_delay_seconds <= 0:
            raise ValueError("retry maximum delay must be positive")
        if not 0 <= settings.retry_jitter_ratio <= 1:
            raise ValueError("retry jitter ratio must be between zero and one")
        self._current_job_id: UUID | None = None
        self._current_job_lock = Lock()
        self._heartbeat: WorkerHeartbeat | None = None
        self._current_lease: JobLease | None = None

    def run_once(self) -> bool:
        job_id = self.queue.pop_job_id()
        if job_id is None:
            return False

        try:
            self.process_job(job_id)
        except Exception as exc:
            logger.error(
                "Worker job failed unexpectedly job_id=%s worker_id=%s error_type=%s",
                job_id,
                self.worker_id,
                type(exc).__name__,
            )
        return True

    def process_job(self, job_id: UUID) -> bool:
        claimed = self._claim_job(job_id)
        if claimed is None:
            return False

        with self._current_job_lock:
            self._current_job_id = job_id
            self._current_lease = claimed.lease
        if self._heartbeat is not None:
            self._heartbeat.refresh()

        try:
            try:
                if self.dispatch_function is dispatch:
                    result = dispatch(
                        claimed.task_type,
                        claimed.payload,
                        job_id=job_id,
                    )
                else:
                    result = self.dispatch_function(
                        claimed.task_type,
                        claimed.payload,
                    )
            except Exception as exc:
                logger.error(
                    "Worker dispatcher failed job_id=%s worker_id=%s error_type=%s",
                    job_id,
                    self.worker_id,
                    type(exc).__name__,
                )
                result = failure_result(
                    claimed.task_type,
                    "INTERNAL_ERROR",
                    "An unexpected error occurred while handling the task.",
                )

            self._finish_job(claimed.lease, result)
            return True
        finally:
            with self._current_job_lock:
                self._current_job_id = None
                self._current_lease = None
            if self._heartbeat is not None:
                self._heartbeat.refresh()

    def run(self, stop_event: Event) -> None:
        logger.info("Worker started worker_id=%s", self.worker_id)
        heartbeat = WorkerHeartbeat(
            self.session_factory,
            worker_id=self.worker_id,
            hostname=socket.gethostname(),
            pid=os.getpid(),
            interval_seconds=self._heartbeat_interval,
            timeout_seconds=self._heartbeat_timeout,
            current_job=self._get_current_job,
            active_lease=self._get_current_lease,
            draining_event=stop_event,
            lease_duration_seconds=self._lease_duration,
            lease_renewal_interval_seconds=self._lease_renewal_interval,
        )
        self._heartbeat = heartbeat
        try:
            heartbeat.start()
            while not stop_event.is_set():
                try:
                    had_job = self.run_once()
                except (QueueUnavailableError, QueueConfigurationError) as exc:
                    logger.error(
                        "Worker queue unavailable worker_id=%s error=%s",
                        self.worker_id,
                        masked_redis_error(exc),
                    )
                    had_job = False
                except Exception as exc:
                    logger.error(
                        "Worker loop iteration failed worker_id=%s error_type=%s",
                        self.worker_id,
                        type(exc).__name__,
                    )
                    had_job = False

                if not had_job:
                    stop_event.wait(self.poll_interval)
        finally:
            stop_event.set()
            heartbeat.refresh()
            try:
                stopped = heartbeat.stop()
            finally:
                self._heartbeat = None
            if not stopped:
                logger.warning(
                    "Worker stopped after stale detection worker_id=%s",
                    self.worker_id,
                )
            logger.info("Worker stopped worker_id=%s", self.worker_id)

    def _get_current_job(self) -> UUID | None:
        with self._current_job_lock:
            return self._current_job_id

    def _get_current_lease(self) -> JobLease | None:
        with self._current_job_lock:
            return self._current_lease

    def _claim_job(self, job_id: UUID) -> ClaimedJob | None:
        try:
            with self.session_factory() as session:
                if session.get_bind().dialect.name == "sqlite":
                    lease_expires_at = func.datetime(
                        func.now(),
                        f"+{self._lease_duration:g} seconds",
                    )
                else:
                    lease_expires_at = func.now() + timedelta(
                        seconds=self._lease_duration
                    )
                claim = transition_job(
                    job_id,
                    expected_status="pending",
                    new_status="processing",
                    values={
                        "started_at": func.now(),
                        "attempts": Job.attempts + 1,
                        "lease_owner_id": self.worker_id,
                        "lease_expires_at": lease_expires_at,
                        "lease_version": Job.lease_version + 1,
                        "next_retry_at": None,
                    },
                )
                claimed = session.execute(
                    claim.returning(
                        Job.task_type,
                        Job.payload,
                        Job.lease_version,
                        Job.attempts,
                    )
                ).one_or_none()
                if claimed is None:
                    session.rollback()
                    logger.info(
                        "Worker skipped unclaimable job job_id=%s worker_id=%s",
                        job_id,
                        self.worker_id,
                    )
                    return None

                session.add(
                    JobAttempt(
                        job_id=job_id,
                        attempt_number=claimed.attempts,
                        worker_id=self.worker_id,
                        outcome="running",
                    )
                )
                session.commit()
                logger.info(
                    "Worker claimed job job_id=%s worker_id=%s attempt=%s lease_version=%s",
                    job_id,
                    self.worker_id,
                    claimed.attempts,
                    claimed.lease_version,
                )
                return ClaimedJob(
                    task_type=claimed.task_type,
                    payload=dict(claimed.payload),
                    lease=JobLease(
                        job_id=job_id,
                        worker_id=self.worker_id,
                        lease_version=claimed.lease_version,
                        attempt_number=claimed.attempts,
                    ),
                )
        except SQLAlchemyError as exc:
            logger.error(
                "Worker could not claim job job_id=%s worker_id=%s error_type=%s",
                job_id,
                self.worker_id,
                type(exc).__name__,
            )
            return None

    def _finish_job(self, lease: JobLease, result: HandlerResult) -> None:
        try:
            with self.session_factory() as session:
                database_time = func.now()
                dialect_name = session.get_bind().dialect.name
                lease_clock = (
                    func.clock_timestamp()
                    if dialect_name == "postgresql"
                    else database_time
                )
                job = session.scalar(
                    select(Job)
                    .where(
                        Job.id == lease.job_id,
                        Job.status == "processing",
                        Job.lease_owner_id == lease.worker_id,
                        Job.lease_version == lease.lease_version,
                        Job.lease_expires_at > lease_clock,
                    )
                    .with_for_update()
                )
                if job is None:
                    session.rollback()
                    logger.warning(
                        "Worker rejected stale job result job_id=%s worker_id=%s "
                        "lease_version=%s",
                        lease.job_id,
                        lease.worker_id,
                        lease.lease_version,
                    )
                    return

                error_message = None if result.success else _safe_error_message(result)
                if result.success:
                    new_status = "completed"
                    completed_at = database_time
                    retry_at = None
                    stored_result = result.model_dump(mode="json")
                elif result.error is not None and result.error.retryable and (
                    job.attempts < job.max_attempts
                ):
                    new_status = "retry_scheduled"
                    completed_at = None
                    retry_delay = compute_retry_delay_seconds(
                        job.attempts,
                        base_delay_seconds=settings.retry_base_delay_seconds,
                        max_delay_seconds=settings.retry_max_delay_seconds,
                        jitter_ratio=settings.retry_jitter_ratio,
                    )
                    if dialect_name == "sqlite":
                        retry_at = func.datetime(
                            database_time,
                            f"+{retry_delay:g} seconds",
                        )
                    else:
                        retry_at = database_time + timedelta(seconds=retry_delay)
                    stored_result = None
                else:
                    new_status = "failed"
                    completed_at = database_time
                    retry_at = None
                    stored_result = None

                transition = transition_job(
                    lease.job_id,
                    expected_status="processing",
                    new_status=new_status,
                    conditions=(
                        Job.lease_owner_id == lease.worker_id,
                        Job.lease_version == lease.lease_version,
                        Job.lease_expires_at > lease_clock,
                    ),
                    values={
                        "completed_at": completed_at,
                        "error_message": error_message,
                        "result": stored_result,
                        "next_retry_at": retry_at,
                        "lease_owner_id": None,
                        "lease_expires_at": None,
                    },
                )
                finished_job_id = session.scalar(
                    transition.returning(Job.id)
                )
                if finished_job_id is None:
                    session.rollback()
                    logger.warning(
                        "Worker rejected stale job result job_id=%s worker_id=%s "
                        "lease_version=%s",
                        lease.job_id,
                        lease.worker_id,
                        lease.lease_version,
                    )
                    return

                attempt_outcome = {
                    "completed": "completed",
                    "retry_scheduled": "retry_scheduled",
                    "failed": "dead",
                }[new_status]
                error_code = (
                    result.error.code[:100]
                    if result.error is not None
                    else ("TASK_FAILED" if not result.success else None)
                )
                updated_attempt_id = session.scalar(
                    update(JobAttempt)
                    .where(
                        JobAttempt.job_id == lease.job_id,
                        JobAttempt.attempt_number == lease.attempt_number,
                        JobAttempt.worker_id == lease.worker_id,
                        JobAttempt.outcome == "running",
                    )
                    .values(
                        ended_at=database_time,
                        outcome=attempt_outcome,
                        error_code=error_code,
                        error_message=error_message,
                        result=stored_result,
                    )
                    .returning(JobAttempt.job_id)
                )
                if updated_attempt_id is None:
                    raise RuntimeError(
                        "The active job attempt record could not be finalized"
                    )

                if new_status == "failed":
                    session.add(
                        DeadLetter(
                            job_id=lease.job_id,
                            final_error_code=error_code or "TASK_FAILED",
                            final_error_message=error_message or "Task failed.",
                        )
                    )
                session.commit()
                logger.info(
                    "Worker transitioned job job_id=%s worker_id=%s "
                    "attempt=%s state=processing->%s",
                    lease.job_id,
                    lease.worker_id,
                    lease.lease_version,
                    new_status,
                )
        except SQLAlchemyError as exc:
            logger.error(
                "Worker could not save job result job_id=%s worker_id=%s error_type=%s",
                lease.job_id,
                lease.worker_id,
                type(exc).__name__,
            )
