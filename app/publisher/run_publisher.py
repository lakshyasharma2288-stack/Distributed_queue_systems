import argparse
import logging
import signal
from threading import Event

from app.core.config import settings
from app.db.database import SessionLocal
from app.publisher.outbox_publisher import publish_pending_events
from app.queue.queue_manager import QueueManager
from app.queue.redis_client import close_redis_client
from app.worker.retry_scheduler import publish_due_retries
from app.worker.recovery import reconcile_pending_jobs, recover_interrupted_jobs

logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish pending outbox events.")
    run_mode = parser.add_mutually_exclusive_group()
    run_mode.add_argument("--once", action="store_true", help="Run one publisher batch.")
    run_mode.add_argument("--loop", action="store_true", help="Poll continuously.")
    parser.add_argument("--batch-size", type=int, default=settings.outbox_batch_size)
    parser.add_argument(
        "--interval",
        type=float,
        default=settings.outbox_poll_interval_seconds,
    )
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch-size must be positive")
    if args.interval <= 0:
        parser.error("--interval must be positive")
    return args


def _run_batch(queue: QueueManager, batch_size: int, stop_event: Event | None = None) -> None:
    with SessionLocal() as session:
        result = publish_pending_events(
            session,
            queue,
            batch_size,
            stop_event=stop_event,
        )
        recovery_result = recover_interrupted_jobs(batch_size)
        retry_result = publish_due_retries(
            session,
            queue,
            settings.retry_scheduler_batch_size,
        )
        reconciliation_result = reconcile_pending_jobs(
            session,
            queue,
            batch_size,
        )
    logger.info(
        "Publisher batch complete fetched=%d published=%d failed=%d still_pending=%d",
        result["fetched"],
        result["published"],
        result["failed"],
        result["still_pending"],
    )
    logger.info(
        "Retry scheduler batch complete published=%d still_due=%d",
        retry_result["published"],
        retry_result["still_due"],
    )
    logger.info(
        "Recovery batch complete recovered=%d retried=%d dead_lettered=%d "
        "pending_reenqueued=%d",
        recovery_result["recovered"],
        recovery_result["retried"],
        recovery_result["dead_lettered"],
        reconciliation_result["enqueued"],
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    args = _parse_args()
    queue = QueueManager()

    if not args.loop:
        try:
            _run_batch(queue, args.batch_size)
        finally:
            close_redis_client()
        return

    stop_event = Event()
    original_sigint = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda _signum, _frame: stop_event.set())
    try:
        while not stop_event.is_set():
            _run_batch(queue, args.batch_size, stop_event)
            stop_event.wait(args.interval)
    finally:
        signal.signal(signal.SIGINT, original_sigint)
        close_redis_client()


if __name__ == "__main__":
    main()
