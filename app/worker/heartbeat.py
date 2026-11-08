import logging
import threading
from collections.abc import Callable
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from app.worker.registry import (
    mark_stale_workers,
    register_worker,
    stop_worker,
    update_worker_heartbeat,
)
from app.worker.leases import JobLease, renew_job_lease

logger = logging.getLogger(__name__)
CurrentJobProvider = Callable[[], UUID | None]
ActiveLeaseProvider = Callable[[], JobLease | None]


class WorkerHeartbeat:
    def __init__(
        self,
        session_factory,
        *,
        worker_id: str,
        hostname: str,
        pid: int,
        interval_seconds: float,
        timeout_seconds: float,
        current_job: CurrentJobProvider,
        active_lease: ActiveLeaseProvider,
        draining_event: threading.Event,
        lease_duration_seconds: float,
        lease_renewal_interval_seconds: float,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if timeout_seconds < 3 * interval_seconds:
            raise ValueError("timeout_seconds must be at least three heartbeat intervals")
        if lease_duration_seconds <= 0:
            raise ValueError("lease_duration_seconds must be positive")
        if lease_renewal_interval_seconds <= 0:
            raise ValueError("lease_renewal_interval_seconds must be positive")
        if lease_renewal_interval_seconds > lease_duration_seconds / 3:
            raise ValueError(
                "lease_renewal_interval_seconds must be at most one third "
                "of the lease duration"
            )

        self._session_factory = session_factory
        self.worker_id = worker_id
        self._hostname = hostname
        self._pid = pid
        self._interval_seconds = interval_seconds
        self._timeout_seconds = timeout_seconds
        self._current_job = current_job
        self._active_lease = active_lease
        self._draining_event = draining_event
        self._lease_duration_seconds = lease_duration_seconds
        self._lease_renewal_interval_seconds = lease_renewal_interval_seconds
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"worker-heartbeat-{worker_id}",
            daemon=True,
        )
        self._registered = False
        self._thread_started = False

    def start(self) -> None:
        register_worker(
            self._session_factory,
            worker_id=self.worker_id,
            hostname=self._hostname,
            pid=self._pid,
        )
        self._registered = True
        self._send_heartbeat()
        self._thread.start()
        self._thread_started = True

    def stop(self) -> bool:
        self._stop_event.set()
        if self._thread_started:
            self._thread.join(timeout=max(1.0, self._interval_seconds * 2))
            if self._thread.is_alive():
                raise RuntimeError("Worker heartbeat thread did not stop")

        if self._registered:
            return stop_worker(self._session_factory, worker_id=self.worker_id)
        return False

    def refresh(self) -> bool:
        return self._send_heartbeat()

    def _send_heartbeat(self) -> bool:
        try:
            updated = update_worker_heartbeat(
                self._session_factory,
                worker_id=self.worker_id,
                current_job_id=self._current_job(),
                status="draining" if self._draining_event.is_set() else "active",
                timeout_seconds=self._timeout_seconds,
            )
        except SQLAlchemyError as exc:
            logger.error(
                "Worker heartbeat failed worker_id=%s error_type=%s",
                self.worker_id,
                type(exc).__name__,
            )
            return False

        if not updated:
            logger.error(
                "Worker heartbeat rejected worker_id=%s; worker cannot be revived",
                self.worker_id,
            )
            self._draining_event.set()
            return False
        return True

    def _run(self) -> None:
        interval = min(
            self._interval_seconds,
            self._lease_renewal_interval_seconds,
        )
        while not self._stop_event.wait(interval):
            heartbeat_ok = self._send_heartbeat()
            try:
                stale_worker_ids = mark_stale_workers(
                    self._session_factory,
                    timeout_seconds=self._timeout_seconds,
                )
            except SQLAlchemyError as exc:
                logger.error(
                    "Worker stale check failed worker_id=%s error_type=%s",
                    self.worker_id,
                    type(exc).__name__,
                )
                continue
            for stale_worker_id in stale_worker_ids:
                logger.warning(
                    "Worker marked stale worker_id=%s detected_by=%s",
                    stale_worker_id,
                    self.worker_id,
                )

            lease = self._active_lease()
            if lease is None or not heartbeat_ok:
                continue
            try:
                renewed = renew_job_lease(
                    self._session_factory,
                    lease,
                    duration_seconds=self._lease_duration_seconds,
                )
            except SQLAlchemyError as exc:
                logger.error(
                    "Worker lease renewal failed job_id=%s worker_id=%s "
                    "lease_version=%s error_type=%s",
                    lease.job_id,
                    lease.worker_id,
                    lease.lease_version,
                    type(exc).__name__,
                )
                continue
            if not renewed:
                if self._active_lease() != lease:
                    continue
                logger.warning(
                    "Worker lost job lease job_id=%s worker_id=%s lease_version=%s",
                    lease.job_id,
                    lease.worker_id,
                    lease.lease_version,
                )
                self._draining_event.set()
