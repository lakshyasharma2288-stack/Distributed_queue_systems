import argparse
import logging
import signal
from threading import Event

from app.core.config import settings
from app.queue.redis_client import close_redis_client
from app.worker.worker import Worker

logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the distributed job worker.")
    parser.add_argument(
        "--interval",
        type=float,
        default=settings.worker_poll_interval_seconds,
        help="Seconds to wait when the queue is empty or unavailable.",
    )
    args = parser.parse_args()
    if args.interval <= 0:
        parser.error("--interval must be positive")
    return args


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    args = _parse_args()
    stop_event = Event()
    handled_signals = [signal.SIGINT]
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        handled_signals.append(sigbreak)
    previous_handlers = {
        signum: signal.getsignal(signum) for signum in handled_signals
    }

    def request_shutdown(_signum: int, _frame: object) -> None:
        logger.info("Worker shutdown requested")
        stop_event.set()

    try:
        for signum in handled_signals:
            signal.signal(signum, request_shutdown)
        Worker(poll_interval=args.interval).run(stop_event)
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        close_redis_client()


if __name__ == "__main__":
    main()
